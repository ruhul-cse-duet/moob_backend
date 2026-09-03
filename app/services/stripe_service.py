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
from typing import Any, Dict, List, Optional

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


async def refund_invoice(*, payment_intent_id: Optional[str] = None,
                         charge_id: Optional[str] = None,
                         amount: Optional[float] = None,
                         reason: Optional[str] = None) -> Dict[str, Any]:
    """Refund a settled payment.

    Takes the payment intent (or the charge, for older records) rather than the
    invoice id, because that is what Stripe refunds against. ``amount`` is in
    the account currency and omitted for a full refund.

    Returns a result dict rather than raising, so the caller can record the
    outcome on the invoice either way - a refund that failed at Stripe must not
    leave our own row marked refunded.
    """
    api = _client()
    if api is None:
        return {"success": False, "message": "Stripe is not configured", "refund_id": None}
    if not payment_intent_id and not charge_id:
        return {"success": False, "refund_id": None,
                "message": "This invoice has no Stripe payment to refund. "
                           "Write it off instead if the money was never taken."}

    params: Dict[str, Any] = {}
    if payment_intent_id:
        params["payment_intent"] = payment_intent_id
    else:
        params["charge"] = charge_id
    if amount is not None:
        params["amount"] = int(round(float(amount) * 100))
    if reason in ("duplicate", "fraudulent", "requested_by_customer"):
        params["reason"] = reason

    try:
        refund = await api.Refund.create_async(**params)
    except stripe.InvalidRequestError as exc:
        # Already refunded, or a charge that cannot be. The message is the
        # useful part and none of it is secret.
        return {"success": False, "refund_id": None, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Stripe refund failed for %s", payment_intent_id or charge_id)
        return {"success": False, "refund_id": None, "message": str(exc)}

    return {"success": refund.status in ("succeeded", "pending"),
            "refund_id": refund.id, "status": refund.status,
            "message": f"Refund {refund.status}"}


# ── cards on file ──────────────────────────────────────────────────────────
#
# The card itself never passes through here. The browser turns it into a
# PaymentMethod id with Stripe.js, and these calls only ever move that id
# around - attach it to the customer, name it the default, take it off again.
# What comes back is the brand, the last four digits and the expiry, which is
# all the app needs to let someone tell one card from another.
def _card_summary(payment_method: Any, default_id: Optional[str]) -> Dict[str, Any]:
    card = getattr(payment_method, "card", None) or {}
    get = card.get if isinstance(card, dict) else lambda key: getattr(card, key, None)
    return {
        "id": payment_method.id,
        "brand": (get("brand") or "card").title(),
        "last4": get("last4") or "",
        "exp_month": get("exp_month"),
        "exp_year": get("exp_year"),
        "is_default": payment_method.id == default_id,
    }


async def _default_payment_method(api: Any, customer_id: str) -> Optional[str]:
    customer = await api.Customer.retrieve_async(customer_id)
    settings_ = getattr(customer, "invoice_settings", None) or {}
    value = (settings_.get("default_payment_method")
             if isinstance(settings_, dict)
             else getattr(settings_, "default_payment_method", None))
    # Expanded objects come back whole; only the id is ever wanted here.
    return getattr(value, "id", value)


async def list_payment_methods(customer_id: Optional[str]) -> List[Dict[str, Any]]:
    """Every card saved against this customer, newest first, default flagged.

    An empty list is the honest answer for a workspace that has no Stripe
    customer yet, or when Stripe is unreachable — the subscription screen still
    has to render, and it says "no card on file" rather than failing.
    """
    api = _client()
    if api is None or not customer_id:
        return []
    try:
        default_id = await _default_payment_method(api, customer_id)
        methods = await api.PaymentMethod.list_async(customer=customer_id, type="card")
    except Exception:  # noqa: BLE001
        logger.exception("Could not list cards for Stripe customer %s", customer_id)
        return []
    return [_card_summary(pm, default_id) for pm in methods.data]


async def attach_payment_method(customer_id: str, payment_method_id: str, *,
                                make_default: bool = True) -> Dict[str, Any]:
    """Save a new card against the customer.

    Defaults to making it the one that gets charged: someone who has just typed
    a card in is adding it to use it, and a new card that silently changes
    nothing is the more surprising outcome.
    """
    api = _client()
    if api is None:
        return {"success": False, "message": "Stripe is not configured", "card": None}
    if not customer_id:
        return {"success": False, "card": None,
                "message": "This workspace has no billing account yet."}
    try:
        attached = await api.PaymentMethod.attach_async(payment_method_id,
                                                        customer=customer_id)
        if make_default:
            await api.Customer.modify_async(
                customer_id,
                invoice_settings={"default_payment_method": payment_method_id},
            )
    except stripe.CardError as exc:
        return {"success": False, "card": None,
                "message": exc.user_message or "That card was declined."}
    except stripe.InvalidRequestError as exc:
        return {"success": False, "card": None, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not attach a card to Stripe customer %s", customer_id)
        return {"success": False, "card": None, "message": str(exc)}

    return {"success": True, "message": "Card saved",
            "card": _card_summary(attached, payment_method_id if make_default else None)}


async def set_default_payment_method(customer_id: str,
                                     payment_method_id: str) -> Dict[str, Any]:
    """Choose which saved card the subscription is billed to."""
    api = _client()
    if api is None:
        return {"success": False, "message": "Stripe is not configured"}
    if not customer_id:
        return {"success": False, "message": "This workspace has no billing account yet."}
    try:
        await api.Customer.modify_async(
            customer_id,
            invoice_settings={"default_payment_method": payment_method_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not set the default card on %s", customer_id)
        return {"success": False, "message": str(exc)}
    return {"success": True, "message": "Card updated"}


async def detach_payment_method(customer_id: str,
                                payment_method_id: str) -> Dict[str, Any]:
    """Remove a saved card.

    The last card is kept: detaching the one the subscription bills to is how a
    renewal fails silently a month later, and the fix for "wrong card" is to add
    the right one, not to be left with none.
    """
    api = _client()
    if api is None:
        return {"success": False, "message": "Stripe is not configured"}
    saved = await list_payment_methods(customer_id)
    if len(saved) <= 1:
        return {"success": False,
                "message": "This is the only card on file. Add another one first, "
                           "so the subscription still has something to renew against."}
    try:
        await api.PaymentMethod.detach_async(payment_method_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Could not detach card %s", payment_method_id)
        return {"success": False, "message": str(exc)}

    # Detaching the default leaves Stripe with none, so the next card takes over
    # rather than the renewal quietly having nothing to charge.
    if any(card["id"] == payment_method_id and card["is_default"] for card in saved):
        remaining = next((c for c in saved if c["id"] != payment_method_id), None)
        if remaining:
            await set_default_payment_method(customer_id, remaining["id"])
    return {"success": True, "message": "Card removed"}


async def change_subscription_plan(
    *,
    subscription_id: str,
    plan_code: PlanCode,
    billing_cycle: BillingCycle,
    amount: Optional[float] = None,
    plan_name: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    tenant_id: Optional[str] = None,
    charge_now: bool = True,
) -> Dict[str, Any]:
    """Move a live subscription onto the Price for another plan or cycle.

    This is what makes an upgrade cost money. Stripe replaces the subscription
    item's Price and - because of ``always_invoice`` - immediately bills the
    prorated difference for the rest of the current period, rather than letting
    the bigger plan ride free until the next renewal.

    ``error_if_incomplete`` is the important half: if the card on file cannot
    pay that proration invoice, Stripe raises instead of parking the
    subscription in ``incomplete``. The caller gets ``success: False`` and must
    leave its own records on the old plan - a workspace holding Enterprise
    seats against a Starter subscription is the exact failure this avoids.

    ``charge_now=False`` switches the Price without billing anything: the
    subscription simply renews at the new plan next period. That is for the
    super admin's override - a comp or a migration, where somebody has already
    decided what this customer pays - and never for a customer-facing upgrade.

    Returns a result dict rather than raising, so the route can turn a decline
    into a 400 the owner can act on.
    """
    api = _client()
    if api is None:
        return {"success": False, "message": "Stripe is not configured",
                "subscription_id": None, "price_id": None}
    if not subscription_id:
        return {"success": False, "subscription_id": None, "price_id": None,
                "message": "This workspace has no Stripe subscription to move."}

    price = price_id(plan_code, billing_cycle)
    if not price and amount is not None:
        price = await ensure_price(plan_code, billing_cycle, amount, plan_name)
    if not price:
        return {"success": False, "subscription_id": subscription_id, "price_id": None,
                "message": (f"No Stripe Price is available for the {plan_code.value} plan "
                            f"billed {billing_cycle.value}. The plan was not changed.")}

    metadata = {"plan_code": plan_code.value, "billing_cycle": billing_cycle.value,
                "tenant_id": tenant_id or ""}
    try:
        current = await api.Subscription.retrieve_async(subscription_id)
        # Two traps in one line. ``current.items`` is dict.items, so the
        # subscription's items have to be subscripted - but what comes back is a
        # ListObject, which raises on ``.get`` rather than behaving like a dict.
        # ``.data`` is the only safe way through.
        items = list(getattr(current["items"], "data", None) or [])
        if not items:
            return {"success": False, "subscription_id": subscription_id, "price_id": None,
                    "message": "That Stripe subscription has no billable item."}
        if items[0]["price"]["id"] == price:
            # Same Price already - modifying would invoice a zero proration and
            # muddy the billing history for what is, to Stripe, a no-op.
            return {"success": True, "subscription_id": subscription_id, "price_id": price,
                    "status": current["status"], "message": "Already on this price",
                    "current_period_end": (current.get("current_period_end")
                                           or items[0].get("current_period_end")),
                    "latest_invoice_id": None}

        options: Dict[str, Any] = {}
        if idempotency_key:
            options["idempotency_key"] = idempotency_key
        updated = await api.Subscription.modify_async(
            subscription_id,
            items=[{"id": items[0]["id"], "price": price}],
            proration_behavior="always_invoice" if charge_now else "none",
            payment_behavior="error_if_incomplete",
            metadata=metadata,
            expand=["latest_invoice"],
            **options,
        )
    except stripe.CardError as exc:
        return {"success": False, "subscription_id": subscription_id, "price_id": None,
                "message": exc.user_message or "The card on file was declined."}
    except Exception as exc:  # noqa: BLE001 - surface the reason, never leak a traceback
        logger.exception("Could not move subscription %s onto %s", subscription_id, price)
        return {"success": False, "subscription_id": subscription_id, "price_id": None,
                "message": str(exc)}

    invoice = updated.get("latest_invoice")
    status = updated["status"]
    # Recent API versions moved the period onto the subscription item, so read
    # it there when the subscription itself no longer carries one.
    period_end = updated.get("current_period_end")
    if period_end is None:
        moved = list(getattr(updated["items"], "data", None) or [])
        period_end = moved[0].get("current_period_end") if moved else None
    return {
        "success": status in ("active", "trialing"),
        "message": f"Subscription {status}",
        "subscription_id": subscription_id,
        "price_id": price,
        "status": status,
        "current_period_end": period_end,
        "latest_invoice_id": (invoice.get("id") if isinstance(invoice, dict) else invoice),
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
