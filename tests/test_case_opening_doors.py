"""Every door into a case makes the same decision, and a partner only sees the
requests they were actually given work on.

Two defects found by driving the deployed platform end to end:

* A case opened from a request copied the procedure's document list onto itself
  but never turned it into document requests, so the client had nothing to
  upload, the case showed an empty checklist, and the guard that stops a case
  closing with documents outstanding counted zero and let it through.
  Completing a consultation was worse: it ignored the procedure altogether.
* `GET /requests` and `GET /requests/{id}` had no partner branch at all, so a
  partner could read the whole intake queue and pull any client's email and
  phone out of it.
"""
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import DocumentStatus, RequestStatus, Role
from app.core.exceptions import Forbidden
from app.core.utils import utcnow
from app.modules.requests import service as requests_service
from app.modules.requests.schemas import ConsultationOutcomeIn
from app.schemas.common import PageParams


class FakeUser:
    def __init__(self, user_id, role):
        self.id = user_id
        self.role = role
        self.raw = {"full_name": f"{role.value} user"}


class Payload:
    """Stands in for the pydantic bodies the routers hand the service."""

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


@pytest.fixture
def db():
    return AsyncMongoMockClient()["webimove_tenant_test"]


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """Opening a case notifies the client; delivery is another test's subject."""
    from app.services import push

    monkeypatch.setattr(push, "dispatch", lambda *a, **k: None)


PROCEDURE = {
    "name": "Student visa",
    "area_key": "immigration",
    "default_deadline_days": 90,
    "workflow_stages": [
        {"key": "documents", "label": "Document collection"},
        {"key": "review", "label": "Review"},
        {"key": "submission", "label": "Submission"},
        {"key": "decision", "label": "Decision"},
    ],
    "required_documents": [
        {"name": "Passport", "category": "identity", "mandatory": True},
        {"name": "Letter of acceptance", "category": "supporting", "mandatory": True},
        {"name": "Proof of funds", "category": "financial", "mandatory": True},
    ],
    "client_fields": [
        {"key": "passport_number", "label": "Passport number", "required": True},
        {"key": "nationality", "label": "Nationality", "required": True},
    ],
}


@pytest.fixture
async def seeded(db):
    consultant = await db.users.insert_one({"role": Role.CONSULTANT_OWNER.value,
                                            "full_name": "Consultant"})
    partner = await db.users.insert_one({"role": Role.PARTNER.value})
    client = await db.users.insert_one({"role": Role.CLIENT.value,
                                        "full_name": "Ana Julia",
                                        "email": "ana@example.com",
                                        "mobile": "+34600000000"})
    other_client = await db.users.insert_one({"role": Role.CLIENT.value,
                                              "full_name": "Someone Else",
                                              "email": "other@example.com",
                                              "mobile": "+34611111111"})
    procedure = await db.procedures.insert_one(dict(PROCEDURE))

    consultant_id = str(consultant.inserted_id)

    async def make_request(client_oid, reference):
        result = await db.requests.insert_one({
            "reference": reference,
            "client_id": str(client_oid),
            "client_name": "Ana Julia",
            "consultant_id": consultant_id,
            "status": RequestStatus.NEW.value,
            "visa_type": "Student visa",
            "destination_country": "Spain",
            "process_area": "immigration",
            "procedure_id": str(procedure.inserted_id),
            "case_id": None,
            "created_at": utcnow(),
        })
        return str(result.inserted_id)

    return {
        "consultant": FakeUser(consultant_id, Role.CONSULTANT_OWNER),
        "partner": FakeUser(str(partner.inserted_id), Role.PARTNER),
        "client_id": str(client.inserted_id),
        "other_client_id": str(other_client.inserted_id),
        "procedure_id": str(procedure.inserted_id),
        "mine": await make_request(client.inserted_id, "REQ-001"),
        "theirs": await make_request(other_client.inserted_id, "REQ-002"),
    }


class TestOpeningACaseFromARequest:
    async def test_the_procedure_checklist_becomes_documents_the_client_can_upload(
            self, db, seeded):
        await requests_service.open_case(
            db, seeded["consultant"], seeded["mine"], Payload(deadline=None))

        rows = await db.documents.find({}).to_list(None)
        assert [r["name"] for r in rows] == [
            "Passport", "Letter of acceptance", "Proof of funds"]
        assert {r["status"] for r in rows} == {DocumentStatus.UPLOAD_NEEDED.value}
        assert all(r["is_required"] for r in rows)
        # Unknown categories are coerced, never rejected.
        assert {r["category"] for r in rows} <= {"identity", "financial", "other"}

    async def test_the_case_carries_the_procedure_stages_and_deadline(self, db, seeded):
        await requests_service.open_case(
            db, seeded["consultant"], seeded["mine"], Payload(deadline=None))

        case = await db.cases.find_one({})
        assert [s["key"] for s in case["workflow_stages"]] == [
            "documents", "review", "submission", "decision"]
        assert case["stage"] == "documents"
        assert case["deadline"] is not None
        assert len(case["client_fields"]) == 2

    async def test_every_document_is_tied_to_the_case_so_the_guard_can_count_them(
            self, db, seeded):
        await requests_service.open_case(
            db, seeded["consultant"], seeded["mine"], Payload(deadline=None))
        case = await db.cases.find_one({})

        outstanding = await db.documents.count_documents({
            "case_id": str(case["_id"]),
            "is_required": True,
            "status": {"$ne": DocumentStatus.APPROVED.value},
        })
        assert outstanding == 3


class TestCompletingAConsultation:
    async def test_the_case_it_opens_knows_its_procedure(self, db, seeded):
        # This door is only open once everything asked for has been approved.
        await db.documents.insert_one({
            "request_id": seeded["mine"],
            "client_id": seeded["client_id"],
            "name": "Passport",
            "status": DocumentStatus.APPROVED.value,
            "is_required": True,
        })

        await requests_service.complete_consultation(
            db, seeded["consultant"], seeded["mine"],
            Payload(case_type=None, deadline=None,
                    outcome=ConsultationOutcomeIn(summary="Consultation done.")))

        case = await db.cases.find_one({})
        assert case["procedure_name"] == "Student visa"
        assert len(case["required_documents"]) == 3
        assert len(case["client_fields"]) == 2
        assert [s["key"] for s in case["workflow_stages"]] == [
            "documents", "review", "submission", "decision"]
        assert case["deadline"] is not None

    async def test_it_does_not_ask_again_for_documents_already_approved(self, db, seeded):
        await db.documents.insert_one({
            "request_id": seeded["mine"],
            "client_id": seeded["client_id"],
            "name": "Passport",
            "status": DocumentStatus.APPROVED.value,
            "is_required": True,
        })

        await requests_service.complete_consultation(
            db, seeded["consultant"], seeded["mine"],
            Payload(case_type=None, deadline=None,
                    outcome=ConsultationOutcomeIn(summary="Consultation done.")))

        # Still the one document, not the procedure's three on top of it.
        assert await db.documents.count_documents({}) == 1


class TestWhatAPartnerMaySeeOfARequest:
    async def test_the_queue_is_not_theirs_to_read(self, db, seeded):
        page = await requests_service.list_requests(
            db, seeded["partner"], PageParams())
        assert page["items"] == []

    async def test_one_they_hold_no_work_on_is_refused(self, db, seeded):
        with pytest.raises(Forbidden):
            await requests_service.get_request(db, seeded["partner"], seeded["theirs"])

    async def test_a_delegated_request_carries_no_way_to_contact_the_client(
            self, db, seeded):
        await requests_service.open_case(
            db, seeded["consultant"], seeded["mine"], Payload(deadline=None))
        case = await db.cases.find_one({})
        await db.tasks.insert_one({
            "case_id": str(case["_id"]),
            "client_id": seeded["client_id"],
            "assignee_id": seeded["partner"].id,
            "title": "Translate the passport",
        })

        out = await requests_service.get_request(
            db, seeded["partner"], seeded["mine"])

        profile = out["client_profile"]
        assert profile["full_name"] == "Ana Julia"
        assert "email" not in profile
        assert "mobile" not in profile

    async def test_a_consultant_still_sees_the_whole_queue(self, db, seeded):
        page = await requests_service.list_requests(
            db, seeded["consultant"], PageParams())
        assert len(page["items"]) == 2
