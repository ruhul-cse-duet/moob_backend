"""Invited, Expired or Active - and a way to send the link again.

Two lines of the review's acceptance checklist. The user row answers "invited"
and "active" on its own; it cannot answer "expired", because the expiry lives on
the invitation, in a different database. So a client stuck at Invited for a
fortnight looked identical whether they had not got round to it or had been
locked out since Tuesday - and those need opposite actions from the consultant
reading the list.
"""
from datetime import timedelta

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role, UserStatus
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import utcnow
from app.modules.users import service
from app.schemas.common import PageParams
from app.services import invites

CONSULTANT_ID = ObjectId()
TENANT = {"_id": ObjectId(), "name": "Jenkins Immigration Law"}


class _Consultant:
    def __init__(self, role=Role.CONSULTANT_OWNER):
        self.id = str(CONSULTANT_ID)
        self.role = role
        self.raw = {"full_name": "Sarah Jenkins"}


@pytest.fixture
def mongo(monkeypatch):
    client = AsyncMongoMockClient()
    pdb = client["webimove_platform"]
    tdb = client["webimove_tenant_test"]

    monkeypatch.setattr(invites, "platform_db", lambda: pdb)
    monkeypatch.setattr(service, "platform_db", lambda: pdb)

    async def _sent(*args, **kwargs):
        return True

    monkeypatch.setattr(service, "send_client_invite_email", _sent)
    return {"platform": pdb, "tenant": tdb}


async def _client_row(mongo, email, *, status=UserStatus.INVITED.value,
                      expires_in_days=7, consumed=False):
    tdb, pdb = mongo["tenant"], mongo["platform"]
    result = await tdb.users.insert_one({
        "email": email, "full_name": "Ayesha Rahman", "role": Role.CLIENT.value,
        "status": status, "consultant_id": str(CONSULTANT_ID),
        "created_at": utcnow(),
    })
    entry = {"email": email, "tenant_id": "t1", "user_id": str(result.inserted_id),
             "role": Role.CLIENT.value,
             "invite_expires_at": utcnow() + timedelta(days=expires_in_days)}
    if not consumed:
        entry["invite_token"] = "tok_" + email
    await pdb.user_directory.insert_one(entry)
    return str(result.inserted_id)


class TestWhatTheListShows:
    async def test_a_live_invitation_reads_as_invited(self, mongo):
        await _client_row(mongo, "ayesha@example.com")

        page = await service.list_users(mongo["tenant"], _Consultant(), PageParams())

        assert page["items"][0]["invite_state"] == "invited"
        assert page["items"][0]["invite_expired"] is False

    async def test_a_lapsed_one_reads_as_expired(self, mongo):
        """The state the row alone cannot express, and the one that matters:
        this client cannot get in, and nobody would know from `status`."""
        await _client_row(mongo, "late@example.com", expires_in_days=-1)

        page = await service.list_users(mongo["tenant"], _Consultant(), PageParams())

        assert page["items"][0]["invite_state"] == "expired"
        assert page["items"][0]["invite_expired"] is True

    async def test_an_active_client_is_not_labelled_at_all(self, mongo):
        # Nothing to say about an invitation that was accepted and is gone.
        await _client_row(mongo, "joined@example.com",
                          status=UserStatus.ACTIVE.value, consumed=True)

        page = await service.list_users(mongo["tenant"], _Consultant(), PageParams())

        assert "invite_state" not in page["items"][0]
        assert page["items"][0]["status"] == UserStatus.ACTIVE.value

    async def test_the_whole_list_costs_one_lookup(self, mongo, monkeypatch):
        """Twenty clients must not be twenty round trips to another database."""
        for i in range(5):
            await _client_row(mongo, f"client{i}@example.com")

        calls = {"n": 0}
        original = invites.states_for

        async def counted(emails):
            calls["n"] += 1
            return await original(emails)

        monkeypatch.setattr(service.invites, "states_for", counted)
        await service.list_users(mongo["tenant"], _Consultant(), PageParams())

        assert calls["n"] == 1


class TestResendingTheInvitation:
    async def test_a_fresh_link_is_issued(self, mongo):
        client_id = await _client_row(mongo, "ayesha@example.com")

        out = await service.resend_client_invite(
            mongo["tenant"], TENANT, _Consultant(), client_id)

        assert out["invite_token"]
        assert out["invite_token"] != "tok_ayesha@example.com"

    async def test_the_old_link_stops_working(self, mongo):
        # Which is also how a link that leaked gets revoked.
        client_id = await _client_row(mongo, "ayesha@example.com")

        await service.resend_client_invite(
            mongo["tenant"], TENANT, _Consultant(), client_id)

        entry = await mongo["platform"].user_directory.find_one(
            {"email": "ayesha@example.com"})
        assert entry["invite_token"] != "tok_ayesha@example.com"

    async def test_an_expired_invitation_can_be_revived(self, mongo):
        client_id = await _client_row(mongo, "late@example.com", expires_in_days=-1)

        await service.resend_client_invite(
            mongo["tenant"], TENANT, _Consultant(), client_id)

        page = await service.list_users(mongo["tenant"], _Consultant(), PageParams())
        assert page["items"][0]["invite_state"] == "invited"

    async def test_a_client_who_already_joined_is_refused(self, mongo):
        client_id = await _client_row(mongo, "joined@example.com",
                                      status=UserStatus.ACTIVE.value, consumed=True)

        with pytest.raises(BadRequest, match="already accepted"):
            await service.resend_client_invite(
                mongo["tenant"], TENANT, _Consultant(), client_id)

    async def test_another_consultants_client_is_refused(self, mongo):
        client_id = await _client_row(mongo, "ayesha@example.com")
        await mongo["tenant"].users.update_one(
            {"_id": ObjectId(client_id)},
            {"$set": {"consultant_id": str(ObjectId())}})

        with pytest.raises(Forbidden):
            await service.resend_client_invite(
                mongo["tenant"], TENANT, _Consultant(Role.CONSULTANT), client_id)

    async def test_an_unknown_client_is_a_404(self, mongo):
        with pytest.raises(NotFound):
            await service.resend_client_invite(
                mongo["tenant"], TENANT, _Consultant(), str(ObjectId()))
