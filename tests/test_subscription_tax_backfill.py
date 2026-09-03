"""Backfill · clearing tax that was recorded but never collected.

The whole risk sits in one decision: which rows were actually charged the
tax-inclusive total. Get it wrong in one direction and the platform keeps
counting revenue it never received; wrong in the other and a real payment is
rewritten to a smaller number than the customer's statement shows.

The dry run matters just as much - a migration nobody can inspect before it
writes is one nobody will run.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

import scripts_fix_subscription_tax as backfill

TENANT_ID = str(ObjectId())


@pytest.fixture
def db(monkeypatch):
    pdb = AsyncMongoMockClient()["webimove_platform"]
    monkeypatch.setattr(backfill, "platform_db", lambda: pdb)
    monkeypatch.setattr(backfill, "connect", lambda: None)

    async def close():
        return None

    monkeypatch.setattr(backfill, "close", close)
    return pdb


def _row(**over):
    row = {"tenant_id": TENANT_ID, "plan_code": "enterprise",
           "billing_cycle": "annual", "status": "active",
           "amount": 2990.0, "tax": 598.0, "total": 3588.0}
    row.update(over)
    return row


async def test_a_recurring_subscription_only_ever_paid_the_subtotal(db):
    await db.subscriptions.insert_one(_row(recurring=True,
                                           stripe_subscription_id="sub_1"))

    await backfill.main(apply=True)

    row = await db.subscriptions.find_one({})
    assert row["tax"] == 0.0
    assert row["total"] == 2990.0


async def test_a_settled_one_off_charge_is_left_exactly_as_it_is(db):
    """The old raw-card path really did charge total_due_today."""
    await db.subscriptions.insert_one(_row(recurring=False,
                                           payment_reference="ch_legacy_1"))

    await backfill.main(apply=True)

    row = await db.subscriptions.find_one({})
    assert row["tax"] == 598.0
    assert row["total"] == 3588.0


async def test_a_row_that_was_never_charged_at_all_is_corrected(db):
    """The old change-plan route wrote rows without taking any money."""
    await db.subscriptions.insert_one(_row())

    await backfill.main(apply=True)

    row = await db.subscriptions.find_one({})
    assert row["tax"] == 0.0
    assert row["total"] == 2990.0


async def test_each_row_keeps_its_own_amount(db):
    await db.subscriptions.insert_many([
        _row(recurring=True, amount=129.0, tax=25.8, total=154.8),
        _row(recurring=True, amount=49.0, tax=9.8, total=58.8),
    ])

    await backfill.main(apply=True)

    totals = sorted([r["total"] async for r in db.subscriptions.find({})])
    assert totals == [49.0, 129.0]


async def test_the_dry_run_writes_nothing(db):
    await db.subscriptions.insert_one(_row(recurring=True))

    await backfill.main(apply=False)

    row = await db.subscriptions.find_one({})
    assert row["tax"] == 598.0
    assert row["total"] == 3588.0


async def test_rows_with_no_tax_are_not_touched(db, capsys):
    await db.subscriptions.insert_one(_row(tax=0.0, total=2990.0))

    await backfill.main(apply=True)

    assert "Nothing to do" in capsys.readouterr().out


async def test_an_unreachable_cluster_is_explained_not_dumped(db, monkeypatch, capsys):
    """A migration that dies in forty lines of driver stack is one nobody runs."""
    from pymongo.errors import ServerSelectionTimeoutError

    class Exploding:
        def find(self, *args, **kwargs):
            raise ServerSelectionTimeoutError(
                "SSL handshake failed: ac-1.mongodb.net:27017: "
                "[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error"
            )

    class Db:
        subscriptions = Exploding()

    monkeypatch.setattr(backfill, "platform_db", lambda: Db())

    with pytest.raises(SystemExit) as exit_code:
        await backfill.main(apply=True)

    assert exit_code.value.code == 1
    out = capsys.readouterr().out
    assert "not on its allowlist" in out
    assert "Network Access" in out
    assert "Traceback" not in out
