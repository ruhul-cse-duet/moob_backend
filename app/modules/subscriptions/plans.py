"""Plan catalogue exactly as designed on the subscription screen."""
from app.core.enums import BillingCycle, PlanCode

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
            "Up to 25 active immigration cases",
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
            "Unlimited immigration cases",
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

TAX_RATE = 0.20  # design shows $258 tax on $1,290


def price_for(plan_code: PlanCode, cycle: BillingCycle) -> float:
    plan = PLANS[plan_code]
    return plan["annual_price"] if cycle == BillingCycle.ANNUAL else plan["monthly_price"]


def order_summary(plan_code: PlanCode, cycle: BillingCycle) -> dict:
    subtotal = price_for(plan_code, cycle)
    tax = round(subtotal * TAX_RATE, 2)
    return {"subtotal": subtotal, "estimated_tax": tax, "total_due_today": round(subtotal + tax, 2)}
