"""Stripe webhook · following a plan change made outside this API.

Somebody can move a subscription onto a different Price straight from the
Stripe dashboard. Until the workspace follows, it keeps the seats, case limit
and feature flags of the plan it used to pay for - the customer is billed for
one thing and using another.

The rules pinned here:

* **Metadata first, the Price second.** Our own subscriptions carry the plan in
  their metadata; a hand-made one is read off the Price instead.
* **Only write when something differs.** An ordinary renewal must not churn the
  tenant row or fill Oversight with entries nobody made.
* **Status handling is unaffected.** The plan syncs whether the subscription is
  active or failing to pay.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import AuditAction, BillingCycle, PlanCode, TenantStatus
from app.modules.webhooks import router as hooks
from app.services import audit

TENANT_ID = ObjectId()


@pytest.fixture
def db(monkeypatch):
    pdb = AsyncMongoMockClient()["webimove_platform"]
    for module in (hooks, audit):
        monkeypatch.setattr(module, "platform_db", lambda: pdb)
    return pdb


@pytest.fixture
async def seeded(db):
    await db.tenants.insert_one({
        "_id": TENANT_ID, "name": "Maple Route Immigration",
        "status": TenantStatus.ACTIVE.value, "plan_code": PlanCode.STARTER.value,
        "billing_cycle": BillingCycle.MONTHLY.value,
        "stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
    })
    await db.subscriptions.insert_one({
        "tenant_id": str(TENANT_ID), "plan_code": PlanCode.STARTER.value,
        "billing_cycle": BillingCycle.MONTHLY.value, "status": "active",
    })
    return db


def _event(*, status="active", metadata=None, price_metadata=None, interval="month"):
    return {
        "id": "sub_1", "object": "subscription", "status": status,
        "customer": "cus_1", "metadata": metadata or {},
        "items": {"data": [{"id": "si_1", "price": {
            "id": "price_x", "metadata": price_metadata or {},
            "recurring": {"interval": interval},
        }}]},
    }


async def test_subscription_metadata_moves_the_workspace_onto_the_new_plan(seeded):
    outcome = await hooks._on_subscription_updated(_event(metadata={
        "plan_code": "enterprise", "billing_cycle": "annual"}))

    assert "plan synced to enterprise" in outcome
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.ENTERPRISE.value
    assert tenant["billing_cycle"] == BillingCycle.ANNUAL.value
    live = await seeded.subscriptions.find_one({"tenant_id": str(TENANT_ID),
                                                "status": "active"})
    assert live["plan_code"] == PlanCode.ENTERPRISE.value

    entry = await seeded.audit_log.find_one({"action": AuditAction.PLAN_CHANGED.value})
    assert entry["actor_email"] == "stripe@webhook"
    assert "starter -> enterprise" in entry["detail"]


async def test_a_dashboard_made_price_is_read_off_the_price_metadata(seeded):
    """`ensure_price` stamps the plan on every Price this API creates."""
    outcome = await hooks._on_subscription_updated(_event(
        price_metadata={"plan_code": "professional", "billing_cycle": "annual"},
        interval="year"))

    assert "plan synced to professional" in outcome
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.PROFESSIONAL.value
    assert tenant["billing_cycle"] == BillingCycle.ANNUAL.value


async def test_an_unstamped_price_still_yields_the_cycle_from_the_interval(seeded):
    outcome = await hooks._on_subscription_updated(_event(interval="year"))

    assert "plan synced" in outcome
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    # Nothing said which plan, so that is left alone; the cycle is unambiguous.
    assert tenant["plan_code"] == PlanCode.STARTER.value
    assert tenant["billing_cycle"] == BillingCycle.ANNUAL.value


async def test_an_ordinary_renewal_writes_nothing(seeded):
    outcome = await hooks._on_subscription_updated(_event(metadata={
        "plan_code": "starter", "billing_cycle": "monthly"}))

    assert "plan synced" not in outcome
    assert await seeded.audit_log.count_documents({}) == 0


async def test_an_unknown_plan_code_is_ignored_rather_than_stored(seeded):
    outcome = await hooks._on_subscription_updated(_event(metadata={
        "plan_code": "platinum", "billing_cycle": "monthly"}))

    assert "plan synced" not in outcome
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["plan_code"] == PlanCode.STARTER.value


async def test_the_plan_follows_even_while_the_payment_is_failing(seeded):
    outcome = await hooks._on_subscription_updated(_event(
        status="past_due", metadata={"plan_code": "enterprise",
                                     "billing_cycle": "monthly"}))

    assert outcome == "marked past_due; plan synced to enterprise"
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["status"] == TenantStatus.PAST_DUE.value
    assert tenant["plan_code"] == PlanCode.ENTERPRISE.value
