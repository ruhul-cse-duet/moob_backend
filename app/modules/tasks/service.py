from typing import Any, Dict, Optional

from fastapi import UploadFile

from app.core.deps import CurrentUser
from app.core.enums import NotificationType, Role, TaskAssigneeType, TaskStatus
from app.core.exceptions import Forbidden, NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import PageParams
from app.services import storage
from app.services.events import log_activity, notify
from app.services.ownership import link_partner_to_consultant
from app.services.pagination import paginate


async def _get(db, task_id: str) -> Dict[str, Any]:
    doc = await db.tasks.find_one({"_id": oid(task_id)})
    if not doc:
        raise NotFound("Task not found")
    return doc


async def create_task(db, user: CurrentUser, data) -> Dict[str, Any]:
    case = await db.cases.find_one({"_id": oid(data.case_id)})
    if not case:
        raise NotFound("Case not found")
    assignee = await db.users.find_one({"_id": oid(data.assignee_id)})
    if not assignee:
        raise NotFound("Assignee not found")

    now = utcnow()
    consultant_id = case.get("consultant_id") or user.id
    partner_id = data.assignee_id if data.assignee_type == TaskAssigneeType.PARTNER else None
    doc = {
        "title": data.title,
        "description": data.description,
        "case_id": data.case_id,
        "case_reference": case["reference"],
        "client_id": case["client_id"],
        "client_name": case.get("client_name"),
        "consultant_id": consultant_id,
        "partner_id": partner_id,
        "assignee_id": data.assignee_id,
        "assignee_name": assignee.get("full_name"),
        "assignee_type": data.assignee_type.value,
        "assigned_by": user.id,
        "status": TaskStatus.PENDING.value,
        "auto_created": False,
        "due_date": data.due_date,
        "reference_files": [rf.model_dump() for rf in data.reference_files],
        "deliverables": [],
        "delivery_notes": None,
        "created_at": now,
        "updated_at": now,
    }
    task_id = str((await db.tasks.insert_one(doc)).inserted_id)
    # A partner accumulates the consultants who delegate to them - many-to-many by design.
    if data.assignee_type == TaskAssigneeType.PARTNER:
        await link_partner_to_consultant(db, data.assignee_id, consultant_id)
    await notify(db, user_ids=[data.assignee_id], type=NotificationType.TASK_ASSIGNED,
                 title=data.title,
                 body=f"{case['reference']} · {case.get('client_name', '')}",
                 data={"task_id": task_id, "case_id": data.case_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="assigned a task", subject=data.title, case_id=data.case_id)
    return serialize({**doc, "_id": oid(task_id)})


async def list_tasks(db, user: CurrentUser, params: PageParams,
                     status: Optional[TaskStatus] = None,
                     case_id: Optional[str] = None,
                     assignee_type: Optional[TaskAssigneeType] = None,
                     mine: bool = False,
                     consultant_id: Optional[str] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if consultant_id:
        query["consultant_id"] = consultant_id
    if user.role in {Role.PARTNER, Role.CLIENT} or mine:
        query["assignee_id"] = user.id
    if status:
        query["status"] = status.value
    if case_id:
        query["case_id"] = case_id
    if assignee_type:
        query["assignee_type"] = assignee_type.value
    return await paginate(db, "tasks", query, params, sort=[("due_date", 1), ("created_at", -1)])


async def get_task(db, user: CurrentUser, task_id: str) -> Dict[str, Any]:
    doc = await _get(db, task_id)
    if user.role in {Role.PARTNER, Role.CLIENT} and doc["assignee_id"] != user.id:
        raise Forbidden("This task is not assigned to you")
    return serialize(doc)


async def update_task(db, user: CurrentUser, task_id: str, data) -> Dict[str, Any]:
    doc = await _get(db, task_id)
    if user.role in {Role.PARTNER, Role.CLIENT} and doc["assignee_id"] != user.id:
        raise Forbidden("This task is not assigned to you")
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    if "status" in payload:
        payload["status"] = payload["status"].value if hasattr(payload["status"], "value") else payload["status"]
    payload["updated_at"] = utcnow()
    await db.tasks.update_one({"_id": oid(task_id)}, {"$set": payload})
    return serialize(await _get(db, task_id))


async def set_status(db, user: CurrentUser, task_id: str, data) -> Dict[str, Any]:
    doc = await _get(db, task_id)
    if user.role in {Role.PARTNER, Role.CLIENT} and doc["assignee_id"] != user.id:
        raise Forbidden("This task is not assigned to you")
    now = utcnow()
    update = {"status": data.status.value, "updated_at": now}
    if data.status == TaskStatus.COMPLETED:
        update["completed_at"] = now
    await db.tasks.update_one({"_id": oid(task_id)},
                              {"$set": update,
                               "$push": {"history": {"status": data.status.value, "at": now,
                                                     "by": user.id, "note": data.note}}})
    if data.status in {TaskStatus.COMPLETED, TaskStatus.SUBMITTED} and doc.get("assigned_by"):
        await notify(db, user_ids=[doc["assigned_by"]], type=NotificationType.TASK_COMPLETED,
                     title_key="notify.task_status_changed",
                     params={"person": doc["assignee_name"], "task": doc["title"]},
                     param_keys={"status": f"task_status.{data.status.value}"},
                     body=doc.get("case_reference", ""), data={"task_id": task_id})
    return serialize(await _get(db, task_id))


async def add_deliverable(db, user: CurrentUser, task_id: str,
                          file: UploadFile) -> Dict[str, Any]:
    doc = await _get(db, task_id)
    if user.role == Role.PARTNER and doc["assignee_id"] != user.id:
        raise Forbidden("This task is not assigned to you")

    stored = await storage.save_upload(
        db, file,
        bucket_name=storage.DELIVERABLES_BUCKET,
        metadata={"task_id": task_id, "case_id": doc.get("case_id"),
                  "partner_id": user.id},
    )
    entry = {**stored, "uploaded_by": user.id,
             "uploaded_by_name": user.raw.get("full_name"), "uploaded_at": utcnow()}
    await db.tasks.update_one({"_id": oid(task_id)},
                              {"$push": {"deliverables": entry},
                               "$set": {"status": TaskStatus.SUBMITTED.value,
                                        "updated_at": utcnow()}})
    return serialize(await _get(db, task_id))


async def delete_deliverable(db, user: CurrentUser, task_id: str,
                             file_id: str) -> Dict[str, Any]:
    """Partner removes a wrongly uploaded deliverable."""
    doc = await _get(db, task_id)
    if doc["assignee_id"] != user.id:
        raise Forbidden("This task is not assigned to you")
    if doc["status"] == TaskStatus.COMPLETED.value:
        raise Forbidden("Cannot modify deliverables on a completed task")

    found = None
    for entry in doc.get("deliverables", []):
        if entry.get("file_id") == file_id:
            found = entry
            break
    if not found:
        raise NotFound("Deliverable not found on this task")

    # Remove the blob from GridFS
    await storage.delete_file(db, file_id,
                              found.get("bucket", storage.DELIVERABLES_BUCKET))

    # Pull the entry from the deliverables array
    await db.tasks.update_one(
        {"_id": oid(task_id)},
        {"$pull": {"deliverables": {"file_id": file_id}},
         "$set": {"updated_at": utcnow()}},
    )

    # If no deliverables remain, revert status to in_progress
    remaining = len(doc.get("deliverables", [])) - 1
    if remaining <= 0:
        await db.tasks.update_one(
            {"_id": oid(task_id)},
            {"$set": {"status": TaskStatus.IN_PROGRESS.value}},
        )

    return serialize(await _get(db, task_id))


async def mark_completed(db, user: CurrentUser, task_id: str,
                         data) -> Dict[str, Any]:
    """Partner marks a task as completed with optional delivery notes."""
    doc = await _get(db, task_id)
    if doc["assignee_id"] != user.id:
        raise Forbidden("This task is not assigned to you")
    now = utcnow()
    update: Dict[str, Any] = {
        "status": TaskStatus.COMPLETED.value,
        "completed_at": now,
        "updated_at": now,
    }
    if data.delivery_notes:
        update["delivery_notes"] = data.delivery_notes
    await db.tasks.update_one(
        {"_id": oid(task_id)},
        {"$set": update,
         "$push": {"history": {"status": TaskStatus.COMPLETED.value, "at": now,
                               "by": user.id, "note": data.delivery_notes}}},
    )
    # Notify the consultant who assigned this task
    if doc.get("assigned_by"):
        await notify(db, user_ids=[doc["assigned_by"]],
                     type=NotificationType.TASK_COMPLETED,
                     title_key="notify.task_completed",
                     params={"person": doc["assignee_name"],
                             "task": doc["title"]},
                     body=doc.get("case_reference", ""),
                     data={"task_id": task_id, "case_id": doc.get("case_id")})
    return serialize(await _get(db, task_id))


async def get_deliverable(db, user: CurrentUser, task_id: str,
                          file_id: str) -> Dict[str, Any]:
    doc = await _get(db, task_id)
    if user.role in {Role.PARTNER, Role.CLIENT} and doc["assignee_id"] != user.id:
        raise Forbidden("This task is not assigned to you")
    for entry in doc.get("deliverables", []):
        if entry.get("file_id") == file_id:
            return entry
    raise NotFound("Deliverable not found on this task")


async def get_partner_dashboard(db, user: CurrentUser) -> Dict[str, Any]:
    """Partner home dashboard with task summary counts."""
    partner_id = user.id
    total = await db.tasks.count_documents({"assignee_id": partner_id})
    pending = await db.tasks.count_documents(
        {"assignee_id": partner_id, "status": TaskStatus.PENDING.value})
    in_progress = await db.tasks.count_documents(
        {"assignee_id": partner_id, "status": TaskStatus.IN_PROGRESS.value})
    completed = await db.tasks.count_documents(
        {"assignee_id": partner_id, "status": TaskStatus.COMPLETED.value})
    submitted = await db.tasks.count_documents(
        {"assignee_id": partner_id, "status": TaskStatus.SUBMITTED.value})

    overdue = await db.tasks.count_documents({
        "assignee_id": partner_id,
        "due_date": {"$lt": utcnow()},
        "status": {"$in": [TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value]},
    })

    # Recent tasks for the home screen list. Submitted work is still the
    # partner's until the consultant signs it off, so it belongs here too.
    cursor = db.tasks.find(
        {"assignee_id": partner_id, "status": {"$in": [
            TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value,
            TaskStatus.SUBMITTED.value]}}
    ).sort("due_date", 1).limit(10)
    active_tasks = [serialize(t) async for t in cursor]

    return {
        "partner_id": partner_id,
        "partner_name": user.raw.get("full_name", ""),
        "consultant_id": user.raw.get("consultant_id"),
        "summary": {
            "total": total,
            "pending": pending,
            "in_progress": in_progress,
            "submitted": submitted,
            "completed": completed,
            "overdue": overdue,
        },
        "active_tasks": active_tasks,
    }
