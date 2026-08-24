"""`events.notify` — the choke point every notification goes through.

Twenty-odd call sites across the domain modules say "tell these people this",
and this one function decides what that means: a record, a socket frame, and a
push. Consolidating it is what made push cover the whole system at once, and it
is also what makes this function the single place a mistake would break every
notification in the product.

So what is pinned here is the contract those call sites depend on:

  * the record is written first, and a delivery failure never undoes it;
  * a target passed as a top-level `extra` still reaches the push, because
    without that a support reply pushes with no ticket to open;
  * `collection` and `id_field` route the platform-side inboxes correctly - they
    are keyed differently from the tenant one, and getting it wrong writes rows
    nobody's query will ever match.
"""
from typing import Any, Dict, List

import pytest

from app.core.enums import NotificationType
from app.services import events


class FakeCollection:
    def __init__(self, name: str, store: Dict[str, List[Dict[str, Any]]]) -> None:
        self._name = name
        self._store = store

    async def insert_many(self, documents):
        self._store.setdefault(self._name, []).extend(documents)

        class Result:
            inserted_ids = [None] * len(documents)

        return Result()

    async def count_documents(self, query):
        rows = self._store.get(self._name, [])
        return len([
            r for r in rows
            if all(r.get(k) == v for k, v in query.items())
        ])


class FakeDb:
    """Just enough of a Motor database for `notify`: `db[name]` and nothing else."""

    def __init__(self) -> None:
        self.store: Dict[str, List[Dict[str, Any]]] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return FakeCollection(name, self.store)


@pytest.fixture
def captured(monkeypatch):
    """Records what would have been pushed and socketed, without doing either."""
    pushes: List[Dict[str, Any]] = []
    frames: List[Dict[str, Any]] = []

    def dispatch(user_ids, **kwargs):
        pushes.append({"user_ids": list(user_ids), **kwargs})

    async def publish(recipients, *, type, title, body, data):
        frames.append({"recipients": list(recipients), "type": type,
                       "title": title, "body": body, "data": data})

    monkeypatch.setattr(events.push, "dispatch", dispatch)
    monkeypatch.setattr(events, "_publish", publish)
    return pushes, frames


async def test_one_call_records_and_delivers(captured):
    """The three legs, from one call. This is the whole point of the choke point."""
    pushes, frames = captured
    db = FakeDb()

    await events.notify(
        db,
        user_ids=["u1", "u2"],
        type=NotificationType.DOCUMENT_APPROVED,
        title="Document approved",
        body="Your passport was approved.",
        data={"document_id": "d1", "case_id": "c1"},
    )

    rows = db.store["notifications"]
    assert [r["user_id"] for r in rows] == ["u1", "u2"]
    assert rows[0]["type"] == "document_approved"
    assert rows[0]["read"] is False
    assert rows[0]["data"] == {"document_id": "d1", "case_id": "c1"}

    assert len(pushes) == 1
    assert pushes[0]["user_ids"] == ["u1", "u2"]
    assert pushes[0]["type"] == "document_approved"
    assert pushes[0]["data"]["case_id"] == "c1"

    assert len(frames) == 1
    assert frames[0]["recipients"] == ["u1", "u2"]


async def test_a_top_level_extra_target_still_reaches_the_push(captured):
    """A support reply carries its ticket beside `data`, not inside it.

    Those records have always stored the reference as a top-level field, and the
    push payload is what a tap routes on - so a target that lives in `extra` has
    to be lifted across or the alert opens nothing.
    """
    pushes, frames = captured
    db = FakeDb()

    await events.notify(
        db,
        user_ids=["u1"],
        type=NotificationType.SUPPORT_REPLY,
        title="Reply on [TKT-1001]",
        body="We have looked into it.",
        extra={"ticket_id": "t1", "reference": "TKT-1001"},
    )

    row = db.store["notifications"][0]
    # Stored where the existing readers expect it.
    assert row["ticket_id"] == "t1"
    assert row["reference"] == "TKT-1001"

    # And delivered where the app expects it.
    assert pushes[0]["data"]["ticket_id"] == "t1"
    assert frames[0]["data"]["ticket_id"] == "t1"
    # `reference` is not a target - it names nothing to open - so it is not
    # pushed as one.
    assert "reference" not in pushes[0]["data"]


async def test_the_platform_inboxes_are_keyed_the_way_their_readers_query(captured):
    """`admin_notifications` is keyed on `admin_id`, not `user_id`.

    Two different platform-side inboxes with two different key names. Writing a
    `user_id` into the one that queries `admin_id` produces rows that exist and
    are never read - a notification that silently never arrives.
    """
    pushes, _ = captured
    db = FakeDb()

    await events.notify(
        db,
        user_ids=["admin1"],
        type=NotificationType.ANNOUNCEMENT,
        title="Maintenance tonight",
        body="",
        collection="admin_notifications",
        id_field="admin_id",
    )

    row = db.store["admin_notifications"][0]
    assert row["admin_id"] == "admin1"
    assert "user_id" not in row
    # The push does not care which inbox it was: it is addressed to the account.
    assert pushes[0]["user_ids"] == ["admin1"]


async def test_no_recipients_writes_nothing(captured):
    """`insert_many([])` raises on a real driver, and the call sites pass lists
    built from optional ids - so an empty one has to be a no-op, not an error."""
    pushes, frames = captured
    db = FakeDb()

    await events.notify(db, user_ids=[None, ""], type=NotificationType.SUBSCRIPTION,
                        title="Nobody")

    assert db.store == {}
    assert pushes == []
    assert frames == []


async def test_the_badge_counts_unread_in_the_right_inbox(captured):
    """iOS draws the number, and it has to come from the collection the
    notification was written to - not from the tenant default."""
    pushes, _ = captured
    db = FakeDb()

    # Two already-unread rows for this admin, in the platform inbox.
    db.store["platform_notifications"] = [
        {"user_id": "a1", "read": False},
        {"user_id": "a1", "read": False},
    ]

    await events.notify(
        db,
        user_ids=["a1"],
        type=NotificationType.SUPPORT_TICKET,
        title="New ticket",
        collection="platform_notifications",
    )

    # The two that were there, plus the one just written.
    assert pushes[0]["badges"] == {"a1": 3}


async def test_a_broadcast_skips_the_badge(captured, monkeypatch):
    """One count query per recipient is fine for a handful and wrong for a fleet.

    An announcement to every account on the platform does not need an exact
    number on the icon, and paying for thousands of queries to get one would be
    the wrong trade.
    """
    pushes, _ = captured
    monkeypatch.setattr(events.settings, "PUSH_BADGE_RECIPIENT_LIMIT", 2)
    db = FakeDb()

    await events.notify(db, user_ids=["u1", "u2", "u3"],
                        type=NotificationType.ANNOUNCEMENT, title="Everyone")

    assert pushes[0]["badges"] is None
    # The records are still written for all three.
    assert len(db.store["notifications"]) == 3


async def test_a_socket_failure_does_not_lose_the_notification(monkeypatch):
    """The record is the truth; the two delivery legs are best effort.

    `_publish` reaching a dead Socket.IO server must not turn a successful
    document approval into a 500 for the consultant who approved it.
    """
    db = FakeDb()
    pushes = []
    monkeypatch.setattr(events.push, "dispatch",
                        lambda ids, **kw: pushes.append(ids))

    async def explode(recipients, **kwargs):
        raise RuntimeError("socket is gone")

    monkeypatch.setattr("app.services.realtime.publish_notification", explode)

    await events.notify(db, user_ids=["u1"],
                        type=NotificationType.CASE_STAGE_CHANGED,
                        title="Stage moved")

    assert len(db.store["notifications"]) == 1
    # And the push still went out - one leg failing does not take the other down.
    assert pushes == [["u1"]]
