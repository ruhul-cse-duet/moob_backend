from datetime import timedelta
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    require_active_tenant,
    require_consultant,
)
from app.core.enums import CASE_STAGE_ORDER, DocumentStatus, RequestStatus, Role, TaskStatus
from app.core.utils import serialize, utcnow

router = APIRouter(tags=["Dashboard & Reporting"],
                   dependencies=[Depends(require_active_tenant)])


@router.get("/dashboard", summary="Home screen: greeting counters, queue preview, activity")
async def dashboard(user: CurrentUser = Depends(get_current_user),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)) -> Dict[str, Any]:
    now = utcnow()
    if user.role == Role.CLIENT:
        scope = {"client_id": user.id}
        return {
            "greeting_name": (user.raw.get("full_name") or "").split(" ")[0],
            "counters": {
                "open_requests": await db.requests.count_documents(
                    {**scope, "status": {"$ne": RequestStatus.COMPLETED.value}}),
                "documents_to_upload": await db.documents.count_documents(
                    {**scope, "status": {"$in": [DocumentStatus.UPLOAD_NEEDED.value,
                                                 DocumentStatus.NEEDS_REUPLOAD.value]}}),
                "active_cases": await db.cases.count_documents(
                    {**scope, "stage": {"$ne": "completed"}}),
                "open_tasks": await db.tasks.count_documents(
                    {"assignee_id": user.id, "status": {"$in": [TaskStatus.PENDING.value,
                                                                TaskStatus.IN_PROGRESS.value]}}),
            },
            "requests": [serialize(d) async for d in
                         db.requests.find(scope).sort("created_at", -1).limit(5)],
            "cases": [serialize(d) async for d in
                      db.cases.find(scope).sort("updated_at", -1).limit(5)],
        }

    if user.role == Role.PARTNER:
        return {
            "greeting_name": (user.raw.get("full_name") or "").split(" ")[0],
            "counters": {
                "pending": await db.tasks.count_documents(
                    {"assignee_id": user.id, "status": TaskStatus.PENDING.value}),
                "in_progress": await db.tasks.count_documents(
                    {"assignee_id": user.id, "status": TaskStatus.IN_PROGRESS.value}),
                "completed": await db.tasks.count_documents(
                    {"assignee_id": user.id, "status": TaskStatus.COMPLETED.value}),
                "due_this_week": await db.tasks.count_documents(
                    {"assignee_id": user.id, "due_date": {"$lte": now + timedelta(days=7)},
                     "status": {"$ne": TaskStatus.COMPLETED.value}}),
            },
            "tasks": [serialize(d) async for d in
                      db.tasks.find({"assignee_id": user.id}).sort("due_date", 1).limit(10)],
        }

    # Consultant workspace home
    return {
        "greeting_name": (user.raw.get("full_name") or "").split(" ")[0],
        "counters": {
            "todays_requests": await db.requests.count_documents(
                {"created_at": {"$gte": now.replace(hour=0, minute=0, second=0, microsecond=0)}}),
            "to_review": await db.requests.count_documents(
                {"status": {"$in": [RequestStatus.NEW.value,
                                    RequestStatus.DOCUMENTS_RECEIVED.value]}}),
            "waiting": await db.requests.count_documents(
                {"status": RequestStatus.WAITING_FOR_CLIENT.value}),
            "completed": await db.requests.count_documents(
                {"status": RequestStatus.COMPLETED.value}),
            "documents_awaiting_decision": await db.documents.count_documents(
                {"status": DocumentStatus.WITH_CONSULTANT.value}),
            "active_cases": await db.cases.count_documents({"stage": {"$ne": "completed"}}),
        },
        "open_requests": [serialize(d) async for d in
                          db.requests.find({"status": {"$ne": RequestStatus.COMPLETED.value}})
                          .sort("created_at", -1).limit(5)],
        "recent_activities": [serialize(d) async for d in
                              db.activities.find().sort("created_at", -1).limit(10)],
    }


@router.get("/reporting/overview", summary="Reporting: pipeline and throughput")
async def reporting(days: int = Query(30, ge=1, le=365),
                    user: CurrentUser = Depends(require_consultant),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)) -> Dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    by_stage = {st.value: await db.cases.count_documents({"stage": st.value})
                for st in CASE_STAGE_ORDER}
    by_status = {st.value: await db.requests.count_documents({"status": st.value})
                 for st in RequestStatus}
    doc_stats = {st.value: await db.documents.count_documents({"status": st.value})
                 for st in DocumentStatus}
    return {
        "period_days": days,
        "requests_received": await db.requests.count_documents({"created_at": {"$gte": since}}),
        "cases_opened": await db.cases.count_documents({"created_at": {"$gte": since}}),
        "cases_completed": await db.cases.count_documents(
            {"stage": "completed", "updated_at": {"$gte": since}}),
        "tasks_completed": await db.tasks.count_documents(
            {"status": TaskStatus.COMPLETED.value, "updated_at": {"$gte": since}}),
        "cases_by_stage": by_stage,
        "requests_by_status": by_status,
        "documents_by_status": doc_stats,
        "clients": await db.users.count_documents({"role": Role.CLIENT.value}),
        "partners": await db.users.count_documents({"role": Role.PARTNER.value}),
    }


@router.get("/activities", summary="Recent activity feed")
async def activities(limit: int = Query(20, ge=1, le=100),
                     user: CurrentUser = Depends(get_current_user),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return {"items": [serialize(d) async for d in
                      db.activities.find().sort("created_at", -1).limit(limit)]}
