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
from app.core.enums import CaseStage, DocumentStatus, RequestStatus, Role, TaskStatus
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


@router.get("/reporting/summary", summary="Reporting: KPI cards for the Reports screen "
                                          "(Cases resolved, Avg resolution, Approval rate, "
                                          "Overdue tasks, Cases by process type)")
async def reporting_summary(period: str = Query("90d", pattern="^(30d|90d|12m)$",
                                                 description="30d | 90d | 12m — matches the "
                                                             "Reports screen filter chips"),
                            user: CurrentUser = Depends(require_consultant),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)) -> Dict[str, Any]:
    now = utcnow()
    period_days = {"30d": 30, "90d": 90, "12m": 365}[period]
    since = now - timedelta(days=period_days)
    prev_since = since - timedelta(days=period_days)

    def pct_change(curr, prev):
        if curr is None or prev in (None, 0):
            return None
        return round(((curr - prev) / prev) * 100)

    # ── Cases resolved: cases that reached "completed" stage in the window ──
    cases_resolved = await db.cases.count_documents(
        {"stage": CaseStage.COMPLETED.value, "updated_at": {"$gte": since, "$lte": now}})
    cases_resolved_prev = await db.cases.count_documents(
        {"stage": CaseStage.COMPLETED.value, "updated_at": {"$gte": prev_since, "$lt": since}})

    # ── Avg resolution: mean days between case creation and completion ──
    async def _avg_resolution_days(start, end) -> Any:
        cursor = db.cases.find(
            {"stage": CaseStage.COMPLETED.value, "updated_at": {"$gte": start, "$lt": end}},
            {"created_at": 1, "updated_at": 1})
        durations = [(c["updated_at"] - c["created_at"]).days
                    async for c in cursor if c.get("created_at") and c.get("updated_at")]
        return round(sum(durations) / len(durations)) if durations else None

    avg_resolution_days = await _avg_resolution_days(since, now + timedelta(seconds=1))
    avg_resolution_days_prev = await _avg_resolution_days(prev_since, since)

    # ── Approval rate: approved vs (approved + rejected) documents decided in window ──
    approved = await db.documents.count_documents(
        {"status": DocumentStatus.APPROVED.value, "updated_at": {"$gte": since, "$lte": now}})
    rejected = await db.documents.count_documents(
        {"status": DocumentStatus.REJECTED.value, "updated_at": {"$gte": since, "$lte": now}})
    decided = approved + rejected
    approval_rate = round((approved / decided) * 100) if decided else None

    approved_prev = await db.documents.count_documents(
        {"status": DocumentStatus.APPROVED.value, "updated_at": {"$gte": prev_since, "$lt": since}})
    rejected_prev = await db.documents.count_documents(
        {"status": DocumentStatus.REJECTED.value, "updated_at": {"$gte": prev_since, "$lt": since}})
    decided_prev = approved_prev + rejected_prev
    approval_rate_prev = round((approved_prev / decided_prev) * 100) if decided_prev else None

    # ── Overdue tasks: currently open tasks past due date, vs same check at period start ──
    overdue_now = await db.tasks.count_documents(
        {"due_date": {"$lt": now},
         "status": {"$nin": [TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]}})
    overdue_at_period_start = await db.tasks.count_documents(
        {"due_date": {"$lt": since}, "created_at": {"$lte": since},
         "status": {"$nin": [TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value]}})

    # ── Cases by process type: case_type is free text, e.g. "Work Visa" ──
    pipeline = [
        {"$match": {"created_at": {"$gte": since, "$lte": now}}},
        {"$group": {"_id": "$case_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    cases_by_process_type = [{"process_type": row["_id"] or "Other", "count": row["count"]}
                             async for row in db.cases.aggregate(pipeline)]

    return {
        "period": period,
        "cases_resolved": {
            "value": cases_resolved,
            "change_pct": pct_change(cases_resolved, cases_resolved_prev),
        },
        "avg_resolution_days": {
            "value": avg_resolution_days,
            "change_days": (avg_resolution_days - avg_resolution_days_prev)
                if avg_resolution_days is not None and avg_resolution_days_prev is not None
                else None,
        },
        "approval_rate": {
            "value": approval_rate,
            "change_pct": pct_change(approval_rate, approval_rate_prev),
        },
        "overdue_tasks": {
            "value": overdue_now,
            "change": overdue_now - overdue_at_period_start,
        },
        "cases_by_process_type": cases_by_process_type,
    }


@router.get("/activities", summary="Recent activity feed")
async def activities(limit: int = Query(20, ge=1, le=100),
                     user: CurrentUser = Depends(get_current_user),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return {"items": [serialize(d) async for d in
                      db.activities.find().sort("created_at", -1).limit(limit)]}
