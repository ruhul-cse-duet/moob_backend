"""Subscription · change plan.

An upgrade is the one place where the entitlement and the money can drift
apart: the seats are handed out in our own database, while the charge lives at
Stripe. The properties pinned here are all about keeping the two together.

* **Stripe first.** Nothing is written down until Stripe has actually moved the
  subscription onto the new Price and billed the difference.
* **A decline changes nothing.** A card that cannot pay the proration leaves the
  workspace on the plan it was already paying for.
* **No subscription, no upgrade.** A workspace with no recurring subscription
  cannot be moved up for free while Stripe is live.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import BillingCycle, PlanCode, TenantStatus
from app.core.exceptions import BadRequest
from app.modules.subscriptions import plans
from app.modules.subscriptions import router as subs
from app.services import audit

TENANT_ID = ObjectId()
SUBSCRIPTION_ID = "sub_live_1"


class Owner:
    id = str(ObjectId())
    tenant_id = str(TENANT_ID)
    email = "owner@maple.example"


@pytest.fixture
def db(monkeypatch):
    pdb = AsyncMongoMockClient()["webimove_platform"]
    # `plans` reads platform_settings for the super admin's price overrides,
    # and `audit` writes the Oversight entry the plan change leaves behind.
    for module in (subs, plans, audit):
        monkeypatch.setattr(module, "platform_db", lambda: pdb)
    return pdb


@pytest.fixture
async def seeded(db):
    await db.tenants.insert_one({
        "_id": TENANT_ID, "name": "Maple Route Immigration",
        "status": TenantStatus.ACTIVE.value, "plan_code": PlanCode.STARTER.value,
        "billing_cycle": BillingCycle.MONTHLY.value,
        "stripe_customer_id": "cus_1", "stripe_subscription_id": SUBSCRIPTION_ID,
    })
    await db.subscriptions.insert_one({
        "tenant_id": str(TENANT_ID), "plan_code": PlanCode.STARTER.value,
        "billing_cycle": BillingCycle.MONTHLY.value, "status": "active",
    })
    return db


def _stripe(monkeypatch, *, configured=True, result=None, calls=None):
    monkeypatch.setattr(subs.stripe_service, "configured", lambda: configured)

    async def change_subscription_plan(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return result

    monkeypatch.setattr(subs.stripe_service, "change_subscription_plan",
                        change_subscription_plan)


async def test_upgrade_bills_stripe_then_records_the_new_plan(seeded, monkeypatch):
    calls = []
    _stripe(monkeypatch, calls=calls, result={
        "success": True, "message": "Subscription active",
        "subscription_id": SUBSCRIPTION_ID, "price_id": "price_pro_monthly",
        "status": "active", "current_period_end": 1893456000,
        "latest_invoice_id": "in_proration_1",
    })

    out = await subs.change_plan(PlanCode.PROFESSIONAL, BillingCycle.MONTHLY, Owner())

    # The charge went out for the plan and the amount the owner was quoted.
    assert calls[0]["subscription_id"] == SUBSCRIPTION_ID
    assert calls[0]["plan_code"] is PlanCode.PROFESSIONAL
    assert calls[0]["billing_cycle"] is BillingCycle.MONTHLY
    # The quoted total and the charge are the same number. A screen that
    # promises $3,588 while Stripe takes $2,990 is the bug this pins shut.
    assert calls[0]["amount"] == out["subtotal"] == out["total_due_today"] == 129.0
    assert out["estimated_tax"] == 0

    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.PROFESSIONAL.value
    # Stripe's period end wins over our own arithmetic.
    assert tenant["renews_on"].year == 2030

    live = await seeded.subscriptions.find_one({"tenant_id": str(TENANT_ID),
                                                "status": "active"})
    assert live["plan_code"] == PlanCode.PROFESSIONAL.value
    assert live["stripe_price_id"] == "price_pro_monthly"
    assert live["last_invoice_id"] == "in_proration_1"
    assert out["billing"]["charged"] is True


async def test_declined_card_leaves_the_old_plan_in_place(seeded, monkeypatch):
    _stripe(monkeypatch, result={
        "success": False, "message": "Your card was declined.",
        "subscription_id": SUBSCRIPTION_ID, "price_id": None,
    })

    with pytest.raises(BadRequest, match="declined"):
        await subs.change_plan(PlanCode.ENTERPRISE, BillingCycle.ANNUAL, Owner())

    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.STARTER.value
    rows = [r async for r in seeded.subscriptions.find({"tenant_id": str(TENANT_ID)})]
    assert len(rows) == 1 and rows[0]["status"] == "active"
    assert rows[0]["plan_code"] == PlanCode.STARTER.value


async def test_workspace_without_a_subscription_cannot_upgrade_for_free(seeded, monkeypatch):
    await seeded.tenants.update_one({"_id": TENANT_ID},
                                    {"$unset": {"stripe_subscription_id": ""}})
    _stripe(monkeypatch, result={"success": True})

    with pytest.raises(BadRequest, match="no recurring subscription"):
        await subs.change_plan(PlanCode.PROFESSIONAL, BillingCycle.MONTHLY, Owner())

    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.STARTER.value


async def test_without_stripe_keys_the_plan_still_moves(seeded, monkeypatch):
    """A development environment has nothing to bill, and must still work."""
    _stripe(monkeypatch, configured=False, result=None)

    out = await subs.change_plan(PlanCode.PROFESSIONAL, BillingCycle.MONTHLY, Owner())

    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.PROFESSIONAL.value
    assert out["billing"]["charged"] is False


async def test_a_suspended_workspace_cannot_upgrade_its_way_back_in(seeded, monkeypatch):
    await seeded.tenants.update_one(
        {"_id": TENANT_ID}, {"$set": {"status": TenantStatus.SUSPENDED.value}})
    _stripe(monkeypatch, result={"success": True})

    with pytest.raises(BadRequest, match="not currently active"):
        await subs.change_plan(PlanCode.PROFESSIONAL, BillingCycle.MONTHLY, Owner())

    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["status"] == TenantStatus.SUSPENDED.value


async def test_downgrade_is_still_refused_before_anything_is_billed(seeded, monkeypatch):
    await seeded.subscriptions.update_many(
        {"tenant_id": str(TENANT_ID)},
        {"$set": {"plan_code": PlanCode.ENTERPRISE.value}})
    calls = []
    _stripe(monkeypatch, calls=calls, result={"success": True})

    with pytest.raises(BadRequest, match="cannot be moved down"):
        await subs.change_plan(PlanCode.STARTER, BillingCycle.MONTHLY, Owner())
    assert calls == []


# --------------------------------------------------------------------------- #
# Super admin · plan override
#
# A different transaction entirely: the price was agreed off-platform, so
# nothing is charged. What must not happen is Stripe quietly renewing at the
# old amount with nobody told about it.
# --------------------------------------------------------------------------- #
from app.modules.admin import organizations as orgs  # noqa: E402


class SuperAdmin:
    id = str(ObjectId())
    email = "superadmin@webimove.com"


@pytest.fixture
def admin_db(db, monkeypatch):
    monkeypatch.setattr(orgs, "platform_db", lambda: db)
    return db


async def test_an_override_charges_nothing_and_says_stripe_was_left_alone(
        seeded, admin_db, monkeypatch):
    calls = []
    monkeypatch.setattr(orgs.stripe_service, "configured", lambda: True)

    async def change_subscription_plan(**kwargs):
        calls.append(kwargs)
        return {"success": True}

    monkeypatch.setattr(orgs.stripe_service, "change_subscription_plan",
                        change_subscription_plan)

    out = await orgs.override_plan(str(TENANT_ID), PlanCode.ENTERPRISE,
                                   BillingCycle.ANNUAL, "migrated deal",
                                   False, SuperAdmin())

    assert calls == []
    assert "Stripe was not touched" in out["message"]
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.ENTERPRISE.value


async def test_sync_stripe_moves_the_price_without_billing_today(
        seeded, admin_db, monkeypatch):
    calls = []
    monkeypatch.setattr(orgs.stripe_service, "configured", lambda: True)

    async def change_subscription_plan(**kwargs):
        calls.append(kwargs)
        return {"success": True, "price_id": "price_ent_annual"}

    monkeypatch.setattr(orgs.stripe_service, "change_subscription_plan",
                        change_subscription_plan)

    out = await orgs.override_plan(str(TENANT_ID), PlanCode.ENTERPRISE,
                                   BillingCycle.ANNUAL, None, True, SuperAdmin())

    # No proration invoice: the next renewal is the first at the new amount.
    assert calls[0]["charge_now"] is False
    assert calls[0]["amount"] == 2990.0
    assert "nothing was charged today" in out["message"]
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["billing_cycle"] == BillingCycle.ANNUAL.value


async def test_sync_stripe_refuses_when_there_is_no_subscription_to_move(
        seeded, admin_db, monkeypatch):
    await seeded.tenants.update_one({"_id": TENANT_ID},
                                    {"$unset": {"stripe_subscription_id": ""}})
    monkeypatch.setattr(orgs.stripe_service, "configured", lambda: True)

    with pytest.raises(BadRequest, match="no Stripe subscription"):
        await orgs.override_plan(str(TENANT_ID), PlanCode.ENTERPRISE,
                                 BillingCycle.ANNUAL, None, True, SuperAdmin())

    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.STARTER.value


async def test_every_plan_quotes_exactly_what_stripe_will_charge(db):
    """No tax is added anywhere, so the checkout total is the plan price.

    Both halves matter: a tax we display but never send to Stripe undercharges,
    and one we do send takes money from a customer for a tax nobody remits.
    """
    for plan_code in PlanCode:
        for cycle in BillingCycle:
            summary = await plans.order_summary(plan_code, cycle)
            assert summary["estimated_tax"] == 0, (plan_code, cycle)
            assert summary["total_due_today"] == summary["subtotal"], (plan_code, cycle)


async def test_the_billing_screen_can_reach_the_publishable_key(monkeypatch):
    """Without this the card form has no key and can only fail to load.

    Signup's copy lives under /auth, which an authenticated billing screen has
    no business calling - so the two answer from one helper instead.
    """
    from app.core.config import settings
    from app.modules.auth import router as auth_router

    monkeypatch.setattr(settings, "STRIPE_PUBLISHABLE_KEY", "pk_test_abc")
    monkeypatch.setattr(subs.stripe_service, "configured", lambda: True)

    config = await subs.payment_config(Owner())

    assert config["publishable_key"] == "pk_test_abc"
    assert config["card_tokenization"] is True
    # Both endpoints must say the same thing, always.
    assert await auth_router.payment_config() == config


async def test_the_card_form_is_told_not_to_collect_when_stripe_is_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "STRIPE_PUBLISHABLE_KEY", "")
    monkeypatch.setattr(subs.stripe_service, "configured", lambda: True)

    config = await subs.payment_config(Owner())

    assert config["publishable_key"] is None
    assert config["card_tokenization"] is False
