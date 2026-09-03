"""Subscription · cancel.

Cancelling has to be safe to repeat. The route refuses to mark our own records
cancelled while Stripe might still be charging the card - correct, and the one
thing that must not be softened. But Stripe answers a *second* cancellation
with "No such subscription", and reading that as "still billing" left an owner
permanently unable to close a workspace whose Stripe side had already stopped.

  * Already stopped at Stripe -> cancel locally. There is nothing left to bill.
  * Genuinely could not stop it -> change nothing, and say so.
"""
import pytest
import stripe
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import BillingCycle, PlanCode, TenantStatus
from app.core.exceptions import BadRequest
from app.modules.subscriptions import router as subs
from app.services import stripe_service

TENANT_ID = ObjectId()
SUBSCRIPTION_ID = "sub_live_1"


class Owner:
    id = str(ObjectId())
    tenant_id = str(TENANT_ID)
    email = "owner@maple.example"


@pytest.fixture
def db(monkeypatch):
    pdb = AsyncMongoMockClient()["webimove_platform"]
    monkeypatch.setattr(subs, "platform_db", lambda: pdb)
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


# ── the route ──────────────────────────────────────────────────────────────
async def test_a_subscription_stripe_has_already_stopped_can_still_be_cancelled(
    seeded, monkeypatch
):
    async def already_stopped(subscription_id):
        return True

    monkeypatch.setattr(subs.stripe_service, "cancel_subscription", already_stopped)

    out = await subs.cancel(Owner())

    assert "cancelled" in out["detail"].lower()
    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["status"] == TenantStatus.CANCELLED.value
    assert tenant["cancelled_at"] is not None
    rows = [r async for r in seeded.subscriptions.find({"tenant_id": str(TENANT_ID)})]
    assert [r["status"] for r in rows] == ["cancelled"]


async def test_a_subscription_that_might_still_bill_is_left_alone(seeded, monkeypatch):
    async def could_not_stop(subscription_id):
        return False

    monkeypatch.setattr(subs.stripe_service, "cancel_subscription", could_not_stop)

    with pytest.raises(BadRequest, match="not billed again"):
        await subs.cancel(Owner())

    tenant = await seeded.tenants.find_one({"_id": TENANT_ID})
    assert tenant["status"] == TenantStatus.ACTIVE.value
    rows = [r async for r in seeded.subscriptions.find({"tenant_id": str(TENANT_ID)})]
    assert [r["status"] for r in rows] == ["active"]


# ── what the service reports, which is what the route acts on ──────────────
class _FakeSubscriptions:
    def __init__(self, status):
        self._status = status

    async def cancel_async(self, subscription_id):
        raise stripe.InvalidRequestError(
            f"No such subscription: '{subscription_id}'", param=None)

    async def retrieve_async(self, subscription_id):
        if self._status is None:
            raise stripe.InvalidRequestError("No such subscription", param=None)
        return type("Sub", (), {"status": self._status})()


def _fake_stripe(monkeypatch, status):
    monkeypatch.setattr(stripe_service, "_client",
                        lambda: type("Api", (), {"Subscription": _FakeSubscriptions(status)})())


@pytest.mark.parametrize("status, stopped", [
    ("canceled", True),            # already cancelled - nothing left to bill
    ("incomplete_expired", True),  # never started and will not
    (None, True),                  # not on this account at all
    ("active", False),             # refused, and still charging: do not proceed
    ("past_due", False),           # retries are still running against the card
])
async def test_a_refused_cancellation_is_judged_by_whether_it_can_still_bill(
    monkeypatch, status, stopped
):
    _fake_stripe(monkeypatch, status)
    assert await stripe_service.cancel_subscription(SUBSCRIPTION_ID) is stopped


async def test_no_subscription_id_is_not_a_stripe_success(monkeypatch):
    _fake_stripe(monkeypatch, "canceled")
    # The route only consults Stripe when there is an id, so this must stay
    # falsy rather than quietly reporting a cancellation that never happened.
    assert await stripe_service.cancel_subscription("") is False
