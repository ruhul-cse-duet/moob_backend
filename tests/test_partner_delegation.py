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
from app.core.utils import utcnow
from app.services.ownership import (
    assert_client_access,
    delegated_case_ids,
    partner_holds_case,
)


class FakeUser:
    def __init__(self, user_id, role):
        self.id = user_id
        self.role = role
        # The services log who acted, and read the name off the raw document.
        self.raw = {"full_name": f"{role.value} user"}


@pytest.fixture
def db():
    return AsyncMongoMockClient()["webimove_tenant_test"]


@pytest.fixture(autouse=True)
def no_push(monkeypatch):
    """Approving notifies the client, and the push leaves a detached task
    behind that outlives the test's event loop. Delivery is test_push's
    subject, not this one's."""
    from app.services import push

    monkeypatch.setattr(push, "dispatch", lambda *a, **k: None)


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


# The case screen offers a delegated partner the same controls the consultant
# has — approve, return, re-read, advance the stage. These go through the real
# services, so the screen and the server cannot drift apart on who may act.


async def _case_with_document(db, seeded, *, status="with_consultant"):
    case = await db.cases.insert_one({
        "reference": "CAS-900", "client_id": seeded["client_id"],
        "consultant_id": seeded["consultant"].id,
        "stage": "documents_uploaded", "progress": 30, "timeline": []})
    case_id = str(case.inserted_id)
    document = await db.documents.insert_one({
        "name": "Passport", "client_id": seeded["client_id"],
        "consultant_id": seeded["consultant"].id, "case_id": case_id,
        "status": status, "file": {"file_id": "f1", "original_name": "p.png"}})
    return case_id, str(document.inserted_id)


async def test_delegated_partner_can_approve_a_document(db, seeded):
    from app.modules.documents import service as documents

    case_id, document_id = await _case_with_document(db, seeded)
    await _give_task(db, seeded["partner"], case_id=case_id,
                     client_id=seeded["client_id"])

    await documents.approve(db, seeded["partner"], document_id)

    stored = await db.documents.find_one({"_id": ObjectId(document_id)})
    assert stored["status"] == "approved"
    assert stored["approved_by"] == seeded["partner"].id


async def test_delegated_partner_can_return_a_document(db, seeded):
    from app.modules.documents import service as documents

    case_id, document_id = await _case_with_document(db, seeded)
    await _give_task(db, seeded["partner"], case_id=case_id,
                     client_id=seeded["client_id"])

    await documents.reject(db, seeded["partner"], document_id, "Page 2 is cut off")

    stored = await db.documents.find_one({"_id": ObjectId(document_id)})
    assert stored["status"] == "needs_reupload"
    # The reason travels — the client screen leads with it.
    assert stored["consultant_feedback"] == "Page 2 is cut off"


async def test_delegated_partner_can_advance_the_stage(db, seeded):
    from app.modules.cases import service as cases
    from app.modules.cases.schemas import AdvanceStage

    case_id, _ = await _case_with_document(db, seeded)
    await _give_task(db, seeded["partner"], case_id=case_id,
                     client_id=seeded["client_id"])

    await cases.advance_stage(db, seeded["partner"], case_id, AdvanceStage())

    stored = await db.cases.find_one({"_id": ObjectId(case_id)})
    assert stored["stage"] != "documents_uploaded"


async def test_a_partner_without_the_task_is_still_refused(db, seeded):
    from app.modules.documents import service as documents

    _case_id, document_id = await _case_with_document(db, seeded)
    # No task on this case for the outsider.
    with pytest.raises(Forbidden):
        await documents.approve(db, seeded["outsider"], document_id)


# Requesting documents and closing the consultation were consultant-only, so a
# partner who had done the reviewing had to stop and have the consultant click
# the last button. Both now admit the partner the work was delegated to — and
# only that partner.


async def _request_with_case(db, seeded, *, approved=True):
    case = await db.cases.insert_one({
        "reference": "CAS-901", "client_id": seeded["client_id"],
        "consultant_id": seeded["consultant"].id,
        "stage": "consultant_review", "progress": 0, "timeline": []})
    case_id = str(case.inserted_id)
    request = await db.requests.insert_one({
        "reference": "REQ-901", "client_id": seeded["client_id"],
        "client_name": "Client Ahsan",
        "consultant_id": seeded["consultant"].id,
        "visa_type": "Work Permit", "destination_country": "Canada",
        "purpose": "work", "status": "documents_requested",
        "case_id": case_id, "created_at": utcnow()})
    request_id = str(request.inserted_id)
    await db.documents.insert_one({
        "name": "Passport", "client_id": seeded["client_id"],
        "request_id": request_id, "case_id": case_id,
        "status": "approved" if approved else "with_consultant",
        "file": {"file_id": "f1", "original_name": "p.png"}})
    return case_id, request_id


async def test_delegated_partner_can_request_more_documents(db, seeded):
    from app.modules.requests import service as requests

    from app.modules.requests.schemas import (RequestDocumentsRequest,
                                              RequestedDocument)

    ask = RequestDocumentsRequest(
        documents=[RequestedDocument(name="Bank Statement")])

    case_id, request_id = await _request_with_case(db, seeded)
    await _give_task(db, seeded["partner"], case_id=case_id,
                     client_id=seeded["client_id"])

    await requests.request_documents(db, seeded["partner"], request_id, ask)

    asked = await db.documents.find_one({"name": "Bank Statement"})
    assert asked is not None


async def test_delegated_partner_can_complete_the_consultation(db, seeded):
    from app.modules.requests import service as requests

    from app.modules.requests.schemas import (CompleteConsultation,
                                              ConsultationOutcomeIn)

    payload = CompleteConsultation(
        outcome=ConsultationOutcomeIn(summary="Eligible for the work permit"),
        case_type="Work Permit")

    case_id, request_id = await _request_with_case(db, seeded)
    # An un-completed request carries no case id — completing it is what
    # creates one. Delegating opened a case up front and stamped it with the
    # request, which is the link the partner's access is found through.
    await db.requests.update_one({"_id": ObjectId(request_id)},
                                 {"$set": {"case_id": None}})
    await db.cases.update_one({"_id": ObjectId(case_id)},
                              {"$set": {"request_id": request_id}})
    await _give_task(db, seeded["partner"], case_id=case_id,
                     client_id=seeded["client_id"])

    await requests.complete_consultation(
        db, seeded["partner"], request_id, payload)

    stored = await db.requests.find_one({"_id": ObjectId(request_id)})
    assert stored["status"] == "completed"
    assert stored["case_id"]


async def test_an_undelegated_partner_cannot_touch_the_request(db, seeded):
    from app.modules.requests import service as requests

    from app.modules.requests.schemas import (RequestDocumentsRequest,
                                              RequestedDocument)

    ask = RequestDocumentsRequest(
        documents=[RequestedDocument(name="Bank Statement")])

    _case_id, request_id = await _request_with_case(db, seeded)

    # No task on this case, so no handover to honour.
    with pytest.raises(Forbidden):
        await requests.request_documents(
            db, seeded["outsider"], request_id, ask)
