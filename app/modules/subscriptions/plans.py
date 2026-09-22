"""Plan catalogue exactly as designed on the subscription screen."""
from copy import deepcopy
from typing import Any, Dict, List

from app.core.enums import BillingCycle, PlanCode
from app.core.config import settings
from app.core.i18n import translate
from app.db.mongo import platform_db

#: The dictionary key behind each piece of plan copy. The English text stays in
#: `PLANS` as the fallback, so a missing translation still reads correctly.
PLAN_TAGLINE_KEYS = {'For solo consultants opening their first workspace.': 'plan.starter.tagline', 'For growing practices with a partner network.': 'plan.professional.tagline', 'For multi-office firms with compliance needs.': 'plan.enterprise.tagline'}

PLAN_FEATURE_KEYS = {'1 consultant seat · Up to 3 partners': 'plan.starter.feature1', 'Up to 25 active cases': 'plan.starter.feature2', 'Client request queue and document centre': 'plan.starter.feature3', 'Email support within 2 business days': 'plan.starter.feature4', '5 consultant seats · Up to 15 partners': 'plan.professional.feature1', 'Unlimited cases': 'plan.professional.feature2', 'AI document review and form drafting': 'plan.professional.feature3', 'Partner task delegation and deliverables': 'plan.professional.feature4', 'Priority support within 4 hours': 'plan.professional.feature5', 'Unlimited consultant seats · Unlimited partners': 'plan.enterprise.feature1', 'Everything in Professional': 'plan.enterprise.feature2', 'Multi-office workspaces and audit trail': 'plan.enterprise.feature3', 'Custom data retention and SSO': 'plan.enterprise.feature4', 'Dedicated success manager': 'plan.enterprise.feature5'}


def localise(plans: Dict[Any, dict], language: str | None) -> List[dict]:
    """The catalogue in one language, ready to serve."""
    out: List[dict] = []
    for plan in plans.values():
        plan = deepcopy(plan)
        tagline_key = PLAN_TAGLINE_KEYS.get(plan.get("tagline"))
        if tagline_key:
            plan["tagline"] = translate(tagline_key, language) or plan["tagline"]
        plan["currency"] = (settings.STRIPE_CURRENCY or "eur").upper()
        plan["features"] = [
            (translate(PLAN_FEATURE_KEYS[f], language) or f)
            if f in PLAN_FEATURE_KEYS else f
            for f in plan.get("features", [])
        ]
        out.append(plan)
    return out

PLANS = {
    PlanCode.STARTER: {
        "code": PlanCode.STARTER,
        "name": "Starter",
        "tagline": "For solo consultants opening their first workspace.",
        "monthly_price": 49.0,
        "annual_price": 470.0,
        "recommended": False,
        "consultant_seats": 1,
        "partner_seats": 3,
        "active_case_limit": 25,
        "features": [
            "1 consultant seat · Up to 3 partners",
            "Up to 25 active cases",
            "Client request queue and document centre",
            "Email support within 2 business days",
        ],
        "flags": {"ai_document_review": False, "partner_delegation": False, "sso": False},
    },
    PlanCode.PROFESSIONAL: {
        "code": PlanCode.PROFESSIONAL,
        "name": "Professional",
        "tagline": "For growing practices with a partner network.",
        "monthly_price": 129.0,
        "annual_price": 1290.0,
        "recommended": True,
        "consultant_seats": 5,
        "partner_seats": 15,
        "active_case_limit": None,
        "features": [
            "5 consultant seats · Up to 15 partners",
            "Unlimited cases",
            "AI document review and form drafting",
            "Partner task delegation and deliverables",
            "Priority support within 4 hours",
        ],
        "flags": {"ai_document_review": True, "partner_delegation": True, "sso": False},
    },
    PlanCode.ENTERPRISE: {
        "code": PlanCode.ENTERPRISE,
        "name": "Enterprise",
        "tagline": "For multi-office firms with compliance needs.",
        "monthly_price": 299.0,
        "annual_price": 2990.0,
        "recommended": False,
        "consultant_seats": None,
        "partner_seats": None,
        "active_case_limit": None,
        "features": [
            "Unlimited consultant seats · Unlimited partners",
            "Everything in Professional",
            "Multi-office workspaces and audit trail",
            "Custom data retention and SSO",
            "Dedicated success manager",
        ],
        "flags": {"ai_document_review": True, "partner_delegation": True, "sso": True},
    },
}

#: Plan tiers, smallest first. ``PLANS`` is declared in that order and
#: ``PlanCode`` matches it, so the rank is the position rather than a second
#: list to keep in step with the first.
_TIERS = list(PLANS)


def rank(plan_code: PlanCode) -> int:
    """How large a plan is. Higher means more of everything."""
    return _TIERS.index(PlanCode(plan_code))


def is_downgrade(current: PlanCode, wanted: PlanCode) -> bool:
    """True when moving from ``current`` to ``wanted`` means less plan."""
    return rank(wanted) < rank(current)


# No tax is added to the price, and none is charged: the plan price is what the
# customer pays. The 20% that used to sit here came from a design mockup rather
# than any tax authority, and it was never sent to Stripe - so the checkout
# promised one number and billed a smaller one.
#
# When the business is actually registered for VAT/GST, do not put a rate back
# here. Turn on Stripe Tax instead (`automatic_tax={"enabled": True}` on the
# subscription): it works out the right rate from the customer's address,
# country by country, and it is the amount Stripe genuinely collects.
TAX_RATE = 0.0
PLAN_PRICE_OVERRIDES_KEY = "plan_price_overrides"
PLAN_OVERRIDES_KEY = "plan_overrides"


def _clean_plan_overrides(raw: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}

    valid_codes = {p.value for p in PlanCode}
    allowed_fields = {
        "name",
        "tagline",
        "monthly_price",
        "annual_price",
        "recommended",
        "consultant_seats",
        "partner_seats",
        "active_case_limit",
        "features",
        "flags",
    }
    out: Dict[str, Dict[str, Any]] = {}
    for code, values in raw.items():
        if code not in valid_codes or not isinstance(values, dict):
            continue

        plan_overrides: Dict[str, Any] = {}
        for field, value in values.items():
            if field not in allowed_fields:
                continue
            if field in {"monthly_price", "annual_price"}:
                if not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0:
                    plan_overrides[field] = round(float(value), 2)
            elif field in {"consultant_seats", "partner_seats", "active_case_limit"}:
                if value is None:
                    plan_overrides[field] = None
                elif not isinstance(value, bool) and isinstance(value, int) and value >= 0:
                    plan_overrides[field] = value
            elif field == "recommended":
                if isinstance(value, bool):
                    plan_overrides[field] = value
            elif field in {"name", "tagline"}:
                if isinstance(value, str) and value.strip():
                    plan_overrides[field] = value.strip()
            elif field == "features":
                if isinstance(value, list) and all(isinstance(item, str) for item in value):
                    plan_overrides[field] = [item.strip() for item in value if item.strip()]
            elif field == "flags":
                if isinstance(value, dict):
                    plan_overrides[field] = {
                        str(k): bool(v) for k, v in value.items() if isinstance(v, bool)
                    }
        if plan_overrides:
            out[code] = plan_overrides
    return out


async def plan_overrides() -> Dict[str, Dict[str, Any]]:
    db = platform_db()
    legacy_row = await db.platform_settings.find_one({"key": PLAN_PRICE_OVERRIDES_KEY})
    legacy = _clean_plan_overrides(legacy_row.get("value") if legacy_row else {})

    row = await db.platform_settings.find_one({"key": PLAN_OVERRIDES_KEY})
    current = _clean_plan_overrides(row.get("value") if row else {})

    for code, values in legacy.items():
        current.setdefault(code, {})
        for field in ("monthly_price", "annual_price"):
            if field in values and field not in current[code]:
                current[code][field] = values[field]
    return current


async def save_plan_overrides(
    overrides: Dict[str, Dict[str, Any]],
    *,
    updated_by: str,
    updated_at,
) -> Dict[str, Dict[str, Any]]:
    cleaned = _clean_plan_overrides(overrides)
    await platform_db().platform_settings.update_one(
        {"key": PLAN_OVERRIDES_KEY},
        {"$set": {"value": cleaned, "updated_by": updated_by, "updated_at": updated_at}},
        upsert=True,
    )
    return cleaned


async def plan_catalogue() -> Dict[PlanCode, dict]:
    plans = deepcopy(PLANS)
    overrides = await plan_overrides()
    for code in PlanCode:
        override = overrides.get(code.value)
        if not override:
            continue
        for field, value in override.items():
            if field == "flags":
                plans[code]["flags"].update(value)
            else:
                plans[code][field] = value
    return plans


async def plan_by_code(plan_code: PlanCode) -> dict:
    return (await plan_catalogue())[plan_code]


async def price_for(plan_code: PlanCode, cycle: BillingCycle) -> float:
    plan = await plan_by_code(plan_code)
    return plan["annual_price"] if cycle == BillingCycle.ANNUAL else plan["monthly_price"]


async def order_summary(plan_code: PlanCode, cycle: BillingCycle) -> dict:
    """What the customer is told they will pay - and what Stripe then charges.

    The two have to agree. ``estimated_tax`` and ``total_due_today`` stay in the
    response so nothing downstream has to change; with no tax they simply read
    zero and the subtotal.
    """
    subtotal = await price_for(plan_code, cycle)
    tax = round(subtotal * TAX_RATE, 2)
    return {"subtotal": subtotal, "estimated_tax": tax, "total_due_today": round(subtotal + tax, 2)}
