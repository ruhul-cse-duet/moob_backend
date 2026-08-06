"""
Partner invitation lifecycle and client-under-consultant signup.

Consultant creates partner -> email link -> preview -> set password -> active,
signed in, and sitting under the consultant who invited them.
"""
from datetime import timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import Role, UserStatus
from app.core.exceptions import BadRequest, NotFound
from app.core.utils import utcnow
from app.services import invites

TENANT_ID = "6600000000000000000000aa"


@pytest.fixture
def mongo(monkeypatch):
    client = AsyncMongoMockClient()
    pdb = client["webimove_platform"]
    tdb = client[f"webimove_tenant_{TENANT_ID}"]
    monkeypatch.setattr(invites, "platform_db", lambda: pdb)
    monkeypatch.setattr(invites, "tenant_db", lambda tid: tdb)
    return {"platform": pdb, "tenant": tdb}


@pytest.fixture
async def seeded(mongo):
    from bson import ObjectId
    pdb, tdb = mongo["platform"], mongo["tenant"]
    await pdb.tenants.insert_one({"_id": ObjectId(TENANT_ID),
                                  "name": "Jenkins Immigration Law",
                                  "status": "active"})
    consultant = await tdb.users.insert_one({
        "full_name": "Sarah Jenkins", "title": "Senior Consultant",
        "email": "sarah@jenkinslaw.com", "role": Role.CONSULTANT_OWNER.value})
    partner = await tdb.users.insert_one({
        "full_name": "Nadia Volkova", "email": "nadia@translations.com",
        "role": Role.PARTNER.value, "partner_role": "Certified Translator",
        "status": UserStatus.INVITED.value, "password_hash": None,
        "consultant_id": str(consultant.inserted_id),
        "consultant_ids": [str(consultant.inserted_id)]})
    return {**mongo, "consultant_id": str(consultant.inserted_id),
            "partner_id": str(partner.inserted_id)}


async def test_invite_preview_shows_who_invited_and_what_for(seeded):
    token = await invites.issue(email="nadia@translations.com", tenant_id=TENANT_ID,
                                user_id=seeded["partner_id"], role=Role.PARTNER,
                                invited_by=seeded["consultant_id"])
    preview = (await invites.lookup(token))["preview"]

    assert preview["email"] == "nadia@translations.com"
    assert preview["role"] == Role.PARTNER.value
    assert preview["partner_role"] == "Certified Translator"
    assert preview["organization"]["name"] == "Jenkins Immigration Law"
    assert preview["invited_by"]["full_name"] == "Sarah Jenkins"
    assert preview["expires_at"] is not None


async def test_link_points_at_the_frontend_not_the_api():
    from app.core.config import settings
    link = invites.build_link("abc123")
    assert link.startswith(settings.FRONTEND_URL.rstrip("/"))
    assert "/api/" not in link
    assert link.endswith("token=abc123")


async def test_token_is_single_use(seeded):
    token = await invites.issue(email="nadia@translations.com", tenant_id=TENANT_ID,
                                user_id=seeded["partner_id"], role=Role.PARTNER,
                                invited_by=seeded["consultant_id"])
    await invites.consume(token)
    with pytest.raises(NotFound):
        await invites.lookup(token)


async def test_expired_token_is_rejected(seeded):
    token = await invites.issue(email="nadia@translations.com", tenant_id=TENANT_ID,
                                user_id=seeded["partner_id"], role=Role.PARTNER,
                                invited_by=seeded["consultant_id"])
    await seeded["platform"].user_directory.update_one(
        {"invite_token": token},
        {"$set": {"invite_expires_at": utcnow() - timedelta(days=1)}})
    with pytest.raises(BadRequest, match="expired"):
        await invites.lookup(token)


async def test_resend_invalidates_the_previous_link(seeded):
    first = await invites.issue(email="nadia@translations.com", tenant_id=TENANT_ID,
                                user_id=seeded["partner_id"], role=Role.PARTNER,
                                invited_by=seeded["consultant_id"])
    second = await invites.refresh("nadia@translations.com")

    assert first != second
    await invites.lookup(second)                 # new link works
    with pytest.raises(NotFound):                # old link does not
        await invites.lookup(first)


async def test_already_accepted_invite_is_rejected(seeded):
    from bson import ObjectId
    token = await invites.issue(email="nadia@translations.com", tenant_id=TENANT_ID,
                                user_id=seeded["partner_id"], role=Role.PARTNER,
                                invited_by=seeded["consultant_id"])
    await seeded["tenant"].users.update_one(
        {"_id": ObjectId(seeded["partner_id"])},
        {"$set": {"status": UserStatus.ACTIVE.value, "password_hash": "already-set"}})
    with pytest.raises(BadRequest, match="already accepted"):
        await invites.lookup(token)


async def test_invited_partner_belongs_to_the_inviting_consultant(seeded):
    """The whole point: after accepting, they are under that consultant."""
    from bson import ObjectId
    partner = await seeded["tenant"].users.find_one(
        {"_id": ObjectId(seeded["partner_id"])})
    assert partner["consultant_id"] == seeded["consultant_id"]
    assert seeded["consultant_id"] in partner["consultant_ids"]


async def test_revoke_removes_the_directory_entry(seeded):
    await invites.issue(email="nadia@translations.com", tenant_id=TENANT_ID,
                        user_id=seeded["partner_id"], role=Role.PARTNER,
                        invited_by=seeded["consultant_id"])
    await invites.revoke("nadia@translations.com")
    assert await seeded["platform"].user_directory.count_documents({}) == 0
