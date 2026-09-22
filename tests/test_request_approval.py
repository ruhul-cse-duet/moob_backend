"""Who opens a request, and what happens to it afterwards.

The product review settled this: WebImove's customer is the consultancy, and the
consultant decides which procedure a client follows. So the consultant opens the
request. A client cannot - not because their queue permissions are wrong, but
because choosing the procedure was never theirs to do.

An earlier build let a client raise one that waited at PENDING_APPROVAL for the
consultant to accept. That status and `approve_request` still exist, for the rows
created while it did; nothing creates a new one. The tests below build those rows
directly, which is the only way they can come about now.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import CaseStage, RequestStatus, Role
from app.core.exceptions import BadRequest, Forbidden
from app.core.utils import utcnow
from app.core.i18n import DEFAULT_LANGUAGE, translate
from app.modules.requests import service
from app.schemas.common import PageParams

CLIENT_ID = ObjectId()
CONSULTANT_ID = ObjectId()


class _User:
    def __init__(self, id, role, raw):
        self.id = id
        self.role = role
        self.raw = raw


def _client():
    return _User(str(CLIENT_ID), Role.CLIENT,
                 {"full_name": "Ayesha Rahman", "consultant_id": str(CONSULTANT_ID),
                  "country_of_residence": "Portugal"})


def _consultant():
    return _User(str(CONSULTANT_ID), Role.CONSULTANT_OWNER,
                 {"full_name": "Sarah Jenkins"})


class _Payload:
    def __init__(self, **kwargs):
        self.client_id = kwargs.get("client_id")
        self.visa_type = kwargs.get("visa_type", "Student Visa")
        self.destination_country = kwargs.get("destination_country", "Canada")
        self.purpose = kwargs.get("purpose", "Masters programme")
        self.additional_information = None
        self.client_notes = None
        self.preferred_appointment = None
        self.consultant_id = kwargs.get("consultant_id")
        self.attached_files = []
        self.is_draft = kwargs.get("is_draft", False)


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["webimove_tenant_test"]
    await database.users.insert_one({
        "_id": CLIENT_ID, "full_name": "Ayesha Rahman", "role": Role.CLIENT.value,
        "country_of_residence": "Portugal", "consultant_id": str(CONSULTANT_ID)})
    await database.users.insert_one({
        "_id": CONSULTANT_ID, "full_name": "Sarah Jenkins",
        "role": Role.CONSULTANT_OWNER.value})

    told = []

    async def _notify(_db, **kwargs):
        told.append(kwargs)

    async def _noop(*args, **kwargs):
        return None

    async def _seq(*args, **kwargs):
        return 101

    monkeypatch.setattr(service, "notify", _notify)
    monkeypatch.setattr(service, "log_activity", _noop)
    monkeypatch.setattr(service, "next_sequence", _seq)
    monkeypatch.setattr(service, "assert_request_access", _noop)
    database.told = told
    return database


async def _legacy_pending(db, status=RequestStatus.PENDING_APPROVAL.value):
    """A row from before the consultant became the only one who opens requests."""
    result = await db.requests.insert_one({
        "reference": "REQ-100", "visa_type": "Student Visa",
        "destination_country": "Canada", "purpose": "Masters programme",
        "status": status, "client_id": str(CLIENT_ID),
        "client_name": "Ayesha Rahman", "consultant_id": str(CONSULTANT_ID),
        "raised_by_consultant": False, "case_id": None, "is_draft": False,
    })
    return str(result.inserted_id)


class TestOnlyTheConsultantOpensOne:
    async def test_it_goes_straight_into_the_queue(self, db):
        out = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        assert out["status"] == RequestStatus.NEW.value
        assert out["client_id"] == str(CLIENT_ID)
        assert out["client_name"] == "Ayesha Rahman"
        # Decided by the act of opening it, so nothing is left to press.
        assert out["approved_at"] is not None

    async def test_the_client_is_told_what_was_opened_for_them(self, db):
        await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        assert db.told[0]["user_ids"] == [str(CLIENT_ID)]
        assert db.told[0]["title_key"] == "notify.request_opened_for_you"

    async def test_a_request_needs_a_client_to_be_for(self, db):
        with pytest.raises(BadRequest):
            await service.create_request(db, _consultant(), _Payload())

    async def test_a_client_cannot_open_one(self, db):
        with pytest.raises(Forbidden, match="consultant opens requests"):
            await service.create_request(db, _client(), _Payload())

        assert await db.requests.count_documents({}) == 0

    async def test_a_partner_cannot_either(self, db):
        partner = _User(str(ObjectId()), Role.PARTNER, {"full_name": "Nadia Volkova"})

        with pytest.raises(Forbidden):
            await service.create_request(
                db, partner, _Payload(client_id=str(CLIENT_ID)))


class TestOpeningTheCase:
    """Finding 13: the request had nowhere to go.

    The only route to a case was `complete_consultation`, which requires every
    requested document to be approved - the gate for *closing* a consultation,
    used to open one. Documents are collected inside the case, so requiring them
    first meant the process could not start at all, and the reviewer stopped
    testing there.
    """

    async def test_a_case_opens_without_the_document_checklist(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))
        db.told.clear()

        case = await service.open_case(db, _consultant(), opened["id"], _OpenCase())

        assert case["reference"].startswith("CAS")
        assert case["client_id"] == str(CLIENT_ID)
        assert await db.cases.count_documents({}) == 1

    async def test_the_request_is_bound_to_it(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        case = await service.open_case(db, _consultant(), opened["id"], _OpenCase())

        request = await db.requests.find_one({"_id": ObjectId(opened["id"])})
        assert request["case_id"] == case["id"]
        assert request["status"] == RequestStatus.UNDER_REVIEW.value

    async def test_documents_already_raised_move_with_it(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))
        await db.documents.insert_one(
            {"request_id": opened["id"], "name": "Passport", "case_id": None})

        case = await service.open_case(db, _consultant(), opened["id"], _OpenCase())

        # Anything scoped by case - a delegated partner's access among it -
        # cannot see a document whose case_id is still null.
        document = await db.documents.find_one({"name": "Passport"})
        assert document["case_id"] == case["id"]

    async def test_the_client_hears_that_their_case_is_open(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))
        db.told.clear()

        await service.open_case(db, _consultant(), opened["id"], _OpenCase())

        assert db.told[0]["user_ids"] == [str(CLIENT_ID)]
        assert db.told[0]["title_key"] == "notify.case_opened"

    async def test_the_consultant_may_assign_the_procedure_here(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        case = await service.open_case(
            db, _consultant(), opened["id"],
            _OpenCase(process_area="labour", case_type="Dismissal claim"))

        assert case["process_area"] == "labour"
        assert case["case_type"] == "Dismissal claim"

    async def test_a_second_case_is_refused(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))
        await service.open_case(db, _consultant(), opened["id"], _OpenCase())

        with pytest.raises(BadRequest, match="already been opened"):
            await service.open_case(db, _consultant(), opened["id"], _OpenCase())

    async def test_an_unapproved_legacy_request_is_refused(self, db):
        request_id = await _legacy_pending(db)

        with pytest.raises(BadRequest, match="Approve this request"):
            await service.open_case(db, _consultant(), request_id, _OpenCase())


class _OpenCase:
    def __init__(self, **kwargs):
        self.process_area = kwargs.get("process_area")
        self.procedure_id = kwargs.get("procedure_id")
        self.case_type = kwargs.get("case_type")
        self.deadline = kwargs.get("deadline")


class TestLegacyPendingRows:
    """Approve and decline still work, for the rows that already exist."""

    async def test_approving_moves_it_into_the_queue(self, db):
        request_id = await _legacy_pending(db)

        out = await service.approve_request(db, _consultant(), request_id)

        assert out["status"] == RequestStatus.NEW.value
        assert out["approved_by"] == str(CONSULTANT_ID)
        assert db.told[0]["title_key"] == "notify.request_approved"

    async def test_declining_keeps_the_record_and_says_why(self, db):
        request_id = await _legacy_pending(db)

        out = await service.decline_request(
            db, _consultant(), request_id, "You already have an open case.")

        assert out["status"] == RequestStatus.DECLINED.value
        assert out["decline_reason"] == "You already have an open case."
        # Kept, not deleted - the client asked and is owed the answer.
        assert await db.requests.count_documents({}) == 1

    async def test_a_request_already_in_the_queue_cannot_be_approved(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        with pytest.raises(BadRequest):
            await service.approve_request(db, _consultant(), opened["id"])

    async def test_documents_cannot_be_asked_for_before_approval(self, db):
        request_id = await _legacy_pending(db)

        with pytest.raises(BadRequest):
            await service.request_documents(db, _consultant(), request_id, None)


class TestWhatTheClientIsShown:
    """Four honest answers, not the consultant's own queue tabs.

    `new`, `waiting_for_client`, `documents_received` and `under_review` are how
    the consultant organises their work. To the client they all mean the same
    thing - somebody is working on it - and showing the internal one made a note
    the consultant wrote to themselves read as a status about the client.
    """

    def test_everything_in_the_queue_reads_as_processing(self):
        from app.core.enums import client_status

        for internal in ("new", "waiting_for_client", "documents_received",
                         "under_review"):
            assert client_status(internal) == "processing"

    def test_the_three_the_client_acted_on_keep_their_own_word(self):
        from app.core.enums import client_status

        assert client_status("pending_approval") == "pending_approval"
        assert client_status("completed") == "completed"
        assert client_status("declined") == "declined"

    def test_a_status_we_do_not_know_still_reads_as_work(self):
        from app.core.enums import client_status

        assert client_status("something_added_later") == "processing"

    def test_every_client_facing_status_has_words(self):
        from app.core.enums import CLIENT_FACING_STATUS
        from app.core.i18n import CATALOGUE

        for shown in set(CLIENT_FACING_STATUS.values()):
            assert f"status.{shown}" in CATALOGUE, shown

    async def test_a_request_opened_for_them_reads_as_processing(self, db):
        await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        listed = await service.list_requests(db, _client(), PageParams())

        assert listed["items"][0]["status"] == RequestStatus.NEW.value
        assert listed["items"][0]["client_status"] == "processing"
        assert listed["items"][0]["client_status_label"] == translate(
            "status.processing", DEFAULT_LANGUAGE)

    async def test_a_finished_request_reads_as_completed(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))
        await db.requests.update_one(
            {"_id": ObjectId(opened["id"])},
            {"$set": {"status": RequestStatus.COMPLETED.value}})

        listed = await service.list_requests(db, _client(), PageParams())
        assert listed["items"][0]["client_status"] == "completed"


class TestWithdrawingARequest:
    """A client may take back what is still only theirs.

    Which, now that only consultants open requests, means the rows raised before
    that changed. The consultant can remove any of their own.
    """

    async def test_a_client_can_remove_a_legacy_pending_one(self, db):
        request_id = await _legacy_pending(db)

        out = await service.delete_request(db, _client(), request_id)

        assert out["deleted"] is True
        assert await db.requests.count_documents({}) == 0

    async def test_a_declined_one_can_be_cleared_away(self, db):
        request_id = await _legacy_pending(db, RequestStatus.DECLINED.value)

        await service.delete_request(db, _client(), request_id)

        assert await db.requests.count_documents({}) == 0

    async def test_work_in_progress_is_not_deletable_by_the_client(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        with pytest.raises(BadRequest):
            await service.delete_request(db, _client(), opened["id"])

        assert await db.requests.count_documents({}) == 1

    async def test_a_request_with_a_case_is_never_the_clients_to_delete(self, db):
        request_id = await _legacy_pending(db)
        await db.requests.update_one({"_id": ObjectId(request_id)},
                                     {"$set": {"case_id": str(ObjectId())}})

        with pytest.raises(BadRequest):
            await service.delete_request(db, _client(), request_id)

    async def test_a_client_cannot_remove_somebody_elses(self, db):
        request_id = await _legacy_pending(db)
        stranger = _User(str(ObjectId()), Role.CLIENT, {"full_name": "Someone Else"})

        with pytest.raises(Forbidden):
            await service.delete_request(db, stranger, request_id)

    async def test_the_consultant_can_remove_any_of_theirs(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        out = await service.delete_request(db, _consultant(), opened["id"])

        assert out["deleted"] is True

    async def test_the_documents_go_with_it(self, db, monkeypatch):
        dropped = []

        async def _delete_file(_db, file_id, bucket):
            dropped.append(file_id)

        monkeypatch.setattr(service.storage, "delete_file", _delete_file)

        request_id = await _legacy_pending(db)
        await db.documents.insert_many([
            {"request_id": request_id, "name": "Passport",
             "file": {"file_id": "gridfs-1"}},
            {"request_id": request_id, "name": "Bank Statement"},
        ])

        out = await service.delete_request(db, _client(), request_id)

        assert out["documents_removed"] == 2
        assert await db.documents.count_documents({}) == 0
        # The blob too - GridFS would otherwise keep it for the life of the tenant.
        assert dropped == ["gridfs-1"]

    async def test_the_consultant_is_told_when_a_client_withdraws(self, db):
        request_id = await _legacy_pending(db)
        db.told.clear()

        await service.delete_request(db, _client(), request_id)

        assert db.told[0]["user_ids"] == [str(CONSULTANT_ID)]
        assert db.told[0]["title_key"] == "notify.request_withdrawn"


class TestTheCaseFollowsTheProcedure:
    """C1: "the case does not use the documents, stages and deadline of the
    procedure."

    The first fix for this went into `create_case` - `POST /cases` - which the
    consultant never touches. They press "Abrir caso", which lands here, and
    that path still opened every case at `new_request` with no deadline. The
    checklist arrived, so it looked half-right on screen and the other
    function's tests stayed green.
    """

    async def _with_procedure(self, db):
        from app.modules.catalog import service as catalog

        await catalog.ensure_defaults(db)
        procedure = await db.procedures.insert_one({
            "area_key": "immigration", "name": "Student visa", "active": True,
            "required_documents": [{"name": "Passport", "mandatory": True}],
            "client_fields": [{"key": "passport_number", "label": "Passport number"}],
            "workflow_stages": [
                {"key": "documents", "name": "Document collection", "duration_days": 14},
                {"key": "review", "name": "Review", "duration_days": 7},
                {"key": "decision", "name": "Decision", "duration_days": 60},
            ],
            "default_deadline_days": 90,
        })
        return str(procedure.inserted_id)

    async def test_the_case_starts_at_the_procedures_first_stage(self, db):
        procedure_id = await self._with_procedure(db)
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        case = await service.open_case(
            db, _consultant(), opened["id"], _OpenCase(procedure_id=procedure_id))

        # Not `new_request` - that is the old fixed immigration ladder.
        assert case["stage"] == "documents"
        assert [s["key"] for s in case["workflow_stages"]] == [
            "documents", "review", "decision"]

    async def test_the_procedures_deadline_is_applied(self, db):
        procedure_id = await self._with_procedure(db)
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        case = await service.open_case(
            db, _consultant(), opened["id"], _OpenCase(procedure_id=procedure_id))

        # 90 days, rather than the "Sem prazo" the review reported.
        assert case["deadline"] is not None
        days = (case["deadline"] - utcnow()).days
        assert 88 <= days <= 90

    async def test_a_chosen_deadline_still_wins(self, db):
        from datetime import timedelta

        procedure_id = await self._with_procedure(db)
        chosen = utcnow() + timedelta(days=5)
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        case = await service.open_case(
            db, _consultant(), opened["id"],
            _OpenCase(procedure_id=procedure_id, deadline=chosen))

        assert case["deadline"] == chosen

    async def test_the_checklist_comes_across_too(self, db):
        procedure_id = await self._with_procedure(db)
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        case = await service.open_case(
            db, _consultant(), opened["id"], _OpenCase(procedure_id=procedure_id))

        assert [d["name"] for d in case["required_documents"]] == ["Passport"]
        assert case["procedure_name"] == "Student visa"

    async def test_a_case_with_no_procedure_still_opens(self, db):
        # A consultation with nothing in the catalogue yet must not break.
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        case = await service.open_case(db, _consultant(), opened["id"], _OpenCase())

        # The old fixed ladder is the fallback, not the default.
        assert case["stage"] == CaseStage.NEW_REQUEST.value
        assert case["workflow_stages"] == []
