"""Who may see a deadline, and who may clear one.

The deadlines worklist mirrors dates from documents, tasks, filings and
appointments into one place so a consultant can ask "what is due". Being one
place is exactly what makes its access rules worth pinning: every date in the
workspace now has a second door, and a worklist that answers the wrong person
undoes the scoping every other screen does carefully.

Two rules, both of which were wrong when the module first landed:

  * dismissing had no check at all, so any signed-in account could clear any
    deadline in the workspace by id - a client silencing the chase on their own
    overdue passport, a partner clearing a filing date they are not shown;
  * a partner could read every deadline belonging to any client assigned to
    them, which is the client contact the product review ruled out. A partner
    sees the deadline on their own task and nothing else.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role
from app.core.exceptions import Forbidden, NotFound
from app.core.utils import utcnow
from app.modules.deadlines import service

OWNER = ObjectId()
CONSULTANT = ObjectId()
OTHER_CONSULTANT = ObjectId()
PARTNER = ObjectId()
CLIENT = ObjectId()


class _User:
    def __init__(self, id, role):
        self.id = str(id)
        self.role = role
        self.raw = {}


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["webimove_tenant_test"]
    # The client's own document deadline, on this consultant's caseload.
    await service.upsert(
        database, kind="document_expiry", source_collection="documents",
        source_id="doc-1", due_date=utcnow(), title="Passport",
        consultant_id=str(CONSULTANT), client_id=str(CLIENT),
        client_name="Ayesha Rahman")
    # The partner's own task deadline.
    await service.upsert(
        database, kind="partner_task", source_collection="tasks",
        source_id="task-1", due_date=utcnow(), title="Sworn translation",
        consultant_id=str(CONSULTANT), client_id=str(CLIENT),
        owner_id=str(PARTNER), owner_name="Marta Vasconcellos")
    return database


async def _id_of(db, source_id):
    row = await db.deadlines.find_one({"source_id": source_id})
    return str(row["_id"])


class TestWhoSeesWhat:
    async def test_the_consultant_sees_the_caseload(self, db):
        items = await service.list_deadlines(db, _User(CONSULTANT, Role.CONSULTANT))

        assert {i["source_id"] for i in items} == {"doc-1", "task-1"}

    async def test_the_client_sees_only_their_own(self, db):
        items = await service.list_deadlines(db, _User(CLIENT, Role.CLIENT))

        assert {i["source_id"] for i in items} == {"doc-1", "task-1"}
        assert all(i["client_id"] == str(CLIENT) for i in items)

    async def test_the_partner_sees_only_their_own_task(self, db):
        """Not the client's document expiry. A partner reading that is the
        contact with the client the review ruled out."""
        items = await service.list_deadlines(db, _User(PARTNER, Role.PARTNER))

        assert [i["source_id"] for i in items] == ["task-1"]

    async def test_the_partners_summary_counts_the_same_rows(self, db):
        # A count that includes what the list hides is the same leak, in a badge.
        summary = await service.summary(db, _User(PARTNER, Role.PARTNER))

        assert sum(summary.values()) > 0
        items = await service.list_deadlines(db, _User(PARTNER, Role.PARTNER))
        assert len(items) == 1


class TestWhoMayClearOne:
    async def test_a_consultant_clears_their_own(self, db):
        deadline_id = await _id_of(db, "doc-1")

        await service.dismiss(db, _User(CONSULTANT, Role.CONSULTANT), deadline_id)

        row = await db.deadlines.find_one({"source_id": "doc-1"})
        assert row["status"] == "dismissed"
        assert row["dismissed_by"] == str(CONSULTANT)

    async def test_a_client_cannot_silence_their_own_deadline(self, db):
        deadline_id = await _id_of(db, "doc-1")

        with pytest.raises(Forbidden):
            await service.dismiss(db, _User(CLIENT, Role.CLIENT), deadline_id)

        row = await db.deadlines.find_one({"source_id": "doc-1"})
        assert row["status"] == "open"

    async def test_a_partner_cannot_either(self, db):
        deadline_id = await _id_of(db, "task-1")

        with pytest.raises(Forbidden):
            await service.dismiss(db, _User(PARTNER, Role.PARTNER), deadline_id)

    async def test_another_consultants_client_is_refused(self, db):
        deadline_id = await _id_of(db, "doc-1")

        with pytest.raises(Forbidden, match="another consultant"):
            await service.dismiss(
                db, _User(OTHER_CONSULTANT, Role.CONSULTANT), deadline_id)

    async def test_the_owner_may_clear_anything_in_the_workspace(self, db):
        deadline_id = await _id_of(db, "doc-1")

        await service.dismiss(db, _User(OWNER, Role.CONSULTANT_OWNER), deadline_id)

        row = await db.deadlines.find_one({"source_id": "doc-1"})
        assert row["status"] == "dismissed"

    async def test_an_unknown_id_is_a_404_not_a_silent_no_op(self, db):
        with pytest.raises(NotFound):
            await service.dismiss(db, _User(CONSULTANT, Role.CONSULTANT),
                                  str(ObjectId()))
