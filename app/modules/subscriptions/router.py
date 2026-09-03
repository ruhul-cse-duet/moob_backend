from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Body, Depends

from app.core.deps import CurrentUser, require_owner
from app.core.enums import AuditAction, BillingCycle, PlanCode, TenantStatus
from app.core.exceptions import BadRequest, NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.auth.schemas import PaymentConfig
from app.modules.subscriptions.plans import is_downgrade, order_summary, plan_by_code
from app.schemas.common import Message
from app.services import audit, stripe_service

router = APIRouter(prefix="/subscription", tags=["Subscription & Billing"])

# An administrator's suspension, or a cancellation, outranks a plan change:
# otherwise "upgrade" is a way to put a closed workspace back into service.
_CLOSED_TO_PLAN_CHANGES = {TenantStatus.SUSPENDED.value, TenantStatus.CANCELLED.value}


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
    """Move the workspace onto another plan, and charge for it.

    The order matters: Stripe bills the prorated difference first, and only a
    subscription that actually moved gets written down here. Updating our own
    records first would hand out Enterprise seats against a Starter
    subscription - the money and the entitlement have to change together.
    """
    db = platform_db()

    # A live subscription can only move up. Dropping to a smaller plan mid-term
    # would take away seats and case capacity that are already in use - and the
    # workspace has been paid for at the larger size until it renews. Once the
    # plan has ended there is nothing left to shrink, so every plan is open
    # again.
    tenant = await db.tenants.find_one({"_id": oid(user.tenant_id)})
    if not tenant:
        raise NotFound("Workspace not found")
    if tenant.get("status") in _CLOSED_TO_PLAN_CHANGES:
        raise BadRequest(
            "This workspace is not currently active, so its plan cannot be "
            "changed. Contact support to reopen it first."
        )
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

    plan = await plan_by_code(plan_code)
    summary = await order_summary(plan_code, billing_cycle)
    billed = await _bill_plan_change(tenant, plan_code, billing_cycle, plan, summary)

    # Stripe's own period end is the truth once it is billing this plan; the
    # computed date is only for workspaces that are not on Stripe at all.
    renews = _from_epoch(billed.get("current_period_end")) or (
        utcnow() + (timedelta(days=365) if billing_cycle == BillingCycle.ANNUAL
                    else timedelta(days=30))
    )
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
        "stripe_subscription_id": tenant.get("stripe_subscription_id"),
        "stripe_price_id": billed.get("price_id"),
        "last_invoice_id": billed.get("latest_invoice_id"),
        "created_at": utcnow(),
    })
    # Oversight lists plan changes; an upgrade that moved real money is exactly
    # the kind of thing an administrator has to be able to find afterwards.
    await audit.record(
        action=AuditAction.PLAN_CHANGED, actor_id=user.id, actor_email=user.email,
        actor_role="consultant_owner", tenant_id=user.tenant_id,
        subject=f"{plan_code.value} ({billing_cycle.value})",
        detail=f"{tenant.get('plan_code')} -> {plan_code.value}",
        meta={"invoice_id": billed.get("latest_invoice_id"),
              "price_id": billed.get("price_id"),
              "total": summary["total_due_today"]},
    )
    return {"detail": f"Plan changed to {plan_code.value}", "renews_on": renews,
            "billing": {"charged": bool(billed.get("latest_invoice_id")),
                        "invoice_id": billed.get("latest_invoice_id"),
                        "message": billed.get("message")},
            **summary}


async def _bill_plan_change(tenant: dict, plan_code: PlanCode, billing_cycle: BillingCycle,
                            plan: dict, summary: dict) -> dict:
    """Charge for the new plan at Stripe, or explain why nothing can be charged.

    Raises ``BadRequest`` rather than returning, because every failure here has
    to stop the plan change: an upgrade that Stripe refused must leave the
    workspace exactly where it was.
    """
    if not stripe_service.configured():
        # No keys at all - a development environment. The catalogue still works
        # and the plan still moves; nothing was ever going to be billed.
        return {}

    subscription_id = tenant.get("stripe_subscription_id")
    if not subscription_id:
        raise BadRequest(
            "This workspace has no recurring subscription to move, so the new "
            "plan cannot be billed. Add a card under Payment methods and "
            "contact support to start the subscription."
        )

    result = await stripe_service.change_subscription_plan(
        subscription_id=subscription_id,
        plan_code=plan_code,
        billing_cycle=billing_cycle,
        # The price the owner was just quoted, so the charge matches the screen
        # even if the super admin edits the catalogue a minute from now.
        amount=summary["subtotal"],
        plan_name=plan.get("name"),
        tenant_id=str(tenant["_id"]),
    )
    if not result["success"]:
        raise BadRequest(result["message"])
    return result


def _from_epoch(value):
    if not value:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


async def _customer_id(user: CurrentUser) -> str:
    tenant = await platform_db().tenants.find_one({"_id": oid(user.tenant_id)},
                                                  {"stripe_customer_id": 1})
    if not tenant:
        raise NotFound("Workspace not found")
    return tenant.get("stripe_customer_id") or ""


@router.get("/payment-config", response_model=PaymentConfig,
            summary="What the billing screen needs to collect a card")
async def payment_config(user: CurrentUser = Depends(require_owner)):
    """The publishable key, for the owner adding a card to an existing workspace.

    Signup has its own copy of this under /auth, which an authenticated billing
    screen has no business calling. Without one here the screen has no key, so
    Stripe.js never initialises and the card form can only report that it failed
    to load.
    """
    return stripe_service.client_config()


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
