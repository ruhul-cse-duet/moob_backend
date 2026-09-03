"""Stripe webhook - the only thing that knows a renewal happened.

Nothing else in the API learns that a card was charged next month, or that it
bounced. Without this route ``renews_on`` is a date nobody enforces and
``TenantStatus.PAST_DUE`` / ``EXPIRED`` are values nothing ever sets.

Three rules this route lives by:

1. **Verify before parsing.** The signature is checked against the raw body.
   An unsigned or badly signed request is a 400 and changes nothing.
2. **Process each event once.** Stripe retries on any non-2xx and can deliver
   the same event twice; ``platform.stripe_events`` has a unique index on the
   event id, so a duplicate is recorded as a no-op.
3. **Never fight an administrator.** A suspended or cancelled organization is
   left alone - billing events only move a workspace between the states billing
   owns.
"""
import json
import logging
from typing import Any, Dict, Optional

import stripe
from fastapi import APIRouter, Header, Request
from pymongo.errors import DuplicateKeyError

from app.core.enums import (
    AuditAction,
    BillingCycle,
    PlanCode,
    PlatformInvoiceStatus,
    TenantStatus,
)
from app.core.exceptions import BadRequest
from app.core.utils import oid, utcnow
from app.db.mongo import platform_db
from app.services import audit, invoices, stripe_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# Which tenant states a billing event is allowed to move away from. An admin
# suspension or an explicit cancellation outranks anything Stripe says.
_PAID_RESTORES_FROM = {TenantStatus.PAST_DUE.value, TenantStatus.EXPIRED.value}
_FAILURE_APPLIES_TO = {TenantStatus.ACTIVE.value, TenantStatus.AWAITING_APPROVAL.value,
                       TenantStatus.PAST_DUE.value}
_CANCEL_APPLIES_TO = {TenantStatus.ACTIVE.value, TenantStatus.AWAITING_APPROVAL.value,
                      TenantStatus.PAST_DUE.value, TenantStatus.EXPIRED.value}

# Last resort for reading a billing cycle back off a Price that carries no
# metadata of ours - one made by hand in the Stripe dashboard.
_INTERVAL_CYCLES = {"month": BillingCycle.MONTHLY, "year": BillingCycle.ANNUAL}

HANDLED = {
    "invoice.paid",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
}


async def _find_tenant(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Locate the organization an event belongs to.

    Prefers the tenant id we wrote into Stripe metadata at creation time, and
    falls back to the stored customer id for subscriptions created elsewhere
    (the Stripe dashboard, a migration script).
    """
    db = platform_db()
    tenant_id = (obj.get("metadata") or {}).get("tenant_id")
    if tenant_id:
        tenant = await db.tenants.find_one({"_id": oid(tenant_id)})
        if tenant:
            return tenant
    customer_id = obj.get("customer")
    if customer_id:
        return await db.tenants.find_one({"stripe_customer_id": customer_id})
    subscription_id = obj.get("subscription") or (
        obj.get("id") if str(obj.get("object")) == "subscription" else None
    )
    if subscription_id:
        return await db.tenants.find_one({"stripe_subscription_id": subscription_id})
    return None


async def _set_status(tenant: Dict[str, Any], new_status: str,
                      allowed_from: set, reason: str, extra: Optional[Dict] = None) -> bool:
    db = platform_db()
    current = tenant.get("status")
    if current not in allowed_from:
        logger.info("Stripe %s ignored for tenant %s in status %s",
                    reason, tenant["_id"], current)
        return False
    update = {"status": new_status, "updated_at": utcnow(), **(extra or {})}
    await db.tenants.update_one({"_id": tenant["_id"]}, {"$set": update})
    await audit.record(
        action=AuditAction.TENANT_STATUS_CHANGED, actor_id=None,
        actor_email="stripe@webhook", actor_role="system",
        tenant_id=str(tenant["_id"]), subject=tenant.get("name"),
        detail=f"{current} -> {new_status} ({reason})",
    )
    return True


async def _on_invoice_paid(obj: Dict[str, Any]) -> str:
    """A renewal (or the first invoice) cleared."""
    tenant = await _find_tenant(obj)
    if not tenant:
        return "no matching organization"
    db = platform_db()
    # The Invoices tab has no other source: without this the billing history is
    # empty however many renewals Stripe has taken.
    await invoices.record_from_stripe(tenant=tenant, obj=obj,
                                      status=PlatformInvoiceStatus.PAID)
    period_end = (obj.get("lines", {}).get("data") or [{}])[0].get("period", {}).get("end")
    renews_on = _from_epoch(period_end)

    await db.subscriptions.update_many(
        {"tenant_id": str(tenant["_id"]), "status": "active"},
        {"$set": {"renews_on": renews_on, "last_payment_at": utcnow(),
                  "last_invoice_id": obj.get("id")}} if renews_on else
        {"$set": {"last_payment_at": utcnow(), "last_invoice_id": obj.get("id")}},
    )
    fields = {"renews_on": renews_on} if renews_on else {}
    restored = await _set_status(tenant, TenantStatus.ACTIVE.value, _PAID_RESTORES_FROM,
                                 "invoice paid", fields)
    if not restored and fields:
        await db.tenants.update_one({"_id": tenant["_id"]},
                                    {"$set": {**fields, "updated_at": utcnow()}})
    return "workspace restored" if restored else "renewal recorded"


async def _on_invoice_failed(obj: Dict[str, Any]) -> str:
    tenant = await _find_tenant(obj)
    if not tenant:
        return "no matching organization"
    # Recorded before the status change, so the invoice an administrator has to
    # resolve exists by the time the workspace goes read-only.
    await invoices.record_from_stripe(tenant=tenant, obj=obj,
                                      status=PlatformInvoiceStatus.FAILED)
    await platform_db().subscriptions.update_many(
        {"tenant_id": str(tenant["_id"]), "status": "active"},
        {"$set": {"last_payment_failed_at": utcnow()}},
    )
    changed = await _set_status(tenant, TenantStatus.PAST_DUE.value, _FAILURE_APPLIES_TO,
                                "invoice payment failed")
    return "marked past_due" if changed else "left as is"


def _plan_from_subscription(obj: Dict[str, Any]) -> Optional[tuple]:
    """Which plan Stripe now thinks this subscription is on.

    Three sources, best first:

    1. The subscription's own metadata, which every subscription this API
       creates or moves carries.
    2. The Price's metadata - ``ensure_price`` stamps it, so a plan changed
       straight from the Stripe dashboard still resolves.
    3. The recurring interval, which at least pins the billing cycle when the
       plan itself is unknowable.

    Returns ``(plan_code, billing_cycle)`` with either part ``None`` when it
    could not be worked out, or ``None`` when neither could.
    """
    price = ((obj.get("items") or {}).get("data") or [{}])[0].get("price") or {}

    def _pick(field: str, enum):
        for source in (obj.get("metadata") or {}, price.get("metadata") or {}):
            raw = source.get(field)
            if raw:
                try:
                    return enum(raw)
                except ValueError:
                    logger.warning("Stripe sent an unknown %s %r", field, raw)
        return None

    plan_code = _pick("plan_code", PlanCode)
    billing_cycle = _pick("billing_cycle", BillingCycle)
    if billing_cycle is None:
        interval = (price.get("recurring") or {}).get("interval")
        billing_cycle = _INTERVAL_CYCLES.get(interval)
    if plan_code is None and billing_cycle is None:
        return None
    return plan_code, billing_cycle


async def _sync_plan(tenant: Dict[str, Any], obj: Dict[str, Any]) -> Optional[str]:
    """Follow a plan change made at Stripe rather than through this API.

    The Stripe dashboard can move a subscription onto a different Price, and
    without this the workspace keeps the seats and feature flags of the plan it
    used to pay for. Only ever writes when something actually differs, so the
    ordinary renewal event stays a no-op.
    """
    resolved = _plan_from_subscription(obj)
    if not resolved:
        return None
    plan_code, billing_cycle = resolved

    changes: Dict[str, Any] = {}
    if plan_code and tenant.get("plan_code") != plan_code.value:
        changes["plan_code"] = plan_code.value
    if billing_cycle and tenant.get("billing_cycle") != billing_cycle.value:
        changes["billing_cycle"] = billing_cycle.value
    if not changes:
        return None

    db = platform_db()
    await db.tenants.update_one({"_id": tenant["_id"]},
                                {"$set": {**changes, "updated_at": utcnow()}})
    await db.subscriptions.update_many(
        {"tenant_id": str(tenant["_id"]), "status": "active"},
        {"$set": {**changes, "updated_at": utcnow()}},
    )
    was = tenant.get("plan_code")
    now = changes.get("plan_code", was)
    await audit.record(
        action=AuditAction.PLAN_CHANGED, actor_id=None,
        actor_email="stripe@webhook", actor_role="system",
        tenant_id=str(tenant["_id"]), subject=now,
        detail=f"{was} -> {now} (changed at Stripe)", meta=changes,
    )
    return f"plan synced to {now}"


async def _on_subscription_updated(obj: Dict[str, Any]) -> str:
    """Mirror Stripe's own view of the subscription."""
    tenant = await _find_tenant(obj)
    if not tenant:
        return "no matching organization"

    # The plan follows Stripe whatever the status is: a past_due workspace is
    # still on whichever plan it is failing to pay for.
    plan_note = await _sync_plan(tenant, obj)

    status = obj.get("status")
    if status in ("past_due", "unpaid"):
        changed = await _set_status(tenant, TenantStatus.PAST_DUE.value,
                                    _FAILURE_APPLIES_TO, f"subscription {status}")
        outcome = "marked past_due" if changed else "left as is"
    elif status in ("active", "trialing"):
        changed = await _set_status(tenant, TenantStatus.ACTIVE.value,
                                    _PAID_RESTORES_FROM, f"subscription {status}")
        outcome = "workspace restored" if changed else "left as is"
    elif status == "incomplete_expired":
        changed = await _set_status(tenant, TenantStatus.EXPIRED.value,
                                    _CANCEL_APPLIES_TO, "subscription expired")
        outcome = "marked expired" if changed else "left as is"
    else:
        outcome = f"no rule for subscription status {status}"
    return f"{outcome}; {plan_note}" if plan_note else outcome


async def _on_subscription_deleted(obj: Dict[str, Any]) -> str:
    tenant = await _find_tenant(obj)
    if not tenant:
        return "no matching organization"
    await platform_db().subscriptions.update_many(
        {"tenant_id": str(tenant["_id"]), "status": "active"},
        {"$set": {"status": "cancelled", "cancelled_at": utcnow()}},
    )
    changed = await _set_status(tenant, TenantStatus.CANCELLED.value, _CANCEL_APPLIES_TO,
                                "subscription cancelled at Stripe",
                                {"cancelled_at": utcnow()})
    return "marked cancelled" if changed else "left as is"


_HANDLERS = {
    "invoice.paid": _on_invoice_paid,
    "invoice.payment_succeeded": _on_invoice_paid,
    "invoice.payment_failed": _on_invoice_failed,
    "customer.subscription.updated": _on_subscription_updated,
    "customer.subscription.deleted": _on_subscription_deleted,
}


def _from_epoch(value: Any):
    if not value:
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


@router.post("/stripe", summary="Stripe billing events (renewals, failed payments)")
async def stripe_webhook(request: Request,
                         stripe_signature: Optional[str] = Header(None, alias="Stripe-Signature")):
    # The signature covers the exact bytes Stripe sent - re-serialising the JSON
    # would change them and every event would fail verification.
    payload = await request.body()
    try:
        stripe_service.construct_event(payload, stripe_signature)
    except stripe.SignatureVerificationError as exc:
        logger.warning("Rejected Stripe webhook with a bad signature: %s", exc)
        raise BadRequest("Signature verification failed") from exc
    except ValueError as exc:
        logger.warning("Rejected malformed Stripe webhook: %s", exc)
        raise BadRequest(str(exc)) from exc

    # The signature vouches for these exact bytes, so the parsed JSON is just as
    # trustworthy - and plain dicts beat StripeObject here, which is not a
    # mapping in stripe>=12 and raises AttributeError on .get().
    event = json.loads(payload)
    event_id = event["id"]
    event_type = event["type"]
    db = platform_db()

    # Claim the event before doing any work. Stripe retries aggressively, and a
    # duplicate delivery must not charge state twice.
    try:
        await db.stripe_events.insert_one({
            "_id": event_id, "type": event_type, "received_at": utcnow(), "status": "processing",
        })
    except DuplicateKeyError:
        logger.info("Stripe event %s already handled", event_id)
        return {"success": True, "message": "Already handled", "event": event_id,
                "duplicate": True}

    if event_type not in _HANDLERS:
        await db.stripe_events.update_one(
            {"_id": event_id}, {"$set": {"status": "ignored", "handled_at": utcnow()}})
        return {"success": True, "message": f"No handler for {event_type}", "event": event_id}

    obj = event["data"]["object"]
    try:
        outcome = await _HANDLERS[event_type](obj)
    except Exception as exc:  # noqa: BLE001
        # Leave the row so the failure is visible, but let Stripe retry: the
        # claim is released so the retry can pick the event up again.
        logger.exception("Stripe event %s (%s) failed", event_id, event_type)
        await db.stripe_events.delete_one({"_id": event_id})
        raise BadRequest(f"Could not process {event_type}") from exc

    await db.stripe_events.update_one(
        {"_id": event_id},
        {"$set": {"status": "handled", "outcome": outcome, "handled_at": utcnow()}})
    logger.info("Stripe %s (%s): %s", event_type, event_id, outcome)
    return {"success": True, "message": outcome, "event": event_id, "type": event_type}
