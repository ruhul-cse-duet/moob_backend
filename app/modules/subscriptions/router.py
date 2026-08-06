from datetime import timedelta
from fastapi import APIRouter, Body, Depends

from app.core.deps import CurrentUser, require_owner
from app.core.enums import BillingCycle, PlanCode, TenantStatus
from app.core.exceptions import NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.subscriptions.plans import PLANS, order_summary
from app.schemas.common import Message

router = APIRouter(prefix="/subscription", tags=["Subscription & Billing"])


@router.get("", summary="Current subscription (owner only - partners never see billing)")
async def current(user: CurrentUser = Depends(require_owner)):
    db = platform_db()
    tenant = await db.tenants.find_one({"_id": oid(user.tenant_id)})
    sub = await db.subscriptions.find_one({"tenant_id": user.tenant_id, "status": "active"})
    if not tenant:
        raise NotFound("Workspace not found")
    plan = PLANS[PlanCode(tenant["plan_code"])]
    return {
        "tenant": serialize(tenant),
        "subscription": serialize(sub),
        "plan": plan,
    }


@router.get("/invoices")
async def invoices(user: CurrentUser = Depends(require_owner)):
    db = platform_db()
    return {"items": [serialize(d) async for d in
                      db.subscriptions.find({"tenant_id": user.tenant_id})
                      .sort("created_at", -1)]}


@router.post("/change-plan", summary="Upgrade or downgrade the plan")
async def change_plan(plan_code: PlanCode = Body(embed=True),
                      billing_cycle: BillingCycle = Body(BillingCycle.MONTHLY, embed=True),
                      user: CurrentUser = Depends(require_owner)):
    db = platform_db()
    summary = order_summary(plan_code, billing_cycle)
    renews = utcnow() + (timedelta(days=365) if billing_cycle == BillingCycle.ANNUAL
                         else timedelta(days=30))
    await db.tenants.update_one(
        {"_id": oid(user.tenant_id)},
        {"$set": {"plan_code": plan_code.value, "billing_cycle": billing_cycle.value,
                  "renews_on": renews, "status": TenantStatus.ACTIVE.value,
                  "updated_at": utcnow()}},
    )
    await db.subscriptions.update_many({"tenant_id": user.tenant_id, "status": "active"},
                                       {"$set": {"status": "superseded"}})
    await db.subscriptions.insert_one({
        "tenant_id": user.tenant_id, "plan_code": plan_code.value,
        "billing_cycle": billing_cycle.value, "amount": summary["subtotal"],
        "tax": summary["estimated_tax"], "total": summary["total_due_today"],
        "status": "active", "started_at": utcnow(), "renews_on": renews,
        "created_at": utcnow(),
    })
    return {"detail": f"Plan changed to {plan_code.value}", "renews_on": renews, **summary}


@router.post("/cancel", response_model=Message)
async def cancel(user: CurrentUser = Depends(require_owner)):
    db = platform_db()
    await db.tenants.update_one({"_id": oid(user.tenant_id)},
                                {"$set": {"status": TenantStatus.CANCELLED.value,
                                          "cancelled_at": utcnow(), "updated_at": utcnow()}})
    await db.subscriptions.update_many({"tenant_id": user.tenant_id, "status": "active"},
                                       {"$set": {"status": "cancelled"}})
    return {"detail": "Subscription cancelled. The workspace stays readable until renewal."}
