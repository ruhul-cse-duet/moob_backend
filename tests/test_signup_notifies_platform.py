"""A consultant finishing signup has to reach the platform's inbox.

The workspace lands in `awaiting_approval`, which is a state only a human can
move out of. Nothing else tells the platform side that a workspace is waiting,
so if this notification is not written the approval queue only moves when
somebody happens to open the organizations screen - which is exactly how a paid
signup sits unnoticed.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.utils import utcnow
from app.modules.auth import service
from app.services import events

ADMIN_ID = ObjectId()


@pytest.fixture
def mongo(monkeypatch):
    client = AsyncMongoMockClient()
    pdb = client["webimove_platform"]

    monkeypatch.setattr(service, "platform_db", lambda: pdb)
    monkeypatch.setattr(service, "tenant_db", lambda tid: client[f"webimove_tenant_{tid}"])
    monkeypatch.setattr(events, "platform_db", lambda: pdb, raising=False)
    # Neither the index build nor the audit trail is what this pins, and both
    # want a real driver.
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(service, "ensure_tenant_indexes", _noop)
    monkeypatch.setattr(service.audit, "record", _noop)
    return pdb


@pytest.fixture
async def signup(mongo, monkeypatch):
    await mongo.platform_admins.insert_one(
        {"_id": ADMIN_ID, "email": "admin@moob.com", "status": "active"})
    doc = {
        "email": "sarah@jenkinslaw.com",
        "full_name": "Sarah Jenkins",
        "mobile": "+351900000000",
        "password_hash": "x",
        "email_verified": True,
        "organization": {"name": "Jenkins Immigration Law", "slug": "jenkins",
                         "business_type": "law_firm", "country": "PT",
                         "city": "Lisbon", "office_address": "Rua A 1"},
        "plan": {"code": "professional", "billing_cycle": "monthly",
                 "subtotal": 100, "estimated_tax": 0, "total_due_today": 100,
                 "renews_on": utcnow()},
        # Already paid: `complete_payment` then skips the card entirely.
        "charge_reference": "ch_test_1",
        "stripe": {"customer_id": "cus_1", "subscription_id": "sub_1"},
        "created_at": utcnow(),
    }
    signup_id = (await mongo.signups.insert_one(doc)).inserted_id
    monkeypatch.setattr(service, "_load_signup",
                        lambda token: _found(mongo, signup_id))
    return mongo


async def _found(pdb, signup_id):
    return await pdb.signups.find_one({"_id": signup_id})


class _Payment:
    payment_method_id = "pm_1"
    card_number = None


async def test_the_platform_is_told_a_new_organization_is_waiting(signup, monkeypatch):
    pushed = []
    monkeypatch.setattr(events.push, "dispatch",
                        lambda *a, **k: pushed.append(k))

    result = await service.complete_payment("token", _Payment())

    rows = await signup.platform_notifications.find({}).to_list(None)
    assert [r["user_id"] for r in rows] == [str(ADMIN_ID)]
    row = rows[0]
    assert row["type"] == "tenant_signup"
    # A key, not a sentence: the administrator who reads it may not read English.
    assert row["title_key"] == "notify.tenant_signup"
    assert row["params"]["organization"] == "Jenkins Immigration Law"
    assert row["data"]["tenant_id"] == result["tenant_id"]
    assert row["read"] is False
    assert pushed, "the administrator's phone should hear about it too"


async def test_a_notification_failure_does_not_undo_a_paid_signup(signup, monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("inbox is down")

    monkeypatch.setattr(service, "notify", _boom)

    result = await service.complete_payment("token", _Payment())

    assert result["tenant_id"]
    assert result["awaiting_approval"] is True
