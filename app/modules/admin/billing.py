"""admin/Billing.tsx — platform revenue across every tenant.  [INFERRED]"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, page_params
from app.core.enums import BillingCycle, PlanCode, TenantStatus
from app.core.utils import utcnow
from app.db.mongo import platform_db
from app.modules.admin.deps import require_billing_admin
from app.modules.subscriptions.plans import PLANS
from app.schemas.common import PageParams
from app.services.pagination import paginate

router = APIRouter(prefix="/billing", tags=["Super Admin · Billing"])


def _monthly_value(sub: dict) -> float:
    amount = sub.get("amount", 0) or 0
    return amount / 12 if sub.get("billing_cycle") == BillingCycle.ANNUAL.value else amount


@router.get("/overview", summary="MRR, ARR, plan mix and churn")
async def overview(user: CurrentUser = Depends(require_billing_admin)):
    db = platform_db()
    mrr = 0.0
    plan_mix = {p.value: 0 for p in PlanCode}
    cycle_mix = {c.value: 0 for c in BillingCycle}

    async for sub in db.subscriptions.find({"status": "active"}):
        mrr += _monthly_value(sub)
        if sub.get("plan_code") in plan_mix:
            plan_mix[sub["plan_code"]] += 1
        if sub.get("billing_cycle") in cycle_mix:
            cycle_mix[sub["billing_cycle"]] += 1

    since = utcnow() - timedelta(days=30)
    active = await db.tenants.count_documents({"status": TenantStatus.ACTIVE.value})
    cancelled_30d = await db.tenants.count_documents(
        {"status": TenantStatus.CANCELLED.value, "cancelled_at": {"$gte": since}})

    collected_30d = 0.0
    async for sub in db.subscriptions.find({"created_at": {"$gte": since}}):
        collected_30d += sub.get("total", 0) or 0

    return {
        "mrr": round(mrr, 2),
        "arr": round(mrr * 12, 2),
        "active_subscriptions": await db.subscriptions.count_documents({"status": "active"}),
        "active_tenants": active,
        "past_due_tenants": await db.tenants.count_documents(
            {"status": TenantStatus.PAST_DUE.value}),
        "cancelled_last_30d": cancelled_30d,
        "churn_rate_30d": round(cancelled_30d / active * 100, 2) if active else 0.0,
        "collected_last_30d": round(collected_30d, 2),
        "plan_mix": plan_mix,
        "billing_cycle_mix": cycle_mix,
        "plan_catalogue": {code.value: {"monthly": PLANS[code]["monthly_price"],
                                        "annual": PLANS[code]["annual_price"]}
                           for code in PlanCode},
    }


@router.get("/subscriptions", summary="Every subscription on the platform")
async def subscriptions(status: Optional[str] = Query(None),
                        plan_code: Optional[PlanCode] = Query(None),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(require_billing_admin)):
    query = {}
    if status:
        query["status"] = status
    if plan_code:
        query["plan_code"] = plan_code.value
    page = await paginate(platform_db(), "subscriptions", query, params,
                          sort=[("created_at", -1)])
    tenants = {}
    for item in page["items"]:
        tid = item.get("tenant_id")
        if tid and tid not in tenants:
            from app.core.utils import oid
            t = await platform_db().tenants.find_one({"_id": oid(tid)})
            tenants[tid] = {"name": t["name"], "owner_email": t["owner_email"]} if t else None
        item["organization"] = tenants.get(tid)
    return page


@router.get("/revenue-by-month", summary="Collected revenue grouped by month")
async def revenue_by_month(months: int = Query(12, ge=1, le=36),
                           user: CurrentUser = Depends(require_billing_admin)):
    since = utcnow() - timedelta(days=31 * months)
    cursor = platform_db().subscriptions.aggregate([
        {"$match": {"created_at": {"$gte": since}}},
        {"$group": {
            "_id": {"y": {"$year": "$created_at"}, "m": {"$month": "$created_at"}},
            "total": {"$sum": "$total"},
            "count": {"$sum": 1},
        }},
        {"$sort": {"_id.y": 1, "_id.m": 1}},
    ])
    return {"items": [{"year": r["_id"]["y"], "month": r["_id"]["m"],
                       "total": round(r["total"] or 0, 2), "count": r["count"]}
                      async for r in cursor]}
