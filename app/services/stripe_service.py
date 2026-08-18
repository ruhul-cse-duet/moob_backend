"""Stripe for the platform's own subscriptions - consultant pays WebImove.

Consultant->client invoicing lives in ``app/modules/billing`` and never touches
Stripe; do not mix the two.

Two things live here:

* **Subscription creation.** A real recurring Stripe Subscription, which is what
  makes renewals happen at all. It needs a ``payment_method_id`` produced by
  Stripe.js in the browser - the card number must never reach this server.
* **Webhook verification.** Stripe signs every event; ``construct_event`` is the
  only thing standing between the webhook route and anyone who can POST to it.

Every call is a no-op returning ``None`` when Stripe is not configured, so a
development environment without keys still boots and serves the rest of the API.
"""
import logging
from typing import Any, Dict, Optional

import stripe

from app.core.config import settings
from app.core.enums import BillingCycle, PlanCode
from app.core.utils import utcnow

logger = logging.getLogger(__name__)

# Values that ship in .env.example / config defaults and are not real keys.
_PLACEHOLDER_MARKERS = ("...", "Mockup")


def _is_placeholder(value: Optional[str]) -> bool:
    """The .env.example values are copied verbatim more often than not."""
    value = (value or "").strip()
    return not value or any(marker in value for marker in _PLACEHOLDER_MARKERS)


def configured() -> bool:
    """True when a usable secret key is set - not one of the shipped placeholders."""
    return not _is_placeholder(settings.STRIPE_SECRET_KEY)


def _client() -> Optional[Any]:
    if not configured():
        return None
    stripe.api_key = settings.STRIPE_SECRET_KEY
    return stripe


def price_id(plan_code: PlanCode, billing_cycle: BillingCycle) -> Optional[str]:
    """The Stripe Price for a plan/cycle, or None if the operator never mapped one."""
    code = plan_code.value if isinstance(plan_code, PlanCode) else str(plan_code)
    cycle = billing_cycle.value if isinstance(billing_cycle, BillingCycle) else str(billing_cycle)
    value = (settings.STRIPE_PRICES or {}).get(f"{code}_{cycle}")
    # An un-edited "price_..." from .env.example is worse than no mapping at all:
    # it would send signup down the subscription path only to fail at Stripe.
    return None if _is_placeholder(value) else value


def supports_subscriptions(plan_code: PlanCode, billing_cycle: BillingCycle) -> bool:
    """Cheap check for the env-mapped path. ``ensure_price`` can still make one."""
    return configured() and bool(price_id(plan_code, billing_cycle))


# --------------------------------------------------------------------------- #
# Product / Price provisioning
#
# The super admin sets prices in the platform dashboard, so the plan catalogue in
# `platform_settings` is the source of truth - not Stripe. A Stripe Price is
# immutable, though: you cannot edit `unit_amount`. So when a price changes we
# create a *new* Price and point new subscriptions at it. Existing subscribers
# keep the Price they signed up on, which is both the SaaS norm and the safe
# default - silently raising someone's recurring charge is a legal problem in
# several jurisdictions.
#
# Prices are cached by (plan, cycle, amount), so flipping a price back to an
# earlier value reuses that Price instead of piling up duplicates in Stripe.
# --------------------------------------------------------------------------- #
_INTERVALS = {BillingCycle.MONTHLY: "month", BillingCycle.ANNUAL: "year"}


async def _ensure_product(plan_code: PlanCode, plan_name: str) -> Optional[str]:
    from app.db.mongo import platform_db

    db = platform_db()
    cached = await db.stripe_products.find_one({"_id": plan_code.value})
    if cached and cached.get("product_id"):
        return cached["product_id"]

    api = _client()
    if api is None:
        return None
    product = await api.Product.create_async(
        name=f"WebImove {plan_name}",
        metadata={"plan_code": plan_code.value},
        idempotency_key=f"product_{plan_code.value}",
    )
    await db.stripe_products.update_one(
        {"_id": plan_code.value},
        {"$set": {"product_id": product.id, "name": plan_name, "created_at": utcnow()}},
        upsert=True,
    )
    return product.id


async def ensure_price(plan_code: PlanCode, billing_cycle: BillingCycle,
                       amount: float, plan_name: Optional[str] = None) -> Optional[str]:
    """The Stripe Price for this plan at this exact amount, creating it if new.

    Returns None when Stripe is not configured, or when the call fails - callers
    must treat that as "no recurring billing available" rather than assuming a
    price exists.
    """
    if not configured():
        return None

    # An explicit env mapping wins, so an operator can pin a hand-made Price.
    mapped = price_id(plan_code, billing_cycle)
    if mapped:
        return mapped

    from app.db.mongo import platform_db

    db = platform_db()
    cents = int(round(float(amount) * 100))
    currency = (settings.STRIPE_CURRENCY or "usd").lower()
    key = f"{plan_code.value}_{billing_cycle.value}_{currency}_{cents}"

    cached = await db.stripe_prices.find_one({"_id": key})
    if cached and cached.get("price_id"):
        return cached["price_id"]

    api = _client()
    try:
        product_id = await _ensure_product(plan_code, plan_name or plan_code.value.title())
        if not product_id:
            return None
        price = await api.Price.create_async(
            product=product_id,
            unit_amount=cents,
            currency=currency,
            recurring={"interval": _INTERVALS[billing_cycle]},
            metadata={"plan_code": plan_code.value, "billing_cycle": billing_cycle.value},
            idempotency_key=f"price_{key}",
        )
    except Exception:  # noqa: BLE001 - never break signup or the admin panel
        logger.exception("Could not create a Stripe Price for %s", key)
        return None

    await db.stripe_prices.update_one(
        {"_id": key},
        {"$set": {"price_id": price.id, "product_id": product_id, "amount": amount,
                  "currency": currency, "plan_code": plan_code.value,
                  "billing_cycle": billing_cycle.value, "created_at": utcnow()}},
        upsert=True,
    )
    logger.info("Created Stripe Price %s for %s at %.2f %s",
                price.id, plan_code.value, amount, currency.upper())
    return price.id


async def create_subscription(
    *,
    email: str,
    full_name: str,
    plan_code: PlanCode,
    billing_cycle: BillingCycle,
    payment_method_id: str,
    idempotency_key: str,
    amount: Optional[float] = None,
    plan_name: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the customer and their recurring subscription.

    ``idempotency_key`` is derived from the signup, so a retried payment attaches
    to the original customer and subscription instead of creating a second one.

    ``amount`` is the price the customer was actually quoted. Passing it lets the
    Price be created on demand from the admin-managed catalogue, so a price the
    super admin set minutes ago still bills correctly.
    """
    api = _client()
    price = price_id(plan_code, billing_cycle)
    if not price and amount is not None:
        price = await ensure_price(plan_code, billing_cycle, amount, plan_name)
    if api is None or not price:
        return {"success": False, "message": "Stripe subscriptions are not configured",
                "customer_id": None, "subscription_id": None, "reference": None}

    metadata = {"plan_code": str(plan_code.value if isinstance(plan_code, PlanCode) else plan_code),
                "billing_cycle": str(billing_cycle.value if isinstance(billing_cycle, BillingCycle)
                                     else billing_cycle),
                "tenant_id": tenant_id or "",
                "owner_email": email}
    try:
        customer = await api.Customer.create_async(
            email=email,
            name=full_name,
            payment_method=payment_method_id,
            invoice_settings={"default_payment_method": payment_method_id},
            metadata=metadata,
            idempotency_key=f"{idempotency_key}_customer",
        )
        subscription = await api.Subscription.create_async(
            customer=customer.id,
            items=[{"price": price}],
            metadata=metadata,
            expand=["latest_invoice"],
            idempotency_key=f"{idempotency_key}_subscription",
        )
    except stripe.CardError as exc:
        return {"success": False, "message": exc.user_message or "Card declined",
                "customer_id": None, "subscription_id": None, "reference": None}
    except Exception as exc:  # noqa: BLE001 - surface the reason, never leak a traceback
        logger.exception("Stripe subscription creation failed for %s", email)
        return {"success": False, "message": str(exc),
                "customer_id": None, "subscription_id": None, "reference": None}

    invoice = getattr(subscription, "latest_invoice", None)
    return {
        "success": subscription.status in ("active", "trialing"),
        "message": f"Subscription {subscription.status}",
        "customer_id": customer.id,
        "subscription_id": subscription.id,
        "status": subscription.status,
        "reference": getattr(invoice, "id", None) or subscription.id,
        "current_period_end": getattr(subscription, "current_period_end", None),
    }


async def cancel_subscription(subscription_id: str) -> bool:
    """Cancel at Stripe. The webhook is what updates our own records."""
    api = _client()
    if api is None or not subscription_id:
        return False
    try:
        await api.Subscription.cancel_async(subscription_id)
        return True
    except Exception:  # noqa: BLE001 - cancelling locally must not depend on Stripe
        logger.exception("Could not cancel Stripe subscription %s", subscription_id)
        return False


def construct_event(payload: bytes, signature: Optional[str]) -> stripe.Event:
    """Verify Stripe's signature and return the event.

    Raises ``ValueError`` for a malformed body and
    ``stripe.SignatureVerificationError`` when the signature does not match -
    the caller must translate both into a 400 and process nothing.
    """
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise ValueError("STRIPE_WEBHOOK_SECRET is not set; refusing to trust this event")
    return stripe.Webhook.construct_event(
        payload, signature or "", settings.STRIPE_WEBHOOK_SECRET
    )
