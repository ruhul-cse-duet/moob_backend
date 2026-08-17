from typing import Any, Dict, Optional

from fastapi import UploadFile

from app.core.deps import CurrentUser
from app.core.enums import DocumentStatus, NotificationType, RequestStatus, Role
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import PageParams
from app.services import storage
from app.services.events import log_activity, notify
from app.services.openai_service import analyze_document
from app.services.ownership import assert_client_access, assigned_client_ids, resolve_consultant_id
from app.services.pagination import paginate


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
        client_ids = await assigned_client_ids(db, user.id)
        query["client_id"] = {"$in": client_ids}
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
        await assert_client_access(db, user, doc["client_id"])
    return serialize(doc)


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
                  "consultant_feedback": None, "updated_at": now}},
    )

    if doc.get("request_id"):
        pending = await db.documents.count_documents({
            "request_id": doc["request_id"],
            "status": {"$in": [DocumentStatus.UPLOAD_NEEDED.value,
                               DocumentStatus.NEEDS_REUPLOAD.value]},
        })
        new_status = (RequestStatus.DOCUMENTS_RECEIVED.value if pending == 0
                      else RequestStatus.WAITING_FOR_CLIENT.value)
        await db.requests.update_one({"_id": oid(doc["request_id"])},
                                     {"$set": {"status": new_status, "updated_at": now}})

    await notify(db, user_ids=[consultant_id], type=NotificationType.DOCUMENT_UPLOADED,
                 title=f"{user.raw.get('full_name')} uploaded {doc['name']}",
                 body=f"Document uploaded: {file.filename or doc['name']}",
                 data={"document_id": document_id, "request_id": doc.get("request_id")})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="uploaded", subject=file.filename or doc["name"],
                       request_id=doc.get("request_id"), case_id=doc.get("case_id"))

    updated_doc = serialize(await _get(db, document_id))
    # Pop-up modal details for mobile app ("Submit to Consultant")
    updated_doc["popup_modal"] = {
        "title": "Upload document",
        "document_name": doc["name"],
        "file_name": file.filename or doc["name"],
        "status_label": "ready",
        "cta_label": "Submit to Consultant",
        "message": f"{file.filename or doc['name']} is ready to be submitted to your consultant.",
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
        await assert_client_access(db, user, doc["client_id"])
    if not doc.get("file"):
        raise BadRequest("Nothing has been uploaded for this document yet")
    now = utcnow()
    await db.documents.update_one(
        {"_id": oid(document_id)},
        {"$set": {"status": DocumentStatus.APPROVED.value, "approved_by": user.id,
                  "approved_at": now, "consultant_feedback": None, "updated_at": now}},
    )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.DOCUMENT_APPROVED,
                 title=f"{doc['name']} approved", body="No further action needed.",
                 data={"document_id": document_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="approved", subject=doc["name"],
                       request_id=doc.get("request_id"), case_id=doc.get("case_id"))
    await _sync_request_status(db, doc)
    return serialize(await _get(db, document_id))


async def reject(db, user: CurrentUser, document_id: str, feedback: str) -> Dict[str, Any]:
    doc = await _get(db, document_id)
    if user.role == Role.PARTNER:
        await assert_client_access(db, user, doc["client_id"])
    now = utcnow()
    await db.documents.update_one(
        {"_id": oid(document_id)},
        {"$set": {"status": DocumentStatus.NEEDS_REUPLOAD.value,
                  "consultant_feedback": feedback, "rejected_by": user.id,
                  "rejected_at": now, "updated_at": now}},
    )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.DOCUMENT_REJECTED,
                 title=f"{doc['name']} needs a re-upload", body=feedback,
                 data={"document_id": document_id})
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
        await assert_client_access(db, user, doc["client_id"])
    if not doc.get("file"):
        raise BadRequest("Nothing has been uploaded for this document yet")
    analysis = await analyze_document(
        file_bytes=await storage.read_bytes(
            db, doc["file"]["file_id"], doc["file"].get("bucket", storage.DOCUMENTS_BUCKET)),
        mime_type=doc["file"].get("mime_type", "application/octet-stream"),
        document_name=doc["name"],
        context=doc.get("category", ""),
    )
    await db.documents.update_one({"_id": oid(document_id)},
                                  {"$set": {"ai_analysis": analysis, "updated_at": utcnow()}})
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
