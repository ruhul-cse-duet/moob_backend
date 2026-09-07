"""Who is allowed to put work into a consultant's queue.

The client asked for this: a request is the consultant's work, so the consultant
is the one who opens it. A client may still ask - they are the person who knows
they need something - but asking is not the same as scheduling, and until now it
was: `POST /requests` was client-only and dropped straight into the queue as
`new`, which meant anyone with an account could create work nobody accepted.

So there are two doors now, and they lead to different places.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import RequestStatus, Role
from app.schemas.common import PageParams
from app.core.exceptions import BadRequest, Forbidden
from app.modules.requests import service

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


class TestAConsultantOpensARequest:
    async def test_it_goes_straight_into_the_queue(self, db):
        out = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        assert out["status"] == RequestStatus.NEW.value
        assert out["client_id"] == str(CLIENT_ID)
        assert out["client_name"] == "Ayesha Rahman"
        assert out["raised_by_consultant"] is True
        # Approved by the act of creating it, so nothing is left to press.
        assert out["approved_at"] is not None

    async def test_the_client_is_the_one_told_about_it(self, db):
        await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        assert db.told[0]["user_ids"] == [str(CLIENT_ID)]
        assert db.told[0]["title_key"] == "notify.request_opened_for_you"

    async def test_a_request_needs_a_client_to_be_for(self, db):
        with pytest.raises(BadRequest):
            await service.create_request(db, _consultant(), _Payload())


class TestAClientAsksForOne:
    async def test_it_waits_for_the_consultant(self, db):
        out = await service.create_request(db, _client(), _Payload())

        assert out["status"] == RequestStatus.PENDING_APPROVAL.value
        assert out["raised_by_consultant"] is False
        assert out["approved_at"] is None

    async def test_the_consultant_is_asked_not_informed(self, db):
        await service.create_request(db, _client(), _Payload())

        assert db.told[0]["user_ids"] == [str(CONSULTANT_ID)]
        assert db.told[0]["title_key"] == "notify.request_needs_approval"

    async def test_a_client_cannot_open_one_for_somebody_else(self, db):
        # The body is ignored for a client; the token decides whose it is.
        someone_else = str(ObjectId())
        out = await service.create_request(
            db, _client(), _Payload(client_id=someone_else))

        assert out["client_id"] == str(CLIENT_ID)


class TestTheConsultantDecides:
    async def _pending(self, db):
        return await service.create_request(db, _client(), _Payload())

    async def test_approving_moves_it_into_the_queue(self, db):
        pending = await self._pending(db)
        db.told.clear()

        out = await service.approve_request(db, _consultant(), pending["id"])

        assert out["status"] == RequestStatus.NEW.value
        assert out["approved_by"] == str(CONSULTANT_ID)
        assert db.told[0]["user_ids"] == [str(CLIENT_ID)]
        assert db.told[0]["title_key"] == "notify.request_approved"

    async def test_declining_keeps_the_record_and_says_why(self, db):
        pending = await self._pending(db)
        db.told.clear()

        out = await service.decline_request(
            db, _consultant(), pending["id"], "You already have an open case.")

        assert out["status"] == RequestStatus.DECLINED.value
        assert out["decline_reason"] == "You already have an open case."
        # Kept, not deleted - the client asked and is owed the answer.
        assert await db.requests.count_documents({}) == 1
        assert db.told[0]["title_key"] == "notify.request_declined"

    async def test_a_request_already_in_the_queue_cannot_be_approved_again(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        with pytest.raises(BadRequest):
            await service.approve_request(db, _consultant(), opened["id"])

    async def test_documents_cannot_be_asked_for_before_approval(self, db):
        pending = await self._pending(db)

        with pytest.raises(BadRequest):
            await service.request_documents(db, _consultant(), pending["id"], None)


async def test_a_partner_cannot_open_a_request(db):
    partner = _User(str(ObjectId()), Role.PARTNER, {"full_name": "Nadia Volkova"})

    with pytest.raises(Forbidden):
        await service.create_request(db, partner, _Payload(client_id=str(CLIENT_ID)))


class TestWhatTheClientIsShown:
    """Four honest answers, not the consultant's five queue tabs.

    `new`, `waiting_for_client`, `documents_received` and `under_review` are how
    the consultant organises their own work. To the client they all mean the
    same thing - somebody is working on it - and showing the internal one made a
    note the consultant wrote to themselves read as a status about the client.
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

        # Better to say "processing" than to show a raw code, or nothing.
        assert client_status("something_added_later") == "processing"

    def test_every_client_facing_status_has_words(self):
        from app.core.enums import CLIENT_FACING_STATUS
        from app.core.i18n import CATALOGUE

        for shown in set(CLIENT_FACING_STATUS.values()):
            assert f"status.{shown}" in CATALOGUE, shown

    async def test_approving_moves_the_client_from_waiting_to_processing(self, db):
        pending = await service.create_request(db, _client(), _Payload())
        listed = await service.list_requests(db, _client(), PageParams())
        assert listed["items"][0]["client_status"] == "pending_approval"

        await service.approve_request(db, _consultant(), pending["id"])

        listed = await service.list_requests(db, _client(), PageParams())
        assert listed["items"][0]["status"] == RequestStatus.NEW.value
        assert listed["items"][0]["client_status"] == "processing"
        assert listed["items"][0]["client_status_label"] == "Processing"

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

    The line is whether a consultant has started. Before that the request is a
    message nobody has answered; after it, files and a checklist hang off it, and
    deleting does not undo that work - it hides it.
    """

    async def test_a_client_can_remove_one_that_is_still_waiting(self, db):
        pending = await service.create_request(db, _client(), _Payload())

        out = await service.delete_request(db, _client(), pending["id"])

        assert out["deleted"] is True
        assert await db.requests.count_documents({}) == 0

    async def test_a_declined_one_can_be_cleared_away(self, db):
        pending = await service.create_request(db, _client(), _Payload())
        await service.decline_request(db, _consultant(), pending["id"], "Not eligible.")

        await service.delete_request(db, _client(), pending["id"])

        assert await db.requests.count_documents({}) == 0

    async def test_work_in_progress_is_not_deletable_by_the_client(self, db):
        opened = await service.create_request(
            db, _consultant(), _Payload(client_id=str(CLIENT_ID)))

        with pytest.raises(BadRequest):
            await service.delete_request(db, _client(), opened["id"])

        assert await db.requests.count_documents({}) == 1

    async def test_a_request_with_a_case_is_never_the_clients_to_delete(self, db):
        pending = await service.create_request(db, _client(), _Payload())
        await db.requests.update_one({"_id": ObjectId(pending["id"])},
                                     {"$set": {"case_id": str(ObjectId())}})

        with pytest.raises(BadRequest):
            await service.delete_request(db, _client(), pending["id"])

    async def test_a_client_cannot_remove_somebody_elses(self, db):
        pending = await service.create_request(db, _client(), _Payload())
        stranger = _User(str(ObjectId()), Role.CLIENT, {"full_name": "Someone Else"})

        with pytest.raises(Forbidden):
            await service.delete_request(db, stranger, pending["id"])

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

        pending = await service.create_request(db, _client(), _Payload())
        await db.documents.insert_many([
            {"request_id": pending["id"], "name": "Passport",
             "file": {"file_id": "gridfs-1"}},
            {"request_id": pending["id"], "name": "Bank Statement"},
        ])

        out = await service.delete_request(db, _client(), pending["id"])

        assert out["documents_removed"] == 2
        assert await db.documents.count_documents({}) == 0
        # The blob too - GridFS would otherwise keep it for the life of the tenant.
        assert dropped == ["gridfs-1"]

    async def test_the_consultant_is_told_when_a_client_withdraws(self, db):
        pending = await service.create_request(db, _client(), _Payload())
        db.told.clear()

        await service.delete_request(db, _client(), pending["id"])

        assert db.told[0]["user_ids"] == [str(CONSULTANT_ID)]
        assert db.told[0]["title_key"] == "notify.request_withdrawn"
