"""admin/Billing.tsx — platform revenue across every tenant.  [INFERRED]"""
from datetime import timedelta
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, model_validator

from fastapi import APIRouter, Body, Depends, Query

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import (
    OPEN_PLATFORM_INVOICE_STATUSES,
    AuditAction,
    BillingCycle,
    PlanCode,
    PlatformInvoiceStatus,
    TenantStatus,
)
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.admin.deps import require_billing_admin
from app.modules.subscriptions.plans import (
    PLAN_PRICE_OVERRIDES_KEY,
    plan_catalogue,
    plan_overrides,
    save_plan_overrides,
)
from app.schemas.common import PageParams
from app.services import audit, invoices as invoice_service, stripe_service
from app.services.pagination import paginate

router = APIRouter(prefix="/billing", tags=["Super Admin · Billing"])


class PlanPriceUpdate(BaseModel):
    monthly_price: Optional[float] = Field(None, ge=0)
    annual_price: Optional[float] = Field(None, ge=0)

    @model_validator(mode="after")
    def has_at_least_one_price(self):
        if self.monthly_price is None and self.annual_price is None:
            raise ValueError("Provide at least one of monthly_price or annual_price")
        return self


class PlanPricing(BaseModel):
    plan_code: PlanCode
    name: str
    monthly_price: float
    annual_price: float
    currency: str = "USD"
    # Present on a price update: which Stripe Prices now back this plan, and
    # whether the sync succeeded. Absent when simply reading the catalogue.
    stripe: Optional[Dict[str, Any]] = None


class SubscriptionPlan(BaseModel):
    plan_code: PlanCode
    name: str
    tagline: str
    monthly_price: float
    annual_price: float
    recommended: bool
    consultant_seats: Optional[int] = None
    partner_seats: Optional[int] = None
    active_case_limit: Optional[int] = None
    features: List[str]
    flags: Dict[str, bool]
    currency: str = "USD"


class SubscriptionPlanUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    tagline: Optional[str] = Field(None, min_length=1, max_length=180)
    monthly_price: Optional[float] = Field(None, ge=0)
    annual_price: Optional[float] = Field(None, ge=0)
    recommended: Optional[bool] = None
    consultant_seats: Optional[int] = Field(None, ge=0)
    partner_seats: Optional[int] = Field(None, ge=0)
    active_case_limit: Optional[int] = Field(None, ge=0)
    features: Optional[List[str]] = None
    flags: Optional[Dict[str, bool]] = None

    @model_validator(mode="after")
    def has_at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("Provide at least one plan field to update")
        return self


def _plan_pricing(plan_code: PlanCode, plan: dict) -> dict:
    return {
        "plan_code": plan_code.value,
        "name": plan["name"],
        "monthly_price": plan["monthly_price"],
        "annual_price": plan["annual_price"],
        "currency": "USD",
    }


def _subscription_plan(plan_code: PlanCode, plan: dict) -> dict:
    return {
        "plan_code": plan_code.value,
        "name": plan["name"],
        "tagline": plan["tagline"],
        "monthly_price": plan["monthly_price"],
        "annual_price": plan["annual_price"],
        "recommended": plan["recommended"],
        "consultant_seats": plan["consultant_seats"],
        "partner_seats": plan["partner_seats"],
        "active_case_limit": plan["active_case_limit"],
        "features": plan["features"],
        "flags": plan["flags"],
        "currency": "USD",
    }


def _monthly_value(sub: dict) -> float:
    amount = sub.get("amount", 0) or 0
    return amount / 12 if sub.get("billing_cycle") == BillingCycle.ANNUAL.value else amount


@router.get("/plans", response_model=list[SubscriptionPlan],
            summary="List editable subscription plans")
async def list_subscription_plans(user: CurrentUser = Depends(require_billing_admin)):
    plans = await plan_catalogue()
    return [_subscription_plan(code, plans[code]) for code in PlanCode]


@router.get("/plans/{plan_code}", response_model=SubscriptionPlan,
            summary="Get one editable subscription plan")
async def get_subscription_plan(plan_code: PlanCode,
                                user: CurrentUser = Depends(require_billing_admin)):
    plans = await plan_catalogue()
    return _subscription_plan(plan_code, plans[plan_code])


@router.get("/plans/{plan_code}/price", response_model=PlanPricing,
            summary="Get one editable subscription plan price")
async def get_plan_price(plan_code: PlanCode,
                         user: CurrentUser = Depends(require_billing_admin)):
    plans = await plan_catalogue()
    return _plan_pricing(plan_code, plans[plan_code])


@router.patch("/plans/{plan_code}", response_model=SubscriptionPlan,
              summary="Update a subscription plan")
async def update_subscription_plan(plan_code: PlanCode, payload: SubscriptionPlanUpdate,
                                   user: CurrentUser = Depends(require_super_admin)):
    overrides = await plan_overrides()
    plan_override = overrides.get(plan_code.value, {}).copy()

    updates = payload.model_dump(exclude_unset=True)
    for field in ("name", "tagline"):
        if field in updates and isinstance(updates[field], str):
            updates[field] = updates[field].strip()
    if "features" in updates and updates["features"] is not None:
        updates["features"] = [item.strip() for item in updates["features"] if item.strip()]

    plan_override.update(updates)
    overrides[plan_code.value] = plan_override
    cleaned = await save_plan_overrides(overrides, updated_by=user.id, updated_at=utcnow())

    await audit.record(
        action=AuditAction.ADMIN_ACTION,
        actor_id=user.id,
        actor_email=user.email,
        actor_role="super_admin",
        subject=f"plan:{plan_code.value}",
        meta=cleaned.get(plan_code.value, {}),
    )

    plans = await plan_catalogue()
    return _subscription_plan(plan_code, plans[plan_code])


@router.get("/overview", summary="MRR, ARR, plan mix and churn")
async def overview(user: CurrentUser = Depends(require_billing_admin)):
    db = platform_db()
    plans = await plan_catalogue()
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
        "plan_catalogue": {code.value: {"monthly": plans[code]["monthly_price"],
                                        "annual": plans[code]["annual_price"]}
                           for code in PlanCode},
    }


@router.put("/plans/{plan_code}/price", response_model=PlanPricing,
            summary="Update a subscription plan price")
async def update_plan_price(plan_code: PlanCode, payload: PlanPriceUpdate,
                            user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    row = await db.platform_settings.find_one({"key": PLAN_PRICE_OVERRIDES_KEY})
    value = row.get("value") if row else {}
    overrides = value.copy() if isinstance(value, dict) else {}
    current = overrides.get(plan_code.value, {})
    plan_override = current.copy() if isinstance(current, dict) else {}

    if payload.monthly_price is not None:
        plan_override["monthly_price"] = round(payload.monthly_price, 2)
    if payload.annual_price is not None:
        plan_override["annual_price"] = round(payload.annual_price, 2)
    overrides[plan_code.value] = plan_override

    await db.platform_settings.update_one(
        {"key": PLAN_PRICE_OVERRIDES_KEY},
        {"$set": {"value": overrides, "updated_by": user.id, "updated_at": utcnow()}},
        upsert=True,
    )
    current_overrides = await plan_overrides()
    current_plan_override = current_overrides.get(plan_code.value, {}).copy()
    current_plan_override.update(plan_override)
    current_overrides[plan_code.value] = current_plan_override
    await save_plan_overrides(current_overrides, updated_by=user.id, updated_at=utcnow())

    await audit.record(
        action=AuditAction.ADMIN_ACTION,
        actor_id=user.id,
        actor_email=user.email,
        actor_role="super_admin",
        subject=f"plan_price:{plan_code.value}",
        meta=plan_override,
    )

    plans = await plan_catalogue()
    pricing = _plan_pricing(plan_code, plans[plan_code])

    # A Stripe Price is immutable, so a new amount means a new Price. Create it
    # now rather than at the next signup, so the admin finds out here if Stripe
    # rejects it. Existing subscribers stay on the Price they signed up with -
    # this never touches a live subscription.
    pricing["stripe"] = await _sync_stripe_prices(plan_code, plans[plan_code])
    return pricing


async def _sync_stripe_prices(plan_code: PlanCode, plan: dict) -> Dict[str, Optional[str]]:
    if not stripe_service.configured():
        return {"synced": False,
                "detail": "Stripe is not configured; prices apply to the catalogue only."}
    result: Dict[str, Optional[str]] = {"synced": True,
                                        "detail": "New signups bill at the new price. "
                                                  "Existing subscribers keep their current price."}
    for cycle, field in ((BillingCycle.MONTHLY, "monthly_price"),
                         (BillingCycle.ANNUAL, "annual_price")):
        price_id = await stripe_service.ensure_price(
            plan_code, cycle, plan[field], plan.get("name"))
        result[cycle.value] = price_id
        if price_id is None:
            result["synced"] = False
            result["detail"] = (f"Could not create a Stripe Price for {cycle.value}. "
                                "The catalogue was updated; check the server logs.")
    return result


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


# --------------------------------------------------------------------------- #
# Invoices — admin/Billing.tsx "Invoices" tab
#
# What an organization owes the platform. Rows come from the Stripe webhook;
# an administrator adds one only for money taken outside Stripe.
#
# The tab is not a read-only ledger. A failed payment puts the workspace into
# PAST_DUE, which makes it read-only for the consultant, their partners and
# their clients - so resolving the invoice is how a paying customer gets their
# workspace back, and these are the endpoints behind that.
# --------------------------------------------------------------------------- #


class ManualInvoice(BaseModel):
    """An invoice for money taken outside Stripe - a bank transfer, usually."""

    tenant_id: str
    amount: float = Field(gt=0)
    billing_cycle: BillingCycle = BillingCycle.MONTHLY
    plan_code: Optional[PlanCode] = None
    payment_method: str = Field(default="Bank transfer", max_length=60)
    status: PlatformInvoiceStatus = PlatformInvoiceStatus.PENDING
    notes: Optional[str] = Field(None, max_length=500)


async def _load_invoice(invoice_id: str) -> Dict[str, Any]:
    doc = await platform_db().platform_invoices.find_one({"_id": oid(invoice_id)})
    if not doc:
        raise NotFound("Invoice not found")
    return doc


async def _settle(invoice: Dict[str, Any], *, status: PlatformInvoiceStatus,
                  user: CurrentUser, detail: str,
                  extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Move an invoice to a closed state, and say so in the audit trail.

    Every one of these is a money decision made by a person, so none of them
    happens without a record of who made it.
    """
    db = platform_db()
    updates = {"status": status.value, "updated_at": utcnow(),
               "resolved_by": user.id, "resolved_at": utcnow(), **(extra or {})}
    await db.platform_invoices.update_one({"_id": invoice["_id"]}, {"$set": updates})
    await audit.record(
        action=AuditAction.ADMIN_ACTION, actor_id=user.id, actor_email=user.email,
        actor_role="super_admin", tenant_id=invoice.get("tenant_id"),
        subject=f"Invoice {invoice.get('reference')}", detail=detail,
    )
    return serialize({**invoice, **updates})


@router.get("/invoices", summary="Every platform invoice — the Invoices tab")
async def list_invoices(status: Optional[PlatformInvoiceStatus] = Query(None),
                        tenant_id: Optional[str] = Query(None),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(require_billing_admin)):
    query: Dict[str, Any] = {}
    if status:
        query["status"] = status.value
    if tenant_id:
        query["tenant_id"] = tenant_id

    db = platform_db()
    page = await paginate(db, "platform_invoices", query, params,
                          sort=[("created_at", -1)])
    # The counters in the page header. Computed over every invoice, not the
    # current page, or paging would change the headline numbers.
    page["summary"] = {
        "paying_organizations": len(
            await db.platform_invoices.distinct(
                "tenant_id", {"status": PlatformInvoiceStatus.PAID.value})
        ),
        "failed_payments": await db.platform_invoices.count_documents(
            {"status": PlatformInvoiceStatus.FAILED.value}),
        "pending_payments": await db.platform_invoices.count_documents(
            {"status": PlatformInvoiceStatus.PENDING.value}),
    }
    return page


@router.get("/invoices/{invoice_id}", summary="One invoice")
async def get_invoice(invoice_id: str,
                      user: CurrentUser = Depends(require_billing_admin)):
    return serialize(await _load_invoice(invoice_id))


@router.post("/invoices", status_code=201,
             summary="Record a payment taken outside Stripe (bank transfer)")
async def create_manual_invoice(payload: ManualInvoice,
                                user: CurrentUser = Depends(require_billing_admin)):
    db = platform_db()
    tenant = await db.tenants.find_one({"_id": oid(payload.tenant_id)})
    if not tenant:
        raise NotFound("Organization not found")

    now = utcnow()
    doc: Dict[str, Any] = {
        "reference": await invoice_service.next_reference(),
        "tenant_id": payload.tenant_id,
        "organization_name": tenant.get("name"),
        "amount": round(payload.amount, 2),
        "currency": (stripe_service.settings.STRIPE_CURRENCY or "usd").lower(),
        "status": payload.status.value,
        "plan_code": payload.plan_code.value if payload.plan_code else tenant.get("plan_code"),
        "billing_cycle": payload.billing_cycle.value,
        "payment_method": payload.payment_method,
        "notes": payload.notes,
        # No Stripe ids on purpose: this money never went through Stripe, so a
        # refund here has nothing to call and must be a write-off instead.
        "created_by": user.id,
        "issued_at": now,
        "created_at": now,
        "updated_at": now,
    }
    if payload.status is PlatformInvoiceStatus.PAID:
        doc["paid_at"] = now
    doc["_id"] = (await db.platform_invoices.insert_one(doc)).inserted_id

    await audit.record(
        action=AuditAction.ADMIN_ACTION, actor_id=user.id, actor_email=user.email,
        actor_role="super_admin", tenant_id=payload.tenant_id,
        subject=f"Invoice {doc['reference']}",
        detail=f"raised manually for {payload.amount} ({payload.payment_method})",
    )
    return serialize(doc)


@router.post("/invoices/{invoice_id}/mark-paid",
             summary="Settle a failed or pending invoice by hand")
async def mark_invoice_paid(invoice_id: str,
                            note: Optional[str] = Body(None, embed=True),
                            user: CurrentUser = Depends(require_billing_admin)):
    """For money that arrived outside Stripe, or a failure resolved directly
    with the customer.

    Deliberately does **not** reactivate the organization. Settling the invoice
    and reopening a workspace are two decisions - the customer may owe more than
    this one invoice - so reactivation stays an explicit call to
    `POST /admin/organizations/{id}/status`, which is what the UI banner
    describes.
    """
    invoice = await _load_invoice(invoice_id)
    if invoice["status"] not in {s.value for s in OPEN_PLATFORM_INVOICE_STATUSES}:
        raise Conflict(f"This invoice is already {invoice['status']}")
    return await _settle(
        invoice, status=PlatformInvoiceStatus.PAID, user=user,
        detail=f"marked paid by hand{f' - {note}' if note else ''}",
        extra={"paid_at": utcnow(), "notes": note or invoice.get("notes")},
    )


@router.post("/invoices/{invoice_id}/write-off",
             summary="Give up on an invoice without taking the money")
async def write_off_invoice(invoice_id: str,
                            reason: str = Body(embed=True, min_length=3, max_length=500),
                            user: CurrentUser = Depends(require_billing_admin)):
    """The other half of the banner's "resolve or write off".

    A reason is required rather than optional: this is revenue being given up,
    and six months later the only explanation will be this field.
    """
    invoice = await _load_invoice(invoice_id)
    if invoice["status"] not in {s.value for s in OPEN_PLATFORM_INVOICE_STATUSES}:
        raise Conflict(f"This invoice is already {invoice['status']}")
    return await _settle(
        invoice, status=PlatformInvoiceStatus.WRITTEN_OFF, user=user,
        detail=f"written off: {reason}", extra={"write_off_reason": reason},
    )


@router.post("/invoices/{invoice_id}/refund",
             summary="Refund a paid invoice through Stripe")
async def refund_invoice(invoice_id: str,
                         amount: Optional[float] = Body(None, embed=True, gt=0),
                         reason: Optional[str] = Body(None, embed=True),
                         user: CurrentUser = Depends(require_billing_admin)):
    """Refund at Stripe first, and only then mark our own row.

    The other order is the one failure the customer notices on their statement:
    an invoice showing "refunded" here while Stripe never returned the money.
    """
    invoice = await _load_invoice(invoice_id)
    if invoice["status"] != PlatformInvoiceStatus.PAID.value:
        raise Conflict(f"Only a paid invoice can be refunded; this one is "
                       f"{invoice['status']}")
    if amount is not None and amount > invoice["amount"]:
        raise BadRequest(
            f"Cannot refund {amount} against an invoice of {invoice['amount']}")

    result = await stripe_service.refund_invoice(
        payment_intent_id=invoice.get("stripe_payment_intent_id"),
        charge_id=invoice.get("stripe_charge_id"),
        amount=amount, reason=reason,
    )
    if not result["success"]:
        raise BadRequest(result["message"])

    refunded = amount if amount is not None else invoice["amount"]
    return await _settle(
        invoice, status=PlatformInvoiceStatus.REFUNDED, user=user,
        detail=f"refunded {refunded} {invoice.get('currency', 'usd').upper()}"
               f"{f' - {reason}' if reason else ''}",
        extra={"refunded_at": utcnow(), "refunded_amount": refunded,
               "stripe_refund_id": result.get("refund_id")},
    )
