from datetime import timedelta
from fastapi import APIRouter, Body, Depends

from app.core.deps import CurrentUser, require_owner
from app.core.enums import BillingCycle, PlanCode, TenantStatus
from app.core.exceptions import BadRequest, NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.subscriptions.plans import is_downgrade, order_summary, plan_by_code
from app.schemas.common import Message
from app.services import stripe_service

router = APIRouter(prefix="/subscription", tags=["Subscription & Billing"])


@router.get("", summary="Current subscription (owner only - partners never see billing)")
async def current(user: CurrentUser = Depends(require_owner)):
    db = platform_db()
    tenant = await db.tenants.find_one({"_id": oid(user.tenant_id)})
    sub = await db.subscriptions.find_one({"tenant_id": user.tenant_id, "status": "active"})
    if not tenant:
        raise NotFound("Workspace not found")
    plan = await plan_by_code(PlanCode(tenant["plan_code"]))
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

    # A live subscription can only move up. Dropping to a smaller plan mid-term
    # would take away seats and case capacity that are already in use - and the
    # workspace has been paid for at the larger size until it renews. Once the
    # plan has ended there is nothing left to shrink, so every plan is open
    # again.
    tenant = await db.tenants.find_one({"_id": oid(user.tenant_id)}, {"plan_code": 1})
    if not tenant:
        raise NotFound("Workspace not found")
    live = await db.subscriptions.find_one({"tenant_id": user.tenant_id, "status": "active"})
    if live:
        current = PlanCode(live.get("plan_code") or tenant["plan_code"])
        if is_downgrade(current, plan_code):
            current_name = (await plan_by_code(current))["name"]
            wanted_name = (await plan_by_code(plan_code))["name"]
            raise BadRequest(
                f"Your {current_name} plan is still running, so it cannot be moved "
                f"down to {wanted_name}. You can upgrade now, or switch to "
                f"{wanted_name} once the current plan ends."
            )

    summary = await order_summary(plan_code, billing_cycle)
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


async def _customer_id(user: CurrentUser) -> str:
    tenant = await platform_db().tenants.find_one({"_id": oid(user.tenant_id)},
                                                  {"stripe_customer_id": 1})
    if not tenant:
        raise NotFound("Workspace not found")
    return tenant.get("stripe_customer_id") or ""


@router.get("/payment-methods", summary="Cards saved for this workspace")
async def payment_methods(user: CurrentUser = Depends(require_owner)):
    """What the subscription is billed to, so the owner can tell one card from
    another and pick between them rather than being shown a single fixed row."""
    cards = await stripe_service.list_payment_methods(await _customer_id(user))
    return {"items": cards}


@router.post("/payment-methods", status_code=201, summary="Save a new card")
async def add_payment_method(payment_method_id: str = Body(embed=True),
                             make_default: bool = Body(True, embed=True),
                             user: CurrentUser = Depends(require_owner)):
    """``payment_method_id`` comes from Stripe.js in the browser. The card
    number itself never reaches this server, which is what keeps it out of our
    PCI scope."""
    result = await stripe_service.attach_payment_method(
        await _customer_id(user), payment_method_id, make_default=make_default)
    if not result["success"]:
        raise BadRequest(result["message"])
    return {"detail": result["message"], "card": result["card"]}


@router.post("/payment-methods/{payment_method_id}/default",
             response_model=Message, summary="Bill the subscription to this card")
async def choose_payment_method(payment_method_id: str,
                                user: CurrentUser = Depends(require_owner)):
    result = await stripe_service.set_default_payment_method(
        await _customer_id(user), payment_method_id)
    if not result["success"]:
        raise BadRequest(result["message"])
    return {"detail": result["message"]}


@router.delete("/payment-methods/{payment_method_id}",
               response_model=Message, summary="Remove a saved card")
async def remove_payment_method(payment_method_id: str,
                                user: CurrentUser = Depends(require_owner)):
    result = await stripe_service.detach_payment_method(
        await _customer_id(user), payment_method_id)
    if not result["success"]:
        raise BadRequest(result["message"])
    return {"detail": result["message"]}


@router.post("/cancel", response_model=Message)
async def cancel(user: CurrentUser = Depends(require_owner)):
    db = platform_db()
    tenant = await db.tenants.find_one({"_id": oid(user.tenant_id)})
    if not tenant:
        raise NotFound("Workspace not found")

    # Stop the recurring charge first. Marking our own row cancelled while Stripe
    # keeps billing monthly is the one failure the customer notices on their
    # statement. The webhook then confirms it from Stripe's side.
    stopped = await stripe_service.cancel_subscription(tenant.get("stripe_subscription_id"))
    if tenant.get("stripe_subscription_id") and not stopped:
        raise BadRequest(
            "Could not cancel the subscription at Stripe. Nothing was changed - "
            "please retry, or contact support so you are not billed again."
        )

    await db.tenants.update_one({"_id": oid(user.tenant_id)},
                                {"$set": {"status": TenantStatus.CANCELLED.value,
                                          "cancelled_at": utcnow(), "updated_at": utcnow()}})
    await db.subscriptions.update_many({"tenant_id": user.tenant_id, "status": "active"},
                                       {"$set": {"status": "cancelled"}})
    return {"detail": "Subscription cancelled. The workspace stays readable until renewal."}
