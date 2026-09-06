"""Delegating a task hands over the work on that case.

A partner given a task is expected to review the client's documents — approve
them, send them back, re-run the analysis — exactly as the consultant would.
Before this, only a whole-client handover (`assign_partner_to_client`) granted
that, so the everyday delegation left the partner able to see the task and
nothing else.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role, TaskStatus
from app.core.exceptions import Forbidden
from app.services.ownership import (
    assert_client_access,
    delegated_case_ids,
    partner_holds_case,
)


class FakeUser:
    def __init__(self, user_id, role):
        self.id = user_id
        self.role = role


@pytest.fixture
def db():
    return AsyncMongoMockClient()["webimove_tenant_test"]


@pytest.fixture
async def seeded(db):
    consultant = await db.users.insert_one({"role": Role.CONSULTANT_OWNER.value})
    partner = await db.users.insert_one({"role": Role.PARTNER.value})
    outsider = await db.users.insert_one({"role": Role.PARTNER.value})
    client = await db.users.insert_one({"role": Role.CLIENT.value})
    other_client = await db.users.insert_one({"role": Role.CLIENT.value})
    return {
        "consultant": FakeUser(str(consultant.inserted_id), Role.CONSULTANT_OWNER),
        "partner": FakeUser(str(partner.inserted_id), Role.PARTNER),
        "outsider": FakeUser(str(outsider.inserted_id), Role.PARTNER),
        "client_id": str(client.inserted_id),
        "other_client_id": str(other_client.inserted_id),
    }


async def _give_task(db, partner, *, case_id, client_id,
                     status=TaskStatus.IN_PROGRESS):
    await db.tasks.insert_one({
        "assignee_id": partner.id, "case_id": case_id,
        "client_id": client_id, "status": status.value})


async def test_a_task_on_the_case_grants_access(db, seeded):
    await _give_task(db, seeded["partner"], case_id="case1",
                     client_id=seeded["client_id"])
    # Does not raise.
    await assert_client_access(db, seeded["partner"], seeded["client_id"],
                               case_id="case1")


async def test_without_a_task_the_partner_is_still_refused(db, seeded):
    with pytest.raises(Forbidden):
        await assert_client_access(db, seeded["outsider"], seeded["client_id"],
                                   case_id="case1")


async def test_access_does_not_leak_to_the_clients_other_cases(db, seeded):
    """One delegated task is authority over that case, not the whole client."""
    await _give_task(db, seeded["partner"], case_id="case1",
                     client_id=seeded["client_id"])
    with pytest.raises(Forbidden):
        await assert_client_access(db, seeded["partner"], seeded["client_id"],
                                   case_id="case2")


async def test_a_cancelled_task_grants_nothing(db, seeded):
    await _give_task(db, seeded["partner"], case_id="case1",
                     client_id=seeded["client_id"],
                     status=TaskStatus.CANCELLED)
    assert not await partner_holds_case(db, seeded["partner"], "case1")
    with pytest.raises(Forbidden):
        await assert_client_access(db, seeded["partner"], seeded["client_id"],
                                   case_id="case1")


async def test_a_bulk_handover_still_works_without_a_case_id(db, seeded):
    """The original route has to keep working exactly as it did."""
    await db.users.update_one(
        {"_id": ObjectId(seeded["client_id"])},
        {"$set": {"partner_id": seeded["partner"].id}})
    await assert_client_access(db, seeded["partner"], seeded["client_id"])


async def test_consultants_are_unaffected(db, seeded):
    await assert_client_access(db, seeded["consultant"], seeded["client_id"])
    await assert_client_access(db, seeded["consultant"], seeded["other_client_id"],
                               case_id="case9")


async def test_delegated_case_ids_lists_live_tasks_only(db, seeded):
    await _give_task(db, seeded["partner"], case_id="case1",
                     client_id=seeded["client_id"])
    await _give_task(db, seeded["partner"], case_id="case2",
                     client_id=seeded["client_id"],
                     status=TaskStatus.CANCELLED)
    ids = await delegated_case_ids(db, seeded["partner"].id)
    assert ids == ["case1"]


async def test_no_case_id_means_no_grant(db, seeded):
    """A null case must never match the `case_id: None` tasks in the table."""
    await db.tasks.insert_one({
        "assignee_id": seeded["partner"].id, "case_id": None,
        "client_id": seeded["client_id"], "status": TaskStatus.PENDING.value})
    assert not await partner_holds_case(db, seeded["partner"], None)
    with pytest.raises(Forbidden):
        await assert_client_access(db, seeded["partner"], seeded["client_id"])
