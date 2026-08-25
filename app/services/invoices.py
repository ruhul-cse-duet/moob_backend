"""
Platform invoices - what an organization owes WebImove.

Not to be confused with `app/modules/billing`, which is a consultant invoicing
their own client and never touches Stripe.

Rows are written from the Stripe webhook, so this file is the answer to "where
does the Invoices tab get its data": every `invoice.paid` and
`invoice.payment_failed` Stripe delivers becomes one row here. An administrator
can also raise one by hand for a payment taken offline - a bank transfer Stripe
never sees.

Recording is idempotent on the Stripe invoice id. Stripe retries any non-2xx
delivery and can send the same event twice, and a duplicate here would show the
organization as having paid twice.
"""
import logging
from typing import Any, Dict, Optional

from pymongo.errors import DuplicateKeyError

from app.core.enums import PlatformInvoiceStatus
from app.core.utils import build_reference, utcnow
from app.db.indexes import next_sequence
from app.db.mongo import platform_db

logger = logging.getLogger(__name__)

COLLECTION = "platform_invoices"

#: Human references start here so they look like an established business rather
#: than INV-001 on the first customer.
_REFERENCE_START = 2000


async def next_reference() -> str:
    return build_reference("INV", await next_sequence(platform_db(), "platform_invoice",
                                                      start=_REFERENCE_START))


def describe_card(obj: Dict[str, Any]) -> Optional[str]:
    """"Visa •••• 4417" from whatever Stripe attached to the invoice.

    Stripe has moved this field more than once between API versions, so each
    known location is tried rather than assuming one shape.
    """
    for path in (("payment_method_details", "card"),
                 ("charges", "data", 0, "payment_method_details", "card")):
        node: Any = obj
        for key in path:
            if isinstance(node, list):
                node = node[key] if len(node) > key else None
            elif isinstance(node, dict):
                node = node.get(key)
            else:
                node = None
            if node is None:
                break
        if isinstance(node, dict) and node.get("last4"):
            brand = str(node.get("brand") or "Card").title()
            return f"{brand} •••• {node['last4']}"
    return None


async def record_from_stripe(
    *,
    tenant: Dict[str, Any],
    obj: Dict[str, Any],
    status: PlatformInvoiceStatus,
) -> Optional[Dict[str, Any]]:
    """Write (or update) the invoice behind a Stripe billing event.

    Returns the stored row, or None when this event has already been recorded.
    """
    db = platform_db()
    stripe_invoice_id = obj.get("id")

    existing = None
    if stripe_invoice_id:
        existing = await db[COLLECTION].find_one({"stripe_invoice_id": stripe_invoice_id})

    now = utcnow()
    amount_cents = (obj.get("amount_paid") or obj.get("amount_due")
                    or obj.get("total") or 0)
    fields: Dict[str, Any] = {
        "tenant_id": str(tenant["_id"]),
        # Denormalised so the Invoices list renders without a lookup per row,
        # and so a deleted organization still has a readable billing history.
        "organization_name": tenant.get("name"),
        "amount": round(float(amount_cents) / 100, 2),
        "currency": (obj.get("currency") or "usd").lower(),
        "status": status.value,
        "plan_code": (obj.get("metadata") or {}).get("plan_code")
                     or tenant.get("plan_code"),
        "billing_cycle": (obj.get("metadata") or {}).get("billing_cycle")
                         or tenant.get("billing_cycle"),
        "stripe_invoice_id": stripe_invoice_id,
        "stripe_payment_intent_id": obj.get("payment_intent"),
        "stripe_charge_id": obj.get("charge"),
        "payment_method": describe_card(obj) or "Card",
        "hosted_invoice_url": obj.get("hosted_invoice_url"),
        "updated_at": now,
    }
    if status is PlatformInvoiceStatus.PAID:
        fields["paid_at"] = now
    elif status is PlatformInvoiceStatus.FAILED:
        fields["failed_at"] = now

    if existing:
        # A retried delivery, or an invoice that failed and then cleared. Only
        # the status moves; the reference and issue date are what a customer
        # quotes, so they stay put.
        await db[COLLECTION].update_one({"_id": existing["_id"]}, {"$set": fields})
        return {**existing, **fields}

    fields["reference"] = await next_reference()
    fields["issued_at"] = now
    fields["created_at"] = now
    try:
        result = await db[COLLECTION].insert_one(fields)
    except DuplicateKeyError:
        # Two deliveries of the same event arriving together. The unique index
        # on stripe_invoice_id is what makes that safe.
        logger.info("Invoice for Stripe %s was already recorded", stripe_invoice_id)
        return None
    return {**fields, "_id": result.inserted_id}
