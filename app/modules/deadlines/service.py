"""The one place a date anybody is meant to act on gets written down.

Before this, a date was a field on whatever record it happened to sit on -
a document's `due_date`, a task's `due_date`, an AI-read passport's
`expiry_date` - each readable only by opening that one record, and nothing
ever asked "what is due". That is the gap the client's own message named:
dates sitting isolated in each screen as plain text, with no reading,
tracking or alerts, and no screen where a consultant sees what is due today,
this week or this month.

Every source keeps its own field - this module does not replace `due_date` on
a document or a task, it *mirrors* it here, in one shape, so a single query
answers "what is due" across every kind of thing that can be due. `kind` says
what the mirrored date is:

- ``document_request``  - a document the client was asked to upload
- ``document_expiry``   - an identity/travel document's own expiry, read by
                           the AI off the document itself
- ``partner_task``      - a task delegated to a partner
- ``filing``             - a case's submission to the authority
- ``authority_response`` - the response window after a filing or an
                           authority's request for more information
- ``appointment``        - a scheduled consultation

A row is upserted keyed on `(source_collection, source_id, kind)`, so the
module that owns the underlying record can call `upsert` every time the date
changes (including to `None`, which clears it) without worrying about
duplicates. It is deleted, not archived, once resolved - this is a worklist of
what still needs attention, not a log; the case history (`case_history.py`)
is where the record of what happened lives.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import CurrentUser
from app.core.enums import Role
from app.core.utils import oid, serialize, utcnow
from app.services.ownership import assigned_client_ids

#: Every kind a deadline can mirror, and the icon/verb the UI shows for it.
KINDS = ("document_request", "document_expiry", "partner_task", "filing",
         "authority_response", "appointment")


async def upsert(
    db: AsyncIOMotorDatabase,
    *,
    kind: str,
    source_collection: str,
    source_id: str,
    due_date: Optional[datetime],
    title: str,
    consultant_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_name: Optional[str] = None,
    case_id: Optional[str] = None,
    case_reference: Optional[str] = None,
    owner_id: Optional[str] = None,
    owner_name: Optional[str] = None,
) -> None:
    """Mirror one source record's date onto the worklist.

    `due_date=None` clears the row instead of writing one with nothing in it -
    a document whose due date was never set has nothing to track, the same as
    before this module existed, rather than a deadline of "null" appearing on
    a calendar.
    """
    if due_date is None:
        await clear(db, kind=kind, source_collection=source_collection, source_id=source_id)
        return

    now = utcnow()
    key = {"kind": kind, "source_collection": source_collection, "source_id": source_id}
    await db.deadlines.update_one(
        key,
        {"$set": {
            **key,
            "due_date": due_date,
            "title": title,
            "consultant_id": consultant_id,
            "client_id": client_id,
            "client_name": client_name,
            "case_id": case_id,
            "case_reference": case_reference,
            "owner_id": owner_id,
            "owner_name": owner_name,
            "status": "open",
            "updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )


async def clear(db: AsyncIOMotorDatabase, *, kind: str,
                source_collection: str, source_id: str) -> None:
    """The underlying thing was resolved - uploaded, completed, filed,
    answered - so there is nothing left to remind anyone about."""
    await db.deadlines.delete_many(
        {"kind": kind, "source_collection": source_collection, "source_id": source_id})


async def clear_source(db: AsyncIOMotorDatabase, *,
                       source_collection: str, source_id: str) -> None:
    """Every deadline mirrored from one record, regardless of kind - used
    when the record itself is deleted (a withdrawn document request)."""
    await db.deadlines.delete_many(
        {"source_collection": source_collection, "source_id": source_id})


def _scope(user: CurrentUser) -> Dict[str, Any]:
    """Same visibility rule as everywhere else a worklist is scoped: a
    consultant sees the caseload, a partner sees only what was handed to
    them, a client sees only their own.
    """
    if user.role == Role.CLIENT:
        return {"client_id": user.id}
    if user.role == Role.PARTNER:
        return {"owner_id": user.id}
    return {}


async def list_deadlines(db: AsyncIOMotorDatabase, user: CurrentUser,
                         date_from: Optional[datetime] = None,
                         date_to: Optional[datetime] = None,
                         kind: Optional[str] = None,
                         include_done: bool = False) -> List[Dict[str, Any]]:
    filters: Dict[str, Any] = {}
    if not include_done:
        filters["status"] = "open"
    if kind:
        filters["kind"] = kind
    if date_from or date_to:
        rng: Dict[str, Any] = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        filters["due_date"] = rng

    if user.role == Role.PARTNER:
        # A partner's own deadlines (`owner_id`) plus anything on a client
        # bulk-assigned to them, the same "two sources" rule used everywhere
        # else a partner's visibility is scoped.
        client_ids = await assigned_client_ids(db, user.id)
        query: Dict[str, Any] = {"$or": [
            {**filters, "owner_id": user.id},
            {**filters, "client_id": {"$in": client_ids}},
        ]}
    else:
        query = {**filters, **_scope(user)}

    now = utcnow()
    items = [serialize(d) async for d in db.deadlines.find(query).sort("due_date", 1)]
    for item in items:
        due = item.get("due_date")
        item["overdue"] = bool(due and item.get("status") == "open" and due < now)
    return items


async def summary(db: AsyncIOMotorDatabase, user: CurrentUser) -> Dict[str, int]:
    """Due today / this week / this month / overdue - the exact question the
    client's message said the workspace had no screen to answer."""
    now = utcnow()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    week_end = today_end + timedelta(days=(6 - now.weekday()))
    month_end = (now.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)

    if user.role == Role.PARTNER:
        client_ids = await assigned_client_ids(db, user.id)

        async def _count(**rng: Any) -> int:
            return await db.deadlines.count_documents({"$or": [
                {"status": "open", "owner_id": user.id, "due_date": rng},
                {"status": "open", "client_id": {"$in": client_ids}, "due_date": rng},
            ]})
    else:
        base = {**_scope(user), "status": "open"}

        async def _count(**rng: Any) -> int:
            return await db.deadlines.count_documents({**base, "due_date": rng})

    return {
        "overdue": await _count(**{"$lt": now}),
        "today": await _count(**{"$gte": now, "$lte": today_end}),
        "this_week": await _count(**{"$gte": now, "$lte": week_end}),
        "this_month": await _count(**{"$gte": now, "$lte": month_end}),
    }


async def dismiss(db: AsyncIOMotorDatabase, user: CurrentUser, deadline_id: str) -> None:
    """A consultant clears a deadline by hand - the underlying record is
    still whatever it was, this only stops the worklist chasing it."""
    await db.deadlines.update_one({"_id": oid(deadline_id)},
                                  {"$set": {"status": "dismissed", "updated_at": utcnow()}})
