from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser
from app.core.i18n import DEFAULT_LANGUAGE, translate
from app.core.enums import (
    CONSULTANT_ROLES,
    CaseStage,
    client_status,
    DocumentStatus,
    NotificationType,
    RequestStatus,
    Role,
)
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import build_reference, oid, serialize, utcnow
from app.db.indexes import next_sequence
from app.modules.deadlines import service as deadlines
from app.schemas.common import PageParams
from app.services.events import log_activity, notify
from app.services.ai_service import suggest_required_documents
from app.services.ownership import (assert_request_access, assigned_client_ids,
                                    delegated_case_ids)
from app.services.pagination import paginate
from app.services import storage


async def _get(db, request_id: str) -> Dict[str, Any]:
    doc = await db.requests.find_one({"_id": oid(request_id)})
    if not doc:
        raise NotFound("Request not found")
    return doc


def _scope(user: CurrentUser) -> Dict[str, Any]:
    if user.role == Role.CLIENT:
        return {"client_id": user.id}
    return {}


async def _partner_scope(db, user: CurrentUser) -> Dict[str, Any]:
    """The requests a partner is allowed to see at all.

    The same two rules the rest of the platform uses for a partner - the whole
    client was handed to them, or they hold a task on the case behind this
    request - written as a query so the list can be filtered in the database
    rather than after the fact. A request still open has no `case_id` of its
    own, so the case that points back at it is matched too.
    """
    client_ids = await assigned_client_ids(db, user.id)
    case_ids = await delegated_case_ids(db, user.id)
    request_ids: List[str] = []
    if case_ids:
        cursor = db.cases.find({"_id": {"$in": [oid(c) for c in case_ids]}},
                               {"request_id": 1})
        async for row in cursor:
            if row.get("request_id"):
                request_ids.append(row["request_id"])
    return {"$or": [
        {"client_id": {"$in": client_ids}},
        {"case_id": {"$in": case_ids}},
        {"_id": {"$in": [oid(r) for r in request_ids]}},
    ]}


async def _client_for(db, client_id: str) -> Dict[str, Any]:
    client = await db.users.find_one({"_id": oid(client_id),
                                      "role": Role.CLIENT.value})
    if not client:
        raise NotFound("Client not found")
    return client


async def create_request(db, user: CurrentUser, data) -> Dict[str, Any]:
    """Open a request. The consultant does this; the client never does.

    It used to work both ways, with a client's request waiting at
    PENDING_APPROVAL for the consultant to take it. The product review settled
    the question differently: the consultant decides what procedure a client
    follows, so a client naming their own is not a request awaiting approval - it
    is a decision they were never the one to make.

    PENDING_APPROVAL and `approve_request` stay, for rows raised before this
    changed. Nothing creates a new one.
    """
    if user.role == Role.CLIENT:
        raise Forbidden(
            "Your consultant opens requests for you. Message them with what you need."
        )
    if user.role in CONSULTANT_ROLES:
        if not data.client_id:
            raise BadRequest("Name the client this request is for")
        client = await _client_for(db, data.client_id)
        client_id = str(client["_id"])
        # A consultant may open one for a colleague's client; the field says
        # whose caseload it lands on and defaults to their own.
        consultant_id = data.consultant_id or user.id
        raised_by_consultant = True
    else:
        raise Forbidden("Only a consultant can open a request")

    if not consultant_id:
        owner = await db.users.find_one({"role": Role.CONSULTANT_OWNER.value})
        consultant_id = str(owner["_id"]) if owner else None

    # What the consultant assigned. A copy of the checklist travels with the
    # case when one is opened, not with the request - see `catalog.snapshot_for_case`.
    procedure = None
    if getattr(data, "procedure_id", None):
        from app.modules.catalog import service as catalog

        procedure = await catalog.snapshot_for_case(db, data.procedure_id)

    seq = await next_sequence(db, "request", start=100)
    now = utcnow()
    doc = {
        "reference": build_reference("REQ", seq),
        "process_area": (procedure or {}).get("process_area")
                        or getattr(data, "process_area", None),
        "procedure_id": (procedure or {}).get("procedure_id"),
        "procedure_name": (procedure or {}).get("procedure_name"),
        # The label everything falls back to when no procedure is assigned. Kept
        # under its old name so nothing that reads a request has to change.
        "visa_type": (data.visa_type or (procedure or {}).get("procedure_name")
                      or "Consultation"),
        "destination_country": data.destination_country,
        "origin_country": client.get("country_of_residence"),
        "purpose": data.purpose,
        "additional_information": data.additional_information,
        "client_notes": data.client_notes,
        "preferred_appointment": data.preferred_appointment,
        "review_notes": None,
        # A draft is a NEW request flagged as one — "draft" is not a status
        # the rest of the API (or its enum) knows about.
        # Straight into the queue: a consultant opening a request has already
        # decided the work exists.
        "status": RequestStatus.NEW.value,
        "raised_by": user.id,
        "raised_by_consultant": raised_by_consultant,
        "approved_by": user.id if raised_by_consultant else None,
        "approved_at": now if raised_by_consultant else None,
        "decline_reason": None,
        "client_id": client_id,
        "client_name": client.get("full_name"),
        "consultant_id": consultant_id,
        "attached_files": data.attached_files or [],
        "is_draft": data.is_draft,
        "case_id": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.requests.insert_one(doc)
    request_id = str(result.inserted_id)

    if not data.is_draft:
        # The client did not ask for this, so they are the one who needs telling.
        await notify(db, user_ids=[client_id],
                     type=NotificationType.REQUEST_SUBMITTED,
                     title_key="notify.request_opened_for_you",
                     params={"consultant": user.raw.get("full_name") or ""},
                     body=f"{doc['visa_type']} · {doc['reference']}",
                     data={"request_id": request_id})
        await log_activity(db, actor_id=user.id,
                           actor_name=user.raw.get("full_name") or "",
                           action="opened a request",
                           subject=doc["reference"], request_id=request_id)
    return serialize({**doc, "_id": result.inserted_id})


#: The statuses a client may withdraw their own request from.
#:
#: Everything else means a consultant has taken it on, and from that point the
#: request is not only the client's: it carries a checklist somebody wrote, files
#: somebody reviewed, and possibly a case built on top. Letting it be deleted
#: there does not undo the work, it orphans it - so those are withdrawn by
#: asking, which is what Messages is for.
CLIENT_DELETABLE_STATUSES = {
    RequestStatus.PENDING_APPROVAL.value,
    RequestStatus.DECLINED.value,
}


async def delete_request(db, user: CurrentUser, request_id: str) -> Dict[str, Any]:
    """Withdraw a request, and anything that only existed because of it.

    A client may remove one they raised while it is still theirs alone - waiting
    to be picked up, turned down, or never sent. A consultant may remove any
    request in their workspace, because they are the one who owns the queue.

    Deleting takes the request's documents with it, blobs included: a document
    row whose request is gone is invisible in every screen that lists documents
    by request, and its file would sit in GridFS for the life of the tenant.
    """
    doc = await _get(db, request_id)

    if user.role == Role.CLIENT:
        if doc["client_id"] != user.id:
            raise Forbidden("This request is not yours")
        if doc.get("case_id"):
            raise BadRequest(
                "A case has already been opened from this request. "
                "Message your consultant to close it."
            )
        if not doc.get("is_draft") and doc["status"] not in CLIENT_DELETABLE_STATUSES:
            raise BadRequest(
                "Your consultant is already working on this request. "
                "Message them to withdraw it."
            )
    elif user.role in CONSULTANT_ROLES:
        await assert_request_access(db, user, doc)
    else:
        raise Forbidden("Only a consultant or the client can remove a request")

    removed_documents = 0
    async for document in db.documents.find({"request_id": request_id},
                                            {"file": 1}):
        stored = (document.get("file") or {}).get("file_id")
        if stored:
            await storage.delete_file(db, stored, storage.DOCUMENTS_BUCKET)
        removed_documents += 1
    await db.documents.delete_many({"request_id": request_id})

    # The request's own supporting material (added via POST .../attachments)
    # lives in the same bucket but never made it into `documents` - without
    # this it survives the request that owned it.
    for attachment in doc.get("attached_files") or []:
        if attachment.get("file_id"):
            await storage.delete_file(
                db, attachment["file_id"], attachment.get("bucket", storage.DOCUMENTS_BUCKET))

    await db.requests.delete_one({"_id": oid(request_id)})

    # Told only when somebody other than the owner of the queue did it - a
    # consultant deleting their own client's request should not ping themselves.
    if user.role == Role.CLIENT and doc.get("consultant_id"):
        await notify(db, user_ids=[doc["consultant_id"]],
                     type=NotificationType.REQUEST_SUBMITTED,
                     title_key="notify.request_withdrawn",
                     params={"client": doc.get("client_name") or ""},
                     body=f"{doc.get('visa_type', '')} · {doc.get('reference', '')}")
    elif user.role in CONSULTANT_ROLES and doc.get("client_id"):
        await notify(db, user_ids=[doc["client_id"]],
                     type=NotificationType.REQUEST_SUBMITTED,
                     title_key="notify.request_removed",
                     body=f"{doc.get('visa_type', '')} · {doc.get('reference', '')}")

    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name") or "",
                       action="withdrew a request", subject=doc.get("reference", ""))

    return {"id": request_id, "deleted": True,
            "reference": doc.get("reference", ""),
            "documents_removed": removed_documents}


async def approve_request(db, user: CurrentUser, request_id: str) -> Dict[str, Any]:
    """Take a client's request into the queue."""
    doc = await _get(db, request_id)
    await assert_request_access(db, user, doc)
    if doc["status"] != RequestStatus.PENDING_APPROVAL.value:
        raise BadRequest("This request is not waiting for approval")

    now = utcnow()
    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$set": {"status": RequestStatus.NEW.value, "approved_by": user.id,
                  "approved_at": now, "decline_reason": None, "updated_at": now}},
    )
    await notify(db, user_ids=[doc["client_id"]],
                 type=NotificationType.REQUEST_SUBMITTED,
                 title_key="notify.request_approved",
                 body=f"{doc['visa_type']} · {doc['reference']}",
                 data={"request_id": request_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name") or "",
                       action="approved a request", subject=doc["reference"],
                       request_id=request_id)
    return serialize(await _get(db, request_id))


async def decline_request(db, user: CurrentUser, request_id: str,
                          reason: str) -> Dict[str, Any]:
    """Turn a client's request down, with a reason they can read.

    The record is kept rather than deleted: the client asked for something and
    is owed an answer, and a request that simply vanishes reads as one that was
    lost.
    """
    doc = await _get(db, request_id)
    await assert_request_access(db, user, doc)
    if doc["status"] != RequestStatus.PENDING_APPROVAL.value:
        raise BadRequest("This request is not waiting for approval")

    now = utcnow()
    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$set": {"status": RequestStatus.DECLINED.value, "decline_reason": reason,
                  "declined_by": user.id, "declined_at": now, "updated_at": now}},
    )
    await notify(db, user_ids=[doc["client_id"]],
                 type=NotificationType.REQUEST_SUBMITTED,
                 title_key="notify.request_declined",
                 body=reason,
                 data={"request_id": request_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name") or "",
                       action="declined a request", subject=doc["reference"],
                       request_id=request_id)
    return serialize(await _get(db, request_id))


async def get_client_dashboard(db, user: CurrentUser,
                               lang: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    if user.role != Role.CLIENT:
        raise Forbidden("Only client users can access client dashboard")

    client_id = user.id
    client_name = user.raw.get("full_name", "Client").split(" ")[0]

    # Unread notifications count
    unread_notifications = await db.notifications.count_documents(
        {"user_id": client_id, "read": False}
    ) if "notifications" in await db.list_collection_names() else 0

    # My requests
    requests_cursor = db.requests.find({"client_id": client_id}).sort("created_at", -1)
    req_list = [serialize(r) async for r in requests_cursor]
    for r in req_list:
        await _attach_document_counts(db, r)

    # Active hero request: the newest one still being worked, falling back to
    # the newest overall once everything is closed.
    hero_req = next((r for r in req_list if r.get("status") != RequestStatus.COMPLETED.value), None)
    if hero_req is None:
        hero_req = req_list[0] if req_list else None

    active_hero = None
    action_next = None
    pending_documents = sum(r.get("documents_action_required", 0) for r in req_list)

    if hero_req:
        total = hero_req.get("documents_total", 0)
        approved = hero_req.get("documents_approved", 0)
        active_hero = {
            "request_id": hero_req["id"],
            "reference": hero_req["reference"],
            "visa_type": hero_req["visa_type"],
            "status": hero_req["status"],
            "overall_progress": int(approved / total * 100) if total > 0 else 0,
        }

        needed = hero_req.get("documents_action_required", 0)
        if needed > 0:
            action_next = {
                # `type` is the code and `count` the number. Building the
                # plural here gets English right and every other language
                # wrong - Spanish and Portuguese do not pluralise this way.
                "type": "upload_documents",
                "count": needed,
                "title": translate("action.upload_documents", lang, count=needed),
                "action_url": f"/documents?request_id={hero_req['id']}",
            }
        elif hero_req.get("status") == RequestStatus.COMPLETED.value:
            action_next = {
                "type": "view_outcome",
                "title": translate("action.view_outcome", lang),
                "action_url": f"/requests/{hero_req['id']}",
            }
        else:
            action_next = {
                "type": "wait",
                "title": translate("action.wait", lang),
                "action_url": None,
            }

    # Format list for home screen cards
    formatted_requests = []
    for r in req_list[:5]:
        formatted_requests.append({
            "id": r["id"],
            "reference": r["reference"],
            "visa_type": r["visa_type"],
            "visa_type_label": _visa_type_label(r["visa_type"], lang),
            "status": r["status"],
            # Both: the code for the app to branch on, the words for it to show.
            # A client that would rather do its own wording is never forced
            # through the catalogue.
            "status_label": translate(f"status.{r['status']}", lang),
            "client_status": client_status(r["status"]),
            "client_status_label": translate(
                f"status.{client_status(r['status'])}", lang),
            "destination_country": r.get("destination_country", ""),
            "created_at": r.get("created_at"),
            "purpose": r.get("purpose", ""),
            "is_draft": r.get("is_draft", False),
            "documents_total": r.get("documents_total", 0),
            "documents_approved": r.get("documents_approved", 0),
            "documents_awaiting_review": r.get("documents_awaiting_review", 0),
            "documents_action_required": r.get("documents_action_required", 0),
        })

    # Recent activity on this client's own requests, newest first.
    request_ids = [r["id"] for r in req_list]
    recent_activities = []
    act_cursor = db.activities.find(
        {"$or": [{"actor_id": client_id}, {"request_id": {"$in": request_ids}}]}
    ).sort("created_at", -1).limit(6)
    async for act in act_cursor:
        recent_activities.append(serialize(act))

    return {
        "client_name": client_name,
        "unread_notifications_count": unread_notifications,
        "active_hero_request": active_hero,
        "action_next_step": action_next,
        "my_requests": formatted_requests,
        "recent_activities": recent_activities,
        "pending_documents_count": pending_documents,
    }


async def get_client_categories(db, lang: str = DEFAULT_LANGUAGE) -> List[Dict[str, Any]]:
    """What this organization actually does, from its own catalogue.

    This used to be nine hard-coded visa types - the same nine for every
    customer, uneditable, and useless to a firm whose work is labour or tax. It
    is now the tenant's procedures, grouped by process area.

    Still on the client side of the API because the client's app labels their
    own case with it. Choosing from it is not theirs to do: the consultant
    assigns the procedure, and `POST /requests` refuses a client outright.
    """
    from app.modules.catalog import service as catalog

    areas = {a["key"]: a for a in await catalog.list_areas(db, lang=lang)}
    out = []
    for procedure in await catalog.list_procedures(db, lang=lang):
        area = areas.get(procedure.get("area_key")) or {}
        out.append({
            "id": procedure["id"],
            "name": procedure["name"],
            "description": procedure.get("description"),
            "process_area": procedure.get("area_key"),
            "process_area_name": area.get("name"),
            "icon": area.get("icon") or "document",
        })
    return out


async def list_requests(db, user: CurrentUser, params: PageParams,
                        status: Optional[RequestStatus] = None,
                        search: Optional[str] = None,
                        consultant_id: Optional[str] = None,
                        lang: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    query: Dict[str, Any] = _scope(user)
    # Collected rather than assigned straight onto `query`: the partner scope
    # and the search are both `$or`, and the second used to overwrite the first.
    clauses: List[Dict[str, Any]] = []
    if user.role == Role.PARTNER:
        clauses.append(await _partner_scope(db, user))
    if consultant_id:
        query["consultant_id"] = consultant_id
    if status:
        query["status"] = status.value
    if search:
        clauses.append({"$or": [
            {"reference": {"$regex": search, "$options": "i"}},
            {"client_name": {"$regex": search, "$options": "i"}},
            {"visa_type": {"$regex": search, "$options": "i"}},
            {"destination_country": {"$regex": search, "$options": "i"}},
        ]})
    if clauses:
        query["$and"] = clauses
    page = await paginate(db, "requests", query, params, sort=[("created_at", -1)])
    for item in page["items"]:
        await _attach_document_counts(db, item)
        _attach_client_status(item, lang)
        item["visa_type_label"] = _visa_type_label(item.get("visa_type"), lang)
    return page


def _visa_type_label(visa_type: Optional[str], lang: str) -> str:
    """`visa_type` in English translated, in most cases: it holds the name of
    the procedure a consultant chose, or the raw text they typed - their own
    wording, kept exactly as they wrote it (see `catalog/templates.py` for why
    that is deliberate). The one exception is "Consultation", which nobody
    typed - it is the value this module writes itself when a request carries
    neither a procedure nor a free-typed type - and being the platform's own
    word, it is the one translated here rather than shown as English on a
    Portuguese or Spanish screen.
    """
    if (visa_type or "").strip().lower() == "consultation":
        return translate("request.consultation_fallback", lang)
    return visa_type or ""


def _attach_client_status(item: Dict[str, Any], lang: str) -> None:
    """Adds the client's view of the status beside the internal one.

    Beside, not instead: the consultant's screens branch on `status` and filter
    the queue by it, and collapsing four of those into one would take that away.
    The client's app reads `client_status` and shows `client_status_label`.
    """
    code = client_status(item.get("status", ""))
    item["client_status"] = code
    item["client_status_label"] = translate(f"status.{code}", lang)


async def _attach_document_counts(db, item: Dict[str, Any]) -> None:
    rid = item["id"]
    item["documents_total"] = await db.documents.count_documents({"request_id": rid})
    item["documents_approved"] = await db.documents.count_documents(
        {"request_id": rid, "status": DocumentStatus.APPROVED.value}
    )
    item["documents_awaiting_review"] = await db.documents.count_documents(
        {"request_id": rid, "status": DocumentStatus.WITH_CONSULTANT.value}
    )
    item["documents_action_required"] = await db.documents.count_documents(
        {"request_id": rid,
         "status": {"$in": [DocumentStatus.UPLOAD_NEEDED.value, DocumentStatus.NEEDS_REUPLOAD.value]}}
    )


async def counts(db, user: CurrentUser) -> Dict[str, int]:
    base = _scope(user)
    out = {}
    for st in RequestStatus:
        out[st.value] = await db.requests.count_documents({**base, "status": st.value})
    return out


async def get_request(db, user: CurrentUser, request_id: str,
                      lang: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    doc = await _get(db, request_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This request is not yours")
    # A partner reaches a request only through work delegated to them.
    if user.role == Role.PARTNER:
        await assert_request_access(db, user, doc)
    out = serialize(doc)
    await _attach_document_counts(db, out)

    if user.role == Role.CLIENT:
        out.pop("review_notes", None)  # private working notes stay with the consultant

    # Step progress tracking (Image 1 & Image 2 top status banner)
    st = out.get("status", "new")
    total = out.get("documents_total", 0)
    approved = out.get("documents_approved", 0)
    under_review = out.get("documents_awaiting_review", 0)
    action_req = out.get("documents_action_required", 0)

    is_requested = total > 0
    is_reviewed = total > 0 and (approved + under_review) == total
    is_complete = st == RequestStatus.COMPLETED.value

    # Codes, not sentences. `key` is what the app looks up in its own strings
    # file, so the same response renders in Spanish, Portuguese or English
    # without the server knowing which.
    out["status_label"] = translate(f"status.{st}", lang)
    out["visa_type_label"] = _visa_type_label(out.get("visa_type"), lang)
    _attach_client_status(out, lang)
    out["status_steps"] = [
        {"key": key, "label": translate(f"step.{key}", lang), "completed": done}
        for key, done in (
            ("submitted", True),
            ("documents_requested", is_requested),
            ("documents_reviewed", is_reviewed),
            ("consultation_complete", is_complete),
        )
    ]

    # Document stats section (Image 2)
    progress_pct = int((approved / total * 100)) if total > 0 else 0
    out["progress_percentage"] = progress_pct
    out["document_stats"] = {
        "summary_text": (translate("documents.summary", lang,
                                   approved=approved, total=total)
                         if total > 0 else
                         translate("documents.none_requested", lang)),
        "total": total,
        "approved": approved,
        "under_review": under_review,
        "action_required": action_req,
        "overall_progress_percentage": progress_pct,
    }

    raw_docs = [
        serialize(d) async for d in db.documents.find({"request_id": request_id}).sort("created_at", 1)
    ]
    formatted_docs = []
    for d in raw_docs:
        d_st = d.get("status", "upload_needed")
        d_badge = "Approved" if d_st == DocumentStatus.APPROVED.value else (
            "Under Review" if d_st == DocumentStatus.WITH_CONSULTANT.value else "Action Required")
        formatted_docs.append({
            "id": d["id"],
            "name": d.get("name", ""),
            "status": d_st,
            "status_badge": d_badge,
            "description": d.get("why") or d.get(
                "description") or f"Your {d.get('name', 'document').lower()} requirement.",
            "due_date": d.get("due_date"),
            "submitted_at": d.get("updated_at") or d.get("created_at"),
            "allow_upload": d_st in [DocumentStatus.UPLOAD_NEEDED.value, DocumentStatus.NEEDS_REUPLOAD.value],
            "file": d.get("file"),
            "ai_analysis": d.get("ai_analysis"),
            # Why it came back. This view builds its own document dicts rather
            # than serialising the record, so anything left out here simply
            # does not reach the client - and a document returned with no
            # reason attached is one they cannot act on.
            "consultant_feedback": d.get("consultant_feedback"),
            "rejected_at": d.get("rejected_at"),
            "comments": d.get("comments", []),
        })
    out["documents"] = formatted_docs

    client = await db.users.find_one({"_id": oid(doc["client_id"])})
    if client:
        profile = {k: v for k, v in client.items() if k != "password_hash"}
        if user.role == Role.PARTNER:
            # Everything a partner needs reaches them through the consultant, so
            # they get the name for context and none of the ways to make contact.
            profile = {k: v for k, v in profile.items()
                       if k not in {"email", "mobile", "phone", "last_login_at"}}
        out["client_profile"] = serialize(profile)
    return out


async def update_request(db, user: CurrentUser, request_id: str, data) -> Dict[str, Any]:
    doc = await _get(db, request_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This request is not yours")
    if user.role == Role.PARTNER:
        await assert_request_access(db, user, doc)
    if user.role == Role.CLIENT and doc["status"] != RequestStatus.NEW.value:
        raise BadRequest("The consultant has started working on this request")
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    payload["updated_at"] = utcnow()
    await db.requests.update_one({"_id": oid(request_id)}, {"$set": payload})

    # Clearing the draft flag is how a draft is submitted, so it earns the same
    # notification and activity entry a fresh submission would.
    if doc.get("is_draft") and payload.get("is_draft") is False:
        await notify(db, user_ids=[doc.get("consultant_id")], type=NotificationType.REQUEST_SUBMITTED,
                     title_key="notify.request_submitted",
                     params={"client": doc.get("client_name") or ""},
                     body=f"{payload.get('visa_type') or doc['visa_type']} · {doc['reference']}",
                     data={"request_id": request_id})
        await log_activity(db, actor_id=doc["client_id"], actor_name=doc.get("client_name") or "Client",
                           action="submitted a request", subject=doc["reference"],
                           request_id=request_id)
    return serialize(await _get(db, request_id))


async def add_attachment(db, user: CurrentUser, request_id: str, file) -> Dict[str, Any]:
    """Supporting material the client sends along with the request itself.

    Kept apart from `documents`: those are the checklist the consultant asked
    for and each carries a review state; these are just context.
    """
    doc = await _get(db, request_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This request is not yours")

    meta = await storage.save_upload(
        db, file,
        metadata={"request_id": request_id, "uploaded_by": user.id},
    )
    meta["uploaded_at"] = utcnow()
    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$push": {"attached_files": meta}, "$set": {"updated_at": utcnow()}},
    )
    return meta


async def remove_attachment(db, user: CurrentUser, request_id: str, file_id: str) -> None:
    doc = await _get(db, request_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This request is not yours")

    meta = next((f for f in doc.get("attached_files", []) if f.get("file_id") == file_id), None)
    if not meta:
        raise NotFound("Attachment not found")
    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$pull": {"attached_files": {"file_id": file_id}}, "$set": {"updated_at": utcnow()}},
    )
    await storage.delete_file(db, file_id, meta.get("bucket", storage.DOCUMENTS_BUCKET))


async def get_attachment(db, user: CurrentUser, request_id: str, file_id: str) -> Dict[str, Any]:
    doc = await _get(db, request_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This request is not yours")
    meta = next((f for f in doc.get("attached_files", []) if f.get("file_id") == file_id), None)
    if not meta:
        raise NotFound("Attachment not found")
    return meta


async def request_documents(db, user: CurrentUser, request_id: str, data) -> Dict[str, Any]:
    """Decides exactly which documents the client must provide.

    Open to a partner the work was delegated to: asking for a missing page is
    part of reviewing the documents, and stopping to have the consultant relay
    it helps nobody.
    """
    doc = await _get(db, request_id)
    await assert_request_access(db, user, doc)
    if doc["status"] == RequestStatus.PENDING_APPROVAL.value:
        # Asking the client for papers is the work starting. Doing it before the
        # request is accepted tells them it was, and leaves the consultant with
        # a checklist against something they may yet decline.
        raise BadRequest("Approve this request before asking for documents")
    now = utcnow()
    inserts = []
    consultant_id = doc.get("consultant_id") or user.id
    for item in data.documents:
        inserts.append({
            "request_id": request_id,
            "case_id": doc.get("case_id"),
            "client_id": doc["client_id"],
            "consultant_id": consultant_id,
            "name": item.name,
            "category": item.category.value,
            "why": item.why,
            "is_required": item.is_required,
            "status": DocumentStatus.UPLOAD_NEEDED.value,
            # Only a date somebody actually chose. This used to invent
            # `now + 14 days` when the consultant left it blank, and the client
            # was then shown a hard "Vencimiento: 30 Sep" they were never given
            # - a deadline nobody set, that nobody was tracking, and that the
            # consultant could not see they had committed to.
            "due_date": item.due_date,
            "file": None,
            "ai_analysis": None,
            "consultant_feedback": None,
            "requested_by": user.id,
            "created_at": now,
            "updated_at": now,
        })
    result = await db.documents.insert_many(inserts)
    # Mirrored onto the worklist one row at a time, keyed by the document's own
    # id, so the calendar shows exactly the deadline this screen just set - and
    # nothing when the consultant left `due_date` blank, same as before.
    for item, document_id in zip(inserts, result.inserted_ids):
        await deadlines.upsert(
            db, kind="document_request", source_collection="documents",
            source_id=str(document_id), due_date=item["due_date"], title=item["name"],
            consultant_id=consultant_id, client_id=doc["client_id"],
            client_name=doc.get("client_name"), case_id=doc.get("case_id"),
        )
    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$set": {"status": RequestStatus.WAITING_FOR_CLIENT.value, "updated_at": now}},
    )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.DOCUMENTS_REQUESTED,
                 title_key="notify.documents_requested",
                 body=data.message or f"{len(inserts)} document(s) needed for {doc['reference']}",
                 data={"request_id": request_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="requested documents for", subject=doc["reference"],
                       request_id=request_id)
    return {"detail": f"{len(inserts)} document(s) requested", "request_id": request_id}


async def suggest_documents(db, user: CurrentUser, request_id: str) -> List[Dict[str, Any]]:
    doc = await _get(db, request_id)
    await assert_request_access(db, user, doc)
    return await suggest_required_documents(
        visa_type=doc["visa_type"],
        destination_country=doc["destination_country"],
        summary=f"{doc['purpose']} {doc.get('additional_information') or ''}",
    )


async def save_review_notes(db, user: CurrentUser, request_id: str, notes: str) -> Dict[str, Any]:
    await _get(db, request_id)
    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$set": {"review_notes": notes, "reviewed_by": user.id, "updated_at": utcnow()}},
    )
    return {"detail": "Notes saved"}


async def open_case(db, user: CurrentUser, request_id: str, data) -> Dict[str, Any]:
    """Turn a request into a case, without waiting on the document checklist.

    `complete_consultation` below is the *end* of a consultation: it requires
    every requested document to be approved, because it writes an outcome. That
    is the right gate for closing something, and the wrong one for starting it -
    the review found the only path to a case locked behind "approve all required
    documents", with no way to open the case in which those documents would be
    collected. The process could not move at all.

    So this is the beginning: the consultant accepts the work, the case exists,
    and the documents are gathered inside it.
    """
    doc = await _get(db, request_id)
    await assert_request_access(db, user, doc)
    if doc.get("case_id"):
        raise BadRequest("A case has already been opened from this request")
    if doc["status"] == RequestStatus.PENDING_APPROVAL.value:
        raise BadRequest("Approve this request before opening a case")
    if doc["status"] == RequestStatus.DECLINED.value:
        raise BadRequest("This request was declined")

    # Assigned here if the consultant is assigning one now, otherwise whatever
    # the request already carries. The client never chooses this.
    procedure_id = getattr(data, "procedure_id", None) or doc.get("procedure_id")
    procedure = {}
    if procedure_id:
        from app.modules.catalog import service as catalog

        procedure = await catalog.snapshot_for_case(db, procedure_id)

    # The stages and the deadline the procedure carries, decided in the one
    # place both ways of opening a case agree on.
    from app.modules.catalog import service as catalog_plan

    plan = catalog_plan.case_plan(procedure, getattr(data, "deadline", None))
    initial_stage = plan["stage"] or CaseStage.NEW_REQUEST.value

    seq = await next_sequence(db, "case", start=80)
    now = utcnow()
    case = {
        "reference": build_reference("CAS", seq),
        "request_id": request_id,
        "client_id": doc["client_id"],
        "client_name": doc.get("client_name"),
        "consultant_id": doc.get("consultant_id") or user.id,
        "process_area": (procedure.get("process_area")
                         or getattr(data, "process_area", None)
                         or doc.get("process_area")),
        "procedure_id": procedure.get("procedure_id"),
        "procedure_name": procedure.get("procedure_name"),
        # The checklist as it stood today. A copy, so editing the catalogue
        # later leaves this case assessed against what it was opened with.
        "required_documents": procedure.get("required_documents", []),
        "client_fields": procedure.get("client_fields", []),
        "workflow_stages": plan["workflow_stages"],
        "case_type": (getattr(data, "case_type", None)
                      or procedure.get("procedure_name")
                      or doc.get("procedure_name") or doc.get("visa_type")),
        "destination_country": doc.get("destination_country"),
        "stage": initial_stage,
        "progress": 0,
        "deadline": plan["deadline"],
        "timeline": [{"stage": initial_stage, "at": now, "by": user.id}],
        "ai_guidance": None,
        "created_at": now,
        "updated_at": now,
    }
    case_id = str((await db.cases.insert_one(case)).inserted_id)

    # Item C1: the case copies the procedure's checklist, but naming documents
    # is not asking for them. Until these rows exist the client has nothing to
    # upload and the guard that stops a case closing with documents still
    # outstanding counts zero. Same helper `POST /cases` uses.
    await catalog_plan.open_checklist_for_case(
        db, procedure=procedure, case_id=case_id, case_reference=case["reference"],
        client_id=doc["client_id"], client_name=doc.get("client_name"),
        consultant_id=case["consultant_id"], requested_by=user.id,
        request_id=request_id, deadline=plan["deadline"],
    )

    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$set": {"case_id": case_id, "status": RequestStatus.UNDER_REVIEW.value,
                  "updated_at": now}},
    )
    # Documents raised against the request belong to the case now, or anything
    # scoped by case - a delegated partner's access among it - cannot see them.
    await db.documents.update_many(
        {"request_id": request_id},
        {"$set": {"case_id": case_id, "updated_at": now}},
    )

    await notify(db, user_ids=[doc["client_id"]],
                 type=NotificationType.CASE_STAGE_CHANGED,
                 title_key="notify.case_opened",
                 body=f"{case['case_type'] or ''} · {case['reference']}",
                 data={"case_id": case_id, "request_id": request_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name") or "",
                       action="opened a case for", subject=case["reference"],
                       request_id=request_id, case_id=case_id)

    return serialize({**case, "_id": oid(case_id)})


async def complete_consultation(db, user: CurrentUser, request_id: str, data) -> Dict[str, Any]:
    """Unlocked only when every required document is approved; opens the case.

    A delegated partner may close it out too — they did the reviewing, so
    making them wait on the consultant for the last click is a stall rather
    than a safeguard.
    """
    doc = await _get(db, request_id)
    await assert_request_access(db, user, doc)
    if doc.get("case_id"):
        raise BadRequest("A case already exists for this request")

    total = await db.documents.count_documents({"request_id": request_id})
    approved = await db.documents.count_documents(
        {"request_id": request_id, "status": DocumentStatus.APPROVED.value}
    )
    if total == 0:
        raise BadRequest("Request the required documents before completing the consultation")
    if approved != total:
        raise BadRequest(f"Approve every required document first ({approved} of {total} approved)")

    # Item C1: this door used to ignore the procedure entirely - the case came
    # out with no checklist, no client fields and a fixed pair of generic
    # stages, which is why cases opened this way showed nothing for the client
    # to fill in. Same decision as the other two doors now.
    from app.modules.catalog import service as catalog_plan

    procedure_id = getattr(data, "procedure_id", None) or doc.get("procedure_id")
    procedure = {}
    if procedure_id:
        procedure = await catalog_plan.snapshot_for_case(db, procedure_id)
    plan = catalog_plan.case_plan(procedure, data.deadline)

    seq = await next_sequence(db, "case", start=80)
    now = utcnow()
    case_type = data.case_type
    deadline = plan["deadline"]
    # The consultation is over and every document it asked for is approved, so
    # the case starts at the stage after collection rather than at the top.
    stages = plan["workflow_stages"]
    initial_stage = (stages[1]["key"] if len(stages) > 1
                     else (stages[0]["key"] if stages
                           else CaseStage.CONSULTANT_REVIEW.value))
    case = {
        "reference": build_reference("CAS", seq),
        "request_id": request_id,
        "client_id": doc["client_id"],
        "client_name": doc.get("client_name"),
        "consultant_id": doc.get("consultant_id") or user.id,
        "process_area": (procedure.get("process_area") or doc.get("process_area")),
        "procedure_id": procedure.get("procedure_id"),
        "procedure_name": procedure.get("procedure_name"),
        "required_documents": procedure.get("required_documents", []),
        "client_fields": procedure.get("client_fields", []),
        "workflow_stages": stages,
        "case_type": (case_type or procedure.get("procedure_name")
                      or doc.get("procedure_name") or doc["visa_type"]),
        "destination_country": doc["destination_country"],
        "stage": initial_stage,
        "progress": 0,
        "deadline": deadline,
        "timeline": [
            {"stage": CaseStage.NEW_REQUEST.value, "at": doc["created_at"], "by": doc["client_id"]},
            {"stage": initial_stage, "at": now, "by": user.id},
        ],
        "ai_guidance": None,
        "created_at": now,
        "updated_at": now,
    }
    case_id = str((await db.cases.insert_one(case)).inserted_id)

    await db.requests.update_one(
        {"_id": oid(request_id)},
        {"$set": {
            "status": RequestStatus.COMPLETED.value,
            "case_id": case_id,
            "outcome": {**data.outcome.model_dump(), "completed_at": now},
            "updated_at": now,
        }},
    )
    await db.documents.update_many({"request_id": request_id}, {"$set": {"case_id": case_id}})
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.CASE_STAGE_CHANGED,
                 title_key="notify.case_opened",
                 body=f"{case['reference']} · {case['case_type']}",
                 data={"case_id": case_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="completed the consultation for", subject=doc["reference"],
                       request_id=request_id, case_id=case_id)
    return {**serialize({**case, "_id": oid(case_id)}), "case_id": case_id}


async def get_consultant_dashboard(db, user: CurrentUser,
                                   lang: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    cid = user.id if user.role in [Role.CONSULTANT, Role.CONSULTANT_OWNER] else None
    query = {"consultant_id": cid} if cid else {}

    unread = await db.notifications.count_documents({"user_id": user.id, "read": False})

    # Counts
    new_reqs = await db.requests.count_documents({**query, "status": RequestStatus.NEW.value})
    under_review = await db.requests.count_documents({**query, "status": RequestStatus.UNDER_REVIEW.value})
    waiting = await db.requests.count_documents({**query, "status": RequestStatus.WAITING_FOR_CLIENT.value})
    docs_received = await db.requests.count_documents({**query, "status": RequestStatus.DOCUMENTS_RECEIVED.value})

    todays_count = new_reqs + under_review
    to_review_count = docs_received + under_review
    waiting_count = waiting

    # The count travels as a number, not baked into a sentence. Spanish and
    # Portuguese pluralise differently from English, so only the app can write
    # this line correctly - and it cannot if it is handed a finished string.
    banner = {
        "key": "client_request_queue",
        "count": to_review_count,
        # Pluralised in the target language, not by bolting an "s" on.
        "subtitle": translate("banner.client_request_queue", lang,
                              count=to_review_count),
        "cta": "review_requests",
        "cta_label": translate("cta.review_requests", lang),
        "action_route": "/requests/queue",
    }

    # Open requests list
    cursor = db.requests.find(query).sort("updated_at", -1).limit(5)
    open_reqs = []
    async for req in cursor:
        client_name = req.get("client_name") or "Client"
        status_val = req.get("status", "new")
        open_reqs.append({
            "status_label": translate(f"status.{status_val}", lang),
            "id": str(req["_id"]),
            "reference": req.get("reference", ""),
            "client_name": client_name,
            "visa_type": req.get("visa_type", ""),
            "visa_type_label": _visa_type_label(req.get("visa_type"), lang),
            "status": status_val,
            "updated_at": req.get("updated_at"),
        })

    # Recent activities
    act_cursor = db.activities.find({}).sort("created_at", -1).limit(5)
    activities = []
    async for act in act_cursor:
        activities.append(serialize(act))

    return {
        "consultant_name": user.raw.get("full_name", "Consultant"),
        "unread_notifications_count": unread,
        "todays_count": todays_count,
        "to_review_count": to_review_count,
        "waiting_count": waiting_count,
        "client_request_queue_banner": banner,
        "open_requests": open_reqs,
        "recent_activity": activities,
    }
