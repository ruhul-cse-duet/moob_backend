from datetime import timedelta
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser
from app.core.i18n import DEFAULT_LANGUAGE, translate
from app.core.enums import (
    CaseStage,
    DocumentStatus,
    NotificationType,
    RequestStatus,
    Role,
)
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import build_reference, oid, serialize, utcnow
from app.db.indexes import next_sequence
from app.schemas.common import PageParams
from app.services.events import log_activity, notify
from app.services.ai_service import suggest_required_documents
from app.services.ownership import assert_request_access
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


async def create_request(db, user: CurrentUser, data) -> Dict[str, Any]:
    if user.role != Role.CLIENT:
        raise Forbidden("Only clients submit immigration requests")

    seq = await next_sequence(db, "request", start=100)
    consultant_id = data.consultant_id or user.raw.get("consultant_id")
    if not consultant_id:
        owner = await db.users.find_one({"role": Role.CONSULTANT_OWNER.value})
        consultant_id = str(owner["_id"]) if owner else None

    now = utcnow()
    doc = {
        "reference": build_reference("REQ", seq),
        "visa_type": data.visa_type,
        "destination_country": data.destination_country,
        "origin_country": user.raw.get("country_of_residence"),
        "purpose": data.purpose,
        "additional_information": data.additional_information,
        "client_notes": data.client_notes,
        "preferred_appointment": data.preferred_appointment,
        "review_notes": None,
        # A draft is a NEW request flagged as one — "draft" is not a status
        # the rest of the API (or its enum) knows about.
        "status": RequestStatus.NEW.value,
        "client_id": user.id,
        "client_name": user.raw.get("full_name"),
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
        await notify(db, user_ids=[consultant_id], type=NotificationType.REQUEST_SUBMITTED,
                     title_key="notify.request_submitted",
                     params={"client": doc["client_name"]},
                     body=f"{doc['visa_type']} · {doc['reference']}",
                     data={"request_id": request_id})
        await log_activity(db, actor_id=user.id, actor_name=doc["client_name"] or "Client",
                           action="submitted a request", subject=doc["reference"],
                           request_id=request_id)
    return serialize({**doc, "_id": result.inserted_id})


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
            "status": r["status"],
            # Both: the code for the app to branch on, the words for it to show.
            # A client that would rather do its own wording is never forced
            # through the catalogue.
            "status_label": translate(f"status.{r['status']}", lang),
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


async def get_client_categories(lang: str = DEFAULT_LANGUAGE) -> List[Dict[str, Any]]:
    """The visa categories offered on the new-request screen.

    Ids and icon tokens only. The name and blurb for each are copy, and a
    catalogue that ships its own English is a catalogue that is English in
    every locale - "Student Visa" has to become "Visa de Estudiante" somewhere,
    and the app is the only place that knows which language it is running in.
    """
    ids = ("student_visa", "work_permit", "family_reunification", "residency",
           "citizenship", "digital_nomad_visa", "business_visa", "investor_visa",
           "others")
    icons = {"student_visa": "academic_cap", "work_permit": "briefcase",
             "family_reunification": "heart", "residency": "home",
             "citizenship": "user_check", "digital_nomad_visa": "airplane",
             "business_visa": "building", "investor_visa": "bank",
             "others": "document"}
    return [{"id": vid, "icon": icons[vid], "name": translate(f"visa.{vid}", lang)}
            for vid in ids]



async def list_requests(db, user: CurrentUser, params: PageParams,
                        status: Optional[RequestStatus] = None,
                        search: Optional[str] = None,
                        consultant_id: Optional[str] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = _scope(user)
    if consultant_id:
        query["consultant_id"] = consultant_id
    if status:
        query["status"] = status.value
    if search:
        query["$or"] = [
            {"reference": {"$regex": search, "$options": "i"}},
            {"client_name": {"$regex": search, "$options": "i"}},
            {"visa_type": {"$regex": search, "$options": "i"}},
            {"destination_country": {"$regex": search, "$options": "i"}},
        ]
    page = await paginate(db, "requests", query, params, sort=[("created_at", -1)])
    for item in page["items"]:
        await _attach_document_counts(db, item)
    return page


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
        {"request_id": rid, "status": {"$in": [DocumentStatus.UPLOAD_NEEDED.value, DocumentStatus.NEEDS_REUPLOAD.value]}}
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
    out = serialize(doc)
    await _attach_document_counts(db, out)

    if user.role == Role.CLIENT:
        out.pop("review_notes", None)   # private working notes stay with the consultant

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
        d_badge = "Approved" if d_st == DocumentStatus.APPROVED.value else ("Under Review" if d_st == DocumentStatus.WITH_CONSULTANT.value else "Action Required")
        formatted_docs.append({
            "id": d["id"],
            "name": d.get("name", ""),
            "status": d_st,
            "status_badge": d_badge,
            "description": d.get("why") or d.get("description") or f"Your {d.get('name', 'document').lower()} requirement.",
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
        out["client_profile"] = serialize(
            {k: v for k, v in client.items() if k != "password_hash"}
        )
    return out



async def update_request(db, user: CurrentUser, request_id: str, data) -> Dict[str, Any]:
    doc = await _get(db, request_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This request is not yours")
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
            "due_date": item.due_date or (now + timedelta(days=14)),
            "file": None,
            "ai_analysis": None,
            "consultant_feedback": None,
            "requested_by": user.id,
            "created_at": now,
            "updated_at": now,
        })
    await db.documents.insert_many(inserts)
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

    seq = await next_sequence(db, "case", start=80)
    now = utcnow()
    case_type = data.case_type
    deadline = data.deadline
    case = {
        "reference": build_reference("CAS", seq),
        "request_id": request_id,
        "client_id": doc["client_id"],
        "client_name": doc.get("client_name"),
        "consultant_id": doc.get("consultant_id") or user.id,
        "case_type": case_type or doc["visa_type"],
        "destination_country": doc["destination_country"],
        "stage": CaseStage.CONSULTANT_REVIEW.value,
        "progress": 0,
        "deadline": deadline,
        "timeline": [
            {"stage": CaseStage.NEW_REQUEST.value, "at": doc["created_at"], "by": doc["client_id"]},
            {"stage": CaseStage.CONSULTANT_REVIEW.value, "at": now, "by": user.id},
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

