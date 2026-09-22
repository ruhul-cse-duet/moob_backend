import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import UploadFile

from app.core.deps import CurrentUser
from app.core.enums import (
    CONSULTANT_ROLES,
    DocumentStatus,
    NotificationType,
    RequestStatus,
    Role,
)
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.i18n import DEFAULT_LANGUAGE
from app.core.i18n import normalize as normalize_language
from app.core.utils import oid, serialize, utcnow
from app.modules.deadlines import service as deadlines
from app.schemas.common import PageParams
from app.services import storage
from app.services.events import log_activity, notify
from app.services.ai_service import analyze_document
from app.services.ownership import (assert_client_access, assigned_client_ids,
                                     delegated_case_ids, resolve_consultant_id)
from app.services.pagination import paginate

logger = logging.getLogger("app.documents")


async def _get(db, document_id: str) -> Dict[str, Any]:
    doc = await db.documents.find_one({"_id": oid(document_id)})
    if not doc:
        raise NotFound("Document not found")
    return doc


async def list_documents(db, user: CurrentUser, params: PageParams,
                         request_id: Optional[str] = None,
                         case_id: Optional[str] = None,
                         status: Optional[DocumentStatus] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if user.role == Role.CLIENT:
        query["client_id"] = user.id
    elif user.role == Role.PARTNER:
        # Two ways a partner reaches a document: the whole client was handed to
        # them, or they hold a task on its case. Listing only the first meant a
        # delegated partner opened the case and found it empty.
        client_ids = await assigned_client_ids(db, user.id)
        case_ids = await delegated_case_ids(db, user.id)
        query["$or"] = [
            {"client_id": {"$in": client_ids}},
            {"case_id": {"$in": case_ids}},
        ]
    if request_id:
        query["request_id"] = request_id
    if case_id:
        query["case_id"] = case_id
    if status:
        query["status"] = status.value
    return await paginate(db, "documents", query, params, sort=[("created_at", 1)])


async def get_document(db, user: CurrentUser, document_id: str) -> Dict[str, Any]:
    doc = await _get(db, document_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This document is not yours")
    if user.role == Role.PARTNER:
        # A task on this document's case is a handover of the work on it, so it
        # carries the same authority the consultant had: read, approve, return
        # and re-read. Without the case id only a whole-client handover counted.
        await assert_client_access(db, user, doc["client_id"],
                                   case_id=doc.get("case_id"))
    return serialize(doc)


def _parse_ai_date(value: Any) -> Optional[datetime]:
    """The AI is asked for `YYYY-MM-DD or null`, not a timestamp - a date read
    off a passport has no time of day. Anything else it might send back (a
    stray sentence, a different format) is a date this module cannot trust,
    so it is treated the same as no date at all rather than raising.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def _track_expiry(db, document_id: str, doc: Dict[str, Any],
                        analysis: Dict[str, Any]) -> None:
    """Mirrors what the AI read off the document itself onto the worklist -
    the exact gap the client's own message named: an expiry date the AI
    already extracts, sitting on the document record with nothing reading it
    or alerting anyone as it approaches.

    Re-run on every analysis (a fresh upload, or a manual re-check) so a
    replaced file's new expiry replaces the old one, and a document that no
    longer reads as having one - or failed to analyse - drops the deadline
    rather than keep chasing a date from the file it replaced.
    """
    due = _parse_ai_date(analysis.get("expiry_date"))
    await deadlines.upsert(
        db, kind="document_expiry", source_collection="documents",
        source_id=document_id, due_date=due, title=doc["name"],
        consultant_id=doc.get("consultant_id"), client_id=doc.get("client_id"),
        client_name=doc.get("client_name"), case_id=doc.get("case_id"),
    )


async def _run_analysis(db, document_id: str, doc: Dict[str, Any],
                        file_meta: Dict[str, Any]) -> Dict[str, Any]:
    """Read the stored file back and ask Claude what it is.

    Reads from GridFS rather than taking the bytes it was just handed: the
    upload has already been consumed by the time this runs in the background,
    and the stored copy is the one the consultant will actually open.
    """
    # Written in the language of the consultant who will read it — the analysis
    # is text generated per document, so it is the one thing on the screen that
    # the translation files cannot reach.
    # Best effort, and deliberately unable to fail the analysis: reading the
    # reviewer's language is a nicety, the analysis is the point. `oid` raises
    # on anything that is not an ObjectId - a consultant id from an older
    # record, a fixture, an import - and that took the whole analysis down with
    # it, leaving the document marked "failed" for a reason nothing on screen
    # could explain.
    reviewer_lang = None
    reviewer_id = doc.get("consultant_id")
    if reviewer_id:
        try:
            reviewer = await db.users.find_one({"_id": oid(reviewer_id)},
                                               {"language": 1})
            reviewer_lang = (reviewer or {}).get("language")
        except Exception:  # noqa: BLE001
            logger.debug("Could not read the reviewer's language for %s", reviewer_id)

    analysis = await analyze_document(
        file_bytes=await storage.read_bytes(
            db, file_meta["file_id"],
            file_meta.get("bucket", storage.DOCUMENTS_BUCKET)),
        mime_type=file_meta.get("mime_type", "application/octet-stream"),
        document_name=doc["name"],
        context=doc.get("category", ""),
        lang=normalize_language(reviewer_lang) or DEFAULT_LANGUAGE,
    )
    await db.documents.update_one({"_id": oid(document_id)},
                                  {"$set": {"ai_analysis": analysis}})
    await _track_expiry(db, document_id, doc, analysis)
    return analysis


async def _analyze_later(db, document_id: str, doc: Dict[str, Any],
                         file_meta: Dict[str, Any]) -> None:
    """The same analysis, off the request's back and unable to break it.

    Claude takes seconds to read a document; a client on a phone must not hold
    the upload open for them, and a model that is down must not turn a stored
    file into a failed upload. So the record is written first with
    `status: analysing`, and this replaces it when the answer arrives - the
    consultant's screen polls the document it already has.
    """
    try:
        await _run_analysis(db, document_id, doc, file_meta)
    except Exception:  # noqa: BLE001
        logger.exception("Analysis failed for document %s", document_id)
        await db.documents.update_one(
            {"_id": oid(document_id)},
            {"$set": {"ai_analysis": {
                "status": "failed", "confidence": 0,
                "recommendation": "manual_review", "extracted_fields": {},
                "issues": [],
                "summary": "Automatic analysis failed. Review this document manually.",
            }}},
        )


async def upload(db, user: CurrentUser, document_id: str,
                 file: UploadFile) -> Dict[str, Any]:
    doc = await _get(db, document_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This document is not yours")
    if doc["status"] == DocumentStatus.APPROVED.value:
        raise BadRequest("This document is already approved")

    # Replacing an earlier upload: drop the old blob so GridFS does not accumulate orphans.
    if doc.get("file", {}) and doc["file"].get("file_id"):
        await storage.delete_file(db, doc["file"]["file_id"], storage.DOCUMENTS_BUCKET)

    now = utcnow()
    stored = await storage.save_upload(
        db, file,
        bucket_name=storage.DOCUMENTS_BUCKET,
        metadata={"document_id": document_id, "client_id": doc["client_id"],
                  "request_id": doc.get("request_id"), "case_id": doc.get("case_id")},
    )
    file_meta = {**stored, "uploaded_at": now, "uploaded_by": user.id}
    consultant_id = doc.get("consultant_id") or await resolve_consultant_id(
        db, request_id=doc.get("request_id"), case_id=doc.get("case_id"),
        client_id=doc.get("client_id"))

    # Client upload: Update status to WITH_CONSULTANT (Under Review)
    await db.documents.update_one(
        {"_id": oid(document_id)},
        {"$set": {"file": file_meta,
                  "status": DocumentStatus.WITH_CONSULTANT.value,
                  "consultant_id": consultant_id,
                  # A stale verdict from the file this one replaces would be
                  # read as a verdict on the new one.
                  "ai_analysis": {"status": "analysing", "confidence": 0,
                                  "recommendation": "manual_review",
                                  "extracted_fields": {}, "issues": [],
                                  "summary": "Reading the document..."},
                  "consultant_feedback": None, "updated_at": now}},
    )

    # What the endpoint has always advertised, and never actually did: every
    # uploaded document is read before a consultant opens it. In the background
    # on purpose - see `_analyze_later`.
    asyncio.create_task(_analyze_later(db, document_id, doc, file_meta))

    # The client answered it - nothing left on the worklist to chase.
    await deadlines.clear(db, kind="document_request", source_collection="documents",
                          source_id=document_id)

    # One rule for what a request's status means, shared with the review path
    # below. This used to have its own: it only moved off "waiting for client"
    # once *every* requested document had been uploaded, so a request with
    # three files sitting unread on the consultant's desk and ten still to come
    # stayed filed under "Awaiting client" and counted as nothing to review -
    # the queue header said "0 to review" while the card said "3 awaiting
    # review", because the two were counting different things.
    await _sync_request_status(db, doc)

    await notify(db, user_ids=[consultant_id], type=NotificationType.DOCUMENT_UPLOADED,
                 title_key="notify.document_uploaded",
                 params={"person": user.raw.get("full_name") or "",
                         "document": doc["name"]},
                 body=f"Document uploaded: {file.filename or doc['name']}",
                 data={"document_id": document_id, "request_id": doc.get("request_id")})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="uploaded", subject=file.filename or doc["name"],
                       request_id=doc.get("request_id"), case_id=doc.get("case_id"))

    updated_doc = serialize(await _get(db, document_id))
    # The confirmation sheet the app shows after an upload. Codes and data
    # only: the file name is data, everything a person reads is the app's.
    updated_doc["popup_modal"] = {
        "key": "upload_document",
        "document_name": doc["name"],
        "file_name": file.filename or doc["name"],
        "status": "ready",
        "cta": "submit_to_consultant",
    }
    return updated_doc



async def _consultant_for(db, doc: Dict[str, Any]) -> Optional[str]:
    if doc.get("request_id"):
        req = await db.requests.find_one({"_id": oid(doc["request_id"])})
        if req:
            return req.get("consultant_id")
    if doc.get("case_id"):
        case = await db.cases.find_one({"_id": oid(doc["case_id"])})
        if case:
            return case.get("consultant_id")
    owner = await db.users.find_one({"role": Role.CONSULTANT_OWNER.value})
    return str(owner["_id"]) if owner else None


async def approve(db, user: CurrentUser, document_id: str) -> Dict[str, Any]:
    doc = await _get(db, document_id)
    if user.role == Role.PARTNER:
        # A task on this document's case is a handover of the work on it, so it
        # carries the same authority the consultant had: read, approve, return
        # and re-read. Without the case id only a whole-client handover counted.
        await assert_client_access(db, user, doc["client_id"],
                                   case_id=doc.get("case_id"))
    if not doc.get("file"):
        raise BadRequest("Nothing has been uploaded for this document yet")
    now = utcnow()
    await db.documents.update_one(
        {"_id": oid(document_id)},
        {"$set": {"status": DocumentStatus.APPROVED.value, "approved_by": user.id,
                  "approved_at": now, "consultant_feedback": None, "updated_at": now}},
    )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.DOCUMENT_APPROVED,
                 title_key="notify.document_approved",
                 body_key="notify.document_approved.body",
                 params={"document": doc["name"]},
                 # Enough to open something: the app has no screen for a
                 # document on its own, so a bare document_id is a dead tap.
                 data={"document_id": document_id,
                       "request_id": doc.get("request_id"),
                       "case_id": doc.get("case_id")})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="approved", subject=doc["name"],
                       request_id=doc.get("request_id"), case_id=doc.get("case_id"))
    await _sync_request_status(db, doc)
    return serialize(await _get(db, document_id))


async def reject(db, user: CurrentUser, document_id: str, feedback: str) -> Dict[str, Any]:
    doc = await _get(db, document_id)
    if user.role == Role.PARTNER:
        # A task on this document's case is a handover of the work on it, so it
        # carries the same authority the consultant had: read, approve, return
        # and re-read. Without the case id only a whole-client handover counted.
        await assert_client_access(db, user, doc["client_id"],
                                   case_id=doc.get("case_id"))
    now = utcnow()
    await db.documents.update_one(
        {"_id": oid(document_id)},
        {"$set": {"status": DocumentStatus.NEEDS_REUPLOAD.value,
                  "consultant_feedback": feedback, "rejected_by": user.id,
                  "rejected_at": now, "updated_at": now},
         # `consultant_feedback` is kept for whatever already reads it as "the
         # current note", but it is overwritten every rejection - a second
         # re-upload round loses what was said about the first. `comments`
         # does not: every round is its own entry, dated and attributed, so a
         # client on their third re-upload can still see what changed each
         # time rather than just what is being asked of them now.
         "$push": {"comments": {"author_id": user.id,
                                "author_name": user.raw.get("full_name"),
                                "text": feedback, "kind": "rejection",
                                "created_at": now}}},
    )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.DOCUMENT_REJECTED,
                 title_key="notify.document_rejected",
                 params={"document": doc["name"]}, body=feedback,
                 data={"document_id": document_id,
                       "request_id": doc.get("request_id"),
                       "case_id": doc.get("case_id")})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="returned to the client", subject=doc["name"],
                       request_id=doc.get("request_id"), case_id=doc.get("case_id"))
    await _sync_request_status(db, doc)
    return serialize(await _get(db, document_id))


async def comment(db, user: CurrentUser, document_id: str, text: str) -> Dict[str, Any]:
    await _get(db, document_id)
    entry = {"author_id": user.id, "author_name": user.raw.get("full_name"),
             "text": text, "created_at": utcnow()}
    await db.documents.update_one({"_id": oid(document_id)},
                                  {"$push": {"comments": entry},
                                   "$set": {"updated_at": utcnow()}})
    return serialize(await _get(db, document_id))


async def reanalyze(db, user: CurrentUser, document_id: str) -> Dict[str, Any]:
    doc = await _get(db, document_id)
    if user.role == Role.PARTNER:
        # A task on this document's case is a handover of the work on it, so it
        # carries the same authority the consultant had: read, approve, return
        # and re-read. Without the case id only a whole-client handover counted.
        await assert_client_access(db, user, doc["client_id"],
                                   case_id=doc.get("case_id"))
    if not doc.get("file"):
        raise BadRequest("Nothing has been uploaded for this document yet")
    # Same read as an upload does, but awaited: a consultant who pressed the
    # button is waiting for the answer on screen.
    analysis = await _run_analysis(db, document_id, doc, doc["file"])
    await db.documents.update_one({"_id": oid(document_id)},
                                  {"$set": {"updated_at": utcnow()}})
    return analysis


async def _sync_request_status(db, doc: Dict[str, Any]) -> None:
    rid = doc.get("request_id")
    if not rid:
        return
    total = await db.documents.count_documents({"request_id": rid})
    approved = await db.documents.count_documents(
        {"request_id": rid, "status": DocumentStatus.APPROVED.value})
    awaiting = await db.documents.count_documents(
        {"request_id": rid, "status": DocumentStatus.WITH_CONSULTANT.value})
    if total and approved == total:
        new = RequestStatus.UNDER_REVIEW.value
    elif awaiting:
        new = RequestStatus.DOCUMENTS_RECEIVED.value
    else:
        new = RequestStatus.WAITING_FOR_CLIENT.value
    await db.requests.update_one({"_id": oid(rid)},
                                 {"$set": {"status": new, "updated_at": utcnow()}})


async def delete_document(db, user: CurrentUser, document_id: str) -> Dict[str, Any]:
    """Withdraw a document requirement, and the file behind it if there is one.

    Consultants only. A client must never be able to remove a requirement they
    have been asked to satisfy, and a partner works to a brief rather than
    setting it - both would turn "I have not uploaded it" into "it was never
    asked for".

    An approved document is refused. It is evidence a decision was taken on,
    and deleting it would quietly rewrite the case's history - and its progress,
    which is now counted from these very records. Reject it first if it really
    has to go.
    """
    doc = await _get(db, document_id)

    if user.role not in CONSULTANT_ROLES:
        raise Forbidden("Only a consultant can remove a document request")

    owner = await _consultant_for(db, doc)
    if user.role != Role.CONSULTANT_OWNER and owner and owner != user.id:
        raise Forbidden("This document belongs to another consultant's client")

    if doc["status"] == DocumentStatus.APPROVED.value:
        raise BadRequest(
            "This document is already approved. Reject it first if it needs to be removed."
        )

    # The blob outlives the record unless it is dropped here, and nothing else
    # refers to it afterwards - GridFS would keep it for the life of the tenant.
    stored = (doc.get("file") or {}).get("file_id")
    if stored:
        await storage.delete_file(db, stored, storage.DOCUMENTS_BUCKET)

    await db.documents.delete_one({"_id": oid(document_id)})
    # The requirement itself is gone, so whatever it put on the worklist -
    # the upload deadline, any expiry read off a file already attached - goes
    # with it.
    await deadlines.clear_source(db, source_collection="documents", source_id=document_id)

    if doc.get("file"):
        # Only worth telling the client when they had actually done the work.
        await notify(
            db, user_ids=[doc["client_id"]],
            type=NotificationType.DOCUMENT_REJECTED,
            title_key="notify.document_withdrawn",
            params={"document": doc["name"]},
            body="Your consultant has withdrawn this request.",
            data={"request_id": doc.get("request_id"), "case_id": doc.get("case_id")},
        )

    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="removed the document request", subject=doc["name"],
                       request_id=doc.get("request_id"), case_id=doc.get("case_id"))

    # The request's status is derived from its documents, so it has to be
    # recomputed against what is left rather than what was there.
    await _sync_request_status(db, doc)
    return {"id": document_id, "deleted": True, "name": doc["name"]}
