from datetime import timedelta
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser
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
from app.services.openai_service import suggest_required_documents
from app.services.pagination import paginate


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
        "status": RequestStatus.NEW.value,
        "client_id": user.id,
        "client_name": user.raw.get("full_name"),
        "consultant_id": consultant_id,
        "attached_files": [],
        "case_id": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.requests.insert_one(doc)
    request_id = str(result.inserted_id)

    await notify(db, user_ids=[consultant_id], type=NotificationType.REQUEST_SUBMITTED,
                 title=f"{doc['client_name']} submitted a request",
                 body=f"{doc['visa_type']} · {doc['reference']}",
                 data={"request_id": request_id})
    await log_activity(db, actor_id=user.id, actor_name=doc["client_name"] or "Client",
                       action="submitted a request", subject=doc["reference"],
                       request_id=request_id)
    return serialize({**doc, "_id": result.inserted_id})


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


async def counts(db, user: CurrentUser) -> Dict[str, int]:
    base = _scope(user)
    out = {}
    for st in RequestStatus:
        out[st.value] = await db.requests.count_documents({**base, "status": st.value})
    return out


async def get_request(db, user: CurrentUser, request_id: str) -> Dict[str, Any]:
    doc = await _get(db, request_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This request is not yours")
    out = serialize(doc)
    await _attach_document_counts(db, out)
    if user.role == Role.CLIENT:
        out.pop("review_notes", None)   # private working notes stay with the consultant
    out["documents"] = [
        serialize(d) async for d in db.documents.find({"request_id": request_id}).sort("created_at", 1)
    ]
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
    return serialize(await _get(db, request_id))


async def request_documents(db, user: CurrentUser, request_id: str, data) -> Dict[str, Any]:
    """Consultant decides exactly which documents the client must provide."""
    doc = await _get(db, request_id)
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
                 title="Your consultant requested documents",
                 body=data.message or f"{len(inserts)} document(s) needed for {doc['reference']}",
                 data={"request_id": request_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="requested documents for", subject=doc["reference"],
                       request_id=request_id)
    return {"detail": f"{len(inserts)} document(s) requested", "request_id": request_id}


async def suggest_documents(db, request_id: str) -> List[Dict[str, Any]]:
    doc = await _get(db, request_id)
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


async def complete_consultation(db, user: CurrentUser, request_id: str,
                                case_type: Optional[str] = None,
                                deadline=None) -> Dict[str, Any]:
    """Unlocked only when every required document is approved; opens the case."""
    doc = await _get(db, request_id)
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
        {"$set": {"status": RequestStatus.COMPLETED.value, "case_id": case_id, "updated_at": now}},
    )
    await db.documents.update_many({"request_id": request_id}, {"$set": {"case_id": case_id}})
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.CASE_STAGE_CHANGED,
                 title="Your case has been opened",
                 body=f"{case['reference']} · {case['case_type']}",
                 data={"case_id": case_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="completed the consultation for", subject=doc["reference"],
                       request_id=request_id, case_id=case_id)
    return serialize({**case, "_id": oid(case_id)})
