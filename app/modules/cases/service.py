from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser
from app.core.enums import (
    CASE_STAGE_ORDER,
    CaseStage,
    NotificationType,
    Role,
    TaskAssigneeType,
    TaskStatus,
)
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import build_reference, oid, serialize, utcnow
from app.db.indexes import next_sequence
from app.schemas.common import PageParams
from app.services.events import log_activity, notify
from app.services.openai_service import case_guidance
from app.services.ownership import attach_consultant
from app.services.pagination import paginate


def _progress(stage: CaseStage) -> int:
    idx = CASE_STAGE_ORDER.index(stage)
    return round((idx / (len(CASE_STAGE_ORDER) - 1)) * 100)


async def _get(db, case_id: str) -> Dict[str, Any]:
    doc = await db.cases.find_one({"_id": oid(case_id)})
    if not doc:
        raise NotFound("Case not found")
    return doc


async def create_case(db, user: CurrentUser, data) -> Dict[str, Any]:
    seq = await next_sequence(db, "case", start=80)
    client = await db.users.find_one({"_id": oid(data.client_id)})
    if not client:
        raise NotFound("Client not found")
    now = utcnow()
    doc = {
        "reference": build_reference("CAS", seq),
        "request_id": data.request_id,
        "client_id": data.client_id,
        "client_name": client.get("full_name"),
        # The client's owning consultant keeps the case unless one is acting directly.
        "consultant_id": client.get("consultant_id") or user.id,
        "case_type": data.case_type,
        "destination_country": data.destination_country,
        "stage": CaseStage.NEW_REQUEST.value,
        "progress": 0,
        "deadline": data.deadline,
        "timeline": [{"stage": CaseStage.NEW_REQUEST.value, "at": now, "by": user.id}],
        "ai_guidance": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.cases.insert_one(doc)
    return await attach_consultant(db, serialize({**doc, "_id": result.inserted_id}))


async def list_cases(db, user: CurrentUser, params: PageParams,
                     stage: Optional[CaseStage] = None,
                     search: Optional[str] = None,
                     consultant_id: Optional[str] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if consultant_id:
        query["consultant_id"] = consultant_id
    if user.role == Role.CLIENT:
        query["client_id"] = user.id
    elif user.role == Role.PARTNER:
        case_ids = await db.tasks.distinct("case_id", {"assignee_id": user.id})
        query["_id"] = {"$in": [oid(c) for c in case_ids if c]}
    if stage:
        query["stage"] = stage.value
    if search:
        query["$or"] = [
            {"reference": {"$regex": search, "$options": "i"}},
            {"client_name": {"$regex": search, "$options": "i"}},
            {"case_type": {"$regex": search, "$options": "i"}},
        ]
    return await paginate(db, "cases", query, params,
                          sort=[("deadline", 1), ("created_at", -1)])


async def stage_counts(db, user: CurrentUser,
                       consultant_id: Optional[str] = None) -> Dict[str, int]:
    base: Dict[str, Any] = {"client_id": user.id} if user.role == Role.CLIENT else {}
    if consultant_id:
        base["consultant_id"] = consultant_id
    out = {"all": await db.cases.count_documents(base)}
    for st in CASE_STAGE_ORDER:
        out[st.value] = await db.cases.count_documents({**base, "stage": st.value})
    return out


async def get_case(db, user: CurrentUser, case_id: str) -> Dict[str, Any]:
    doc = await _get(db, case_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This case is not yours")
    out = await attach_consultant(db, serialize(doc))
    out["documents"] = [
        serialize(d) async for d in db.documents.find({"case_id": case_id}).sort("created_at", 1)
    ]
    out["client_tasks"] = [
        serialize(t) async for t in db.tasks.find(
            {"case_id": case_id, "assignee_type": TaskAssigneeType.CLIENT.value}
        ).sort("created_at", 1)
    ]
    out["partner_tasks"] = [
        serialize(t) async for t in db.tasks.find(
            {"case_id": case_id, "assignee_type": TaskAssigneeType.PARTNER.value}
        ).sort("created_at", 1)
    ]
    out["stage_order"] = [st.value for st in CASE_STAGE_ORDER]
    return out


async def update_case(db, case_id: str, data) -> Dict[str, Any]:
    await _get(db, case_id)
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    payload["updated_at"] = utcnow()
    await db.cases.update_one({"_id": oid(case_id)}, {"$set": payload})
    return await attach_consultant(db, serialize(await _get(db, case_id)))


async def advance_stage(db, user: CurrentUser, case_id: str, data) -> Dict[str, Any]:
    doc = await _get(db, case_id)
    current = CaseStage(doc["stage"])
    if data.stage:
        target = data.stage
    else:
        idx = CASE_STAGE_ORDER.index(current)
        if idx >= len(CASE_STAGE_ORDER) - 1:
            raise BadRequest("This case is already completed")
        target = CASE_STAGE_ORDER[idx + 1]

    now = utcnow()
    await db.cases.update_one(
        {"_id": oid(case_id)},
        {"$set": {"stage": target.value, "progress": _progress(target), "updated_at": now},
         "$push": {"timeline": {"stage": target.value, "at": now,
                                "by": user.id, "note": data.note}}},
    )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.CASE_STAGE_CHANGED,
                 title=f"{doc['reference']} moved to {target.value.replace('_', ' ')}",
                 body=data.note or "", data={"case_id": case_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action=f"advanced to {target.value}", subject=doc["reference"],
                       case_id=case_id)
    return await attach_consultant(db, serialize(await _get(db, case_id)))


async def generate_guidance(db, case_id: str, create_tasks: bool = True) -> Dict[str, Any]:
    doc = await _get(db, case_id)
    documents = [
        {"name": d["name"], "status": d["status"], "category": d.get("category")}
        async for d in db.documents.find({"case_id": case_id})
    ]
    guidance = await case_guidance(case_context={
        "reference": doc["reference"],
        "case_type": doc["case_type"],
        "destination_country": doc.get("destination_country"),
        "stage": doc["stage"],
        "deadline": doc.get("deadline"),
        "documents": documents,
    })
    await db.cases.update_one(
        {"_id": oid(case_id)},
        {"$set": {"ai_guidance": guidance, "guidance_generated_at": utcnow(),
                  "updated_at": utcnow()}},
    )

    if create_tasks and guidance.get("client_tasks"):
        from datetime import timedelta
        now = utcnow()
        rows = []
        for t in guidance["client_tasks"]:
            rows.append({
                "title": t.get("title", "Client task"),
                "description": t.get("description"),
                "case_id": case_id,
                "case_reference": doc["reference"],
                "client_id": doc["client_id"],
                "consultant_id": doc.get("consultant_id"),
                "assignee_type": TaskAssigneeType.CLIENT.value,
                "assignee_id": doc["client_id"],
                "assignee_name": doc.get("client_name"),
                "status": TaskStatus.PENDING.value,
                "auto_created": True,
                "due_date": now + timedelta(days=int(t.get("due_in_days", 7) or 7)),
                "created_at": now,
                "updated_at": now,
            })
        if rows:
            await db.tasks.insert_many(rows)
            await notify(db, user_ids=[doc["client_id"]],
                         type=NotificationType.TASK_ASSIGNED,
                         title=f"{len(rows)} new task(s) on {doc['reference']}",
                         data={"case_id": case_id})
    return guidance


async def timeline(db, case_id: str) -> List[Dict[str, Any]]:
    doc = await _get(db, case_id)
    done = {entry["stage"]: entry for entry in doc.get("timeline", [])}
    return [
        {"stage": st.value,
         "label": st.value.replace("_", " ").title(),
         "completed": st.value in done,
         "at": done.get(st.value, {}).get("at"),
         "current": doc["stage"] == st.value}
        for st in CASE_STAGE_ORDER
    ]
