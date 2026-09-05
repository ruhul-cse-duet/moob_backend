"""Registering is not the same as asking for something.

Finishing signup used to insert a request on the client's behalf - reference,
`status: new`, and a purpose sentence the server wrote itself ("... enquiry
raised during registration"). It landed in the consultant's queue as real work
nobody had actually submitted, and the client had no idea it existed.

Now registration creates the account and stops. `POST /requests` is the only way
a request comes into being. The consultant is still told a new client joined,
because that is worth knowing - it just is not a request.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role
from app.modules.auth import service

SIGNUP_ID = ObjectId()
TENANT_ID = ObjectId()
CONSULTANT_ID = ObjectId()


class _Agreements:
    def model_dump(self):
        return {"terms": True, "privacy": True}


@pytest.fixture
async def signed_up(monkeypatch):
    client = AsyncMongoMockClient()
    pdb = client["webimove_platform"]
    tdb = client[f"webimove_tenant_{TENANT_ID}"]

    await pdb.tenants.insert_one({"_id": TENANT_ID, "name": "Jenkins Immigration Law",
                                  "status": "active"})
    await tdb.users.insert_one({"_id": CONSULTANT_ID, "full_name": "Sarah Jenkins",
                                "role": Role.CONSULTANT_OWNER.value})
    await pdb.signups.insert_one({
        "_id": SIGNUP_ID, "email": "ayesha@example.com", "full_name": "Ayesha Rahman",
        "mobile": "+351900000000", "password_hash": "x",
        "tenant_id": str(TENANT_ID), "consultant_id": str(CONSULTANT_ID),
        "immigration_profile": {"preferred_immigration_type": "Student Visa",
                                "destination_country": "Canada",
                                "nationality": "Bangladeshi"},
    })

    notified = []

    async def _notify(db, **kwargs):
        notified.append(kwargs)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(service, "platform_db", lambda: pdb)
    monkeypatch.setattr(service, "tenant_db", lambda tid: tdb)
    monkeypatch.setattr(service, "_load_signup",
                        lambda token: pdb.signups.find_one({"_id": SIGNUP_ID}))
    monkeypatch.setattr(service, "notify", _notify)
    monkeypatch.setattr(service.consent_service, "record_signup_agreements", _noop)
    monkeypatch.setattr(service, "_token_response",
                        lambda *a, **k: _returns({"access_token": "a",
                                                  "refresh_token": "r"}))
    return {"platform": pdb, "tenant": tdb, "notified": notified}


async def _returns(value):
    return value


async def test_no_request_is_invented_for_the_client(signed_up):
    await service.finalize_client_signup("token", _Agreements())

    assert await signed_up["tenant"].requests.count_documents({}) == 0


async def test_the_account_is_still_created_and_bound_to_its_consultant(signed_up):
    result = await service.finalize_client_signup("token", _Agreements())

    user = await signed_up["tenant"].users.find_one({"email": "ayesha@example.com"})
    assert user["role"] == Role.CLIENT.value
    assert user["consultant_id"] == str(CONSULTANT_ID)
    # The details from step 4 travel onto the account, which is where the
    # consultant reads them now that there is no request carrying them.
    assert user["nationality"] == "Bangladeshi"
    assert user["preferred_immigration_type"] == "Student Visa"
    # Nothing to summarise, and the screen is told so rather than shown a
    # reference for a request that does not exist.
    assert result["request_summary"]["request_id"] is None
    assert result["request_summary"]["status"] == "account_ready"


async def test_the_consultant_hears_about_the_client_not_about_a_request(signed_up):
    await service.finalize_client_signup("token", _Agreements())

    assert len(signed_up["notified"]) == 1
    said = signed_up["notified"][0]
    assert said["user_ids"] == [str(CONSULTANT_ID)]
    assert said["title_key"] == "notify.client_registered"
    assert said["type"].value == "client_joined"
    # A request id here would send the app to a screen for something that was
    # never created.
    assert "request_id" not in said["data"]
