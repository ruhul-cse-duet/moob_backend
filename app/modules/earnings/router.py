"""
partner/PartnerEarnings.tsx  [INFERRED]

Modelled as an accrual ledger: the consultant sets a fee when assigning a partner
task, an earning row is accrued when the partner completes it, the consultant
approves it, and payouts batch approved rows. Ledger rather than a running total
so every number on the screen is traceable to a task.
"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, status as http
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_consultant,
)
from app.core.enums import NotificationType, PayoutStatus, Role, TaskStatus
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import PageParams
from app.services.events import notify
from app.services.pagination import paginate

router = APIRouter(prefix="/earnings", tags=["Partner Earnings"])


class EarningCreate(BaseModel):
    task_id: str
    amount: float = Field(gt=0)
    currency: str = "USD"
    note: Optional[str] = None


@router.post("", status_code=http.HTTP_201_CREATED,
             summary="Accrue a fee against a completed partner task")
async def accrue(payload: EarningCreate,
                 user: CurrentUser = Depends(require_consultant),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    task = await db.tasks.find_one({"_id": oid(payload.task_id)})
    if not task:
        raise NotFound("Task not found")
    if await db.earnings.find_one({"task_id": payload.task_id}):
        raise BadRequest("This task already has an earning recorded")

    now = utcnow()
    doc = {
        "task_id": payload.task_id,
        "task_title": task["title"],
        "case_id": task.get("case_id"),
        "case_reference": task.get("case_reference"),
        "partner_id": task["assignee_id"],
        "partner_name": task.get("assignee_name"),
        "consultant_id": task.get("consultant_id") or user.id,
        "amount": payload.amount,
        "currency": payload.currency,
        "note": payload.note,
        "status": PayoutStatus.ACCRUED.value,
        "accrued_by": user.id,
        "created_at": now,
        "updated_at": now,
    }
    earning_id = str((await db.earnings.insert_one(doc)).inserted_id)
    await notify(db, user_ids=[task["assignee_id"]], type=NotificationType.TASK_COMPLETED,
                 title=f"{payload.currency} {payload.amount:.2f} accrued",
                 body=task["title"], data={"earning_id": earning_id})
    return serialize({**doc, "_id": oid(earning_id)})


@router.get("/summary", summary="Totals for the partner earnings header")
async def summary(partner_id: Optional[str] = Query(None),
                  user: CurrentUser = Depends(get_current_user),
                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    target = user.id if user.role == Role.PARTNER else partner_id
    match = {"partner_id": target} if target else {}

    totals = {}
    for st in PayoutStatus:
        cursor = db.earnings.aggregate([
            {"$match": {**match, "status": st.value}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}, "n": {"$sum": 1}}},
        ])
        row = None
        async for r in cursor:
            row = r
        totals[st.value] = {"amount": round((row or {}).get("total", 0) or 0, 2),
                            "count": (row or {}).get("n", 0)}

    since = utcnow() - timedelta(days=30)
    cursor = db.earnings.aggregate([
        {"$match": {**match, "created_at": {"$gte": since}}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ])
    last_30 = 0.0
    async for r in cursor:
        last_30 = round(r.get("total", 0) or 0, 2)

    open_tasks = await db.tasks.count_documents({
        "assignee_id": target,
        "status": {"$in": [TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value]},
    }) if target else 0

    return {"by_status": totals, "last_30_days": last_30, "open_tasks": open_tasks,
            "currency": "USD"}


@router.get("", summary="Earnings ledger")
async def list_earnings(status: Optional[PayoutStatus] = Query(None),
                        partner_id: Optional[str] = Query(None),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    query = {}
    if user.role == Role.PARTNER:
        query["partner_id"] = user.id
    elif partner_id:
        query["partner_id"] = partner_id
    elif user.role == Role.CLIENT:
        raise Forbidden("Clients cannot see partner earnings")
    if status:
        query["status"] = status.value
    return await paginate(db, "earnings", query, params, sort=[("created_at", -1)])


@router.post("/{earning_id}/approve", summary="Approve an accrued fee for payout")
async def approve(earning_id: str,
                  user: CurrentUser = Depends(require_consultant),
                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    result = await db.earnings.find_one_and_update(
        {"_id": oid(earning_id), "status": PayoutStatus.ACCRUED.value},
        {"$set": {"status": PayoutStatus.APPROVED.value, "approved_by": user.id,
                  "approved_at": utcnow(), "updated_at": utcnow()}},
        return_document=True)
    if not result:
        raise BadRequest("Only accrued earnings can be approved")
    return serialize(result)


@router.post("/payouts", summary="Mark approved earnings as paid")
async def create_payout(partner_id: str = Body(embed=True),
                        reference: Optional[str] = Body(None, embed=True),
                        user: CurrentUser = Depends(require_consultant),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    now = utcnow()
    rows = [serialize(e) async for e in db.earnings.find(
        {"partner_id": partner_id, "status": PayoutStatus.APPROVED.value})]
    if not rows:
        raise BadRequest("Nothing approved and unpaid for this partner")

    total = round(sum(r["amount"] for r in rows), 2)
    payout = {
        "partner_id": partner_id,
        "partner_name": rows[0].get("partner_name"),
        "consultant_id": user.id,
        "earning_ids": [r["id"] for r in rows],
        "amount": total,
        "currency": rows[0].get("currency", "USD"),
        "reference": reference,
        "paid_by": user.id,
        "created_at": now,
    }
    payout_id = str((await db.payouts.insert_one(payout)).inserted_id)
    await db.earnings.update_many(
        {"_id": {"$in": [oid(r["id"]) for r in rows]}},
        {"$set": {"status": PayoutStatus.PAID.value, "payout_id": payout_id,
                  "paid_at": now, "updated_at": now}})
    await notify(db, user_ids=[partner_id], type=NotificationType.TASK_COMPLETED,
                 title=f"Payout of {payout['currency']} {total:.2f} sent",
                 data={"payout_id": payout_id})
    return serialize({**payout, "_id": oid(payout_id)})


@router.get("/payouts", summary="Payout history")
async def list_payouts(params: PageParams = Depends(page_params),
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    query = {"partner_id": user.id} if user.role == Role.PARTNER else {}
    return await paginate(db, "payouts", query, params, sort=[("created_at", -1)])
