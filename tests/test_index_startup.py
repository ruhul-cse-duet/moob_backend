"""Index creation on boot.

Every `create_index` is a network round trip to Atlas, and there are dozens of
them on the platform database. Awaited one after another they are paid for
sequentially before the API answers its first request - which on a free cluster,
on a free Render instance that has just been woken up, is the difference between
a cold start a person waits through and one they give up on.

They do not depend on each other, so they are gathered. What is asserted here is
that they genuinely overlap: `asyncio.gather` over coroutines that were awaited
inside a loop would still be sequential, and the difference is invisible without
counting.

The counts are compared against each other - peak versus total - rather than
against a hard-coded number. Pinning the literal count would mean every added
index broke this file, which trains people to update the number rather than read
what it is telling them.
"""
import asyncio

import pytest

from app.db import indexes


class RecordingCollection:
    """Notes when each index call is in flight, so overlap can be counted."""

    def __init__(self, tracker):
        self._tracker = tracker

    async def create_index(self, *args, **kwargs):
        self._tracker["in_flight"] += 1
        self._tracker["peak"] = max(self._tracker["peak"], self._tracker["in_flight"])
        self._tracker["total"] += 1
        # Stands in for the round trip. Without a real await the calls could
        # complete one at a time and still look concurrent.
        await asyncio.sleep(0.01)
        self._tracker["in_flight"] -= 1


class RecordingDb:
    def __init__(self):
        self.tracker = {"in_flight": 0, "peak": 0, "total": 0}

    def __getattr__(self, _name):
        return RecordingCollection(self.tracker)

    def __getitem__(self, _name):
        return RecordingCollection(self.tracker)


async def test_platform_indexes_are_created_concurrently(monkeypatch):
    """Every round trip overlapping, not queueing.

    The peak is what proves it. If these were awaited in a loop the peak would
    be 1, the total would be the same, and nothing else about the code would
    look different.
    """
    db = RecordingDb()
    monkeypatch.setattr(indexes, "platform_db", lambda: db)

    await indexes.ensure_platform_indexes()

    assert db.tracker["total"] > 30, "suspiciously few indexes - did a block get dropped?"
    assert db.tracker["peak"] == db.tracker["total"], (
        f"index creation serialised - the boot cost is back to "
        f"{db.tracker['total']} sequential round trips"
    )
    # Nothing left running.
    assert db.tracker["in_flight"] == 0


async def test_tenant_indexes_are_created_concurrently():
    """Same for a new workspace, which is a person waiting on a signup."""
    db = RecordingDb()

    await indexes.ensure_tenant_indexes(db)

    assert db.tracker["total"] > 30
    assert db.tracker["peak"] == db.tracker["total"]
    assert db.tracker["in_flight"] == 0


async def test_one_failing_index_does_not_abandon_the_rest(monkeypatch, caplog):
    """A stale unique constraint over data that now has duplicates.

    `gather` without `return_exceptions` would raise on the first failure and
    skip whatever had not been scheduled yet - so one bad index would quietly
    cost all the others, and every query they served would go to a collection
    scan. Booting degraded is right; booting half-indexed and silent is not.
    """
    db = RecordingDb()
    original = RecordingCollection.create_index
    calls = {"n": 0}

    async def sometimes_fails(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("index conflicts with existing data")
        await original(self, *args, **kwargs)

    monkeypatch.setattr(RecordingCollection, "create_index", sometimes_fails)
    monkeypatch.setattr(indexes, "platform_db", lambda: db)

    with caplog.at_level("WARNING"):
        await indexes.ensure_platform_indexes()

    # Every one was attempted, not just those before the failure.
    assert calls["n"] == db.tracker["total"] + 1  # +1: the one that raised
    # And the failure is on the record rather than swallowed.
    assert any("could not be created" in r.message for r in caplog.records)


async def test_a_total_failure_is_reported_as_one(monkeypatch):
    """Every index failing is the database being gone, and must be said so.

    A single failure is a stale constraint - the API runs, just slower. All of
    them failing is a connection that does not exist, and swallowing that is
    what let startup log "Database connected; indexes ready" over an Atlas
    cluster that was refusing every handshake. The honest message - the one
    naming MONGODB_URI and the IP allowlist - lives in `main.lifespan`, and it
    only prints if something is raised for it to catch.
    """
    async def always_fails(self, *args, **kwargs):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(RecordingCollection, "create_index", always_fails)
    monkeypatch.setattr(indexes, "platform_db", lambda: RecordingDb())

    with pytest.raises(RuntimeError, match="no route to host"):
        await indexes.ensure_platform_indexes()


async def test_the_api_still_boots_when_the_database_is_gone(monkeypatch):
    """Raising above must not turn a degraded boot into a failed one.

    This is the property the previous version of the test above was really
    protecting - it just protected it in the wrong place, by staying silent
    here instead of letting the caller decide.
    """
    from app import main

    async def unreachable():
        raise RuntimeError("no route to host")

    monkeypatch.setattr(main, "connect", lambda: None)
    monkeypatch.setattr(main, "ensure_platform_indexes", unreachable)

    # Not caplog: `setup_logging` gives the app its own handlers and stops
    # propagation, so pytest's capture never sees these records. Listening on
    # the logger itself is what actually observes what an operator would read.
    import logging

    heard = []

    class _Listen(logging.Handler):
        def emit(self, record):
            heard.append(record.getMessage())

    handler = _Listen(level=logging.ERROR)
    logging.getLogger("app.main").addHandler(handler)
    try:
        async with main.lifespan(main.app):
            pass
    finally:
        logging.getLogger("app.main").removeHandler(handler)

    said = " ".join(heard)
    assert "degraded mode" in said
    # The two settings that actually cause this, named where somebody will read
    # them at three in the morning.
    assert "MONGODB_URI" in said and "allowlist" in said
