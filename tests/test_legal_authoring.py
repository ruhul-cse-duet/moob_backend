"""
Super Admin · authoring the Privacy Policy and Terms of Service.

One rule holds this together: **a published version is immutable**. Acceptance
is stored as a version *string*, and `/legal/acceptances` decides `up_to_date`
by comparing that string against the live version. Rewrite the body of a version
people already accepted and their records now claim they agreed to text they
never saw - while the platform still reports everyone as up to date. So updating
a live policy means duplicating it, editing the draft, and publishing that.
"""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import PolicyKind
from app.core.exceptions import Conflict, NotFound
from app.modules.legal import router as legal
from app.services import audit

PRIVACY = PolicyKind.PRIVACY_POLICY


class Admin:
    id = str(ObjectId())
    email = "superadmin@webimove.com"
    tenant_id = None


@pytest.fixture
def db(monkeypatch):
    client = AsyncMongoMockClient()
    pdb = client["webimove_platform"]
    monkeypatch.setattr(legal, "platform_db", lambda: pdb)
    monkeypatch.setattr(audit, "platform_db", lambda: pdb)
    return pdb


async def create(version="1.0", publish=True, kind=PRIVACY, body="Original text"):
    return await legal.create_policy(
        legal.PolicyCreate(kind=kind, version=version, title="Privacy Policy",
                           body_markdown=body, publish=publish),
        Admin(),
    )


# ------------------------------------------------------------------ creating
@pytest.mark.asyncio
async def test_creating_and_publishing_a_first_version(db):
    result = await create()
    assert result["published"] is True
    assert (await legal.policy(PRIVACY))["version"] == "1.0"


@pytest.mark.asyncio
async def test_a_duplicate_version_number_is_refused(db):
    """Acceptance is keyed on the version string, so two rows sharing one
    version make "accepted Privacy Policy 2.0" ambiguous."""
    await create(version="1.0")
    with pytest.raises(Conflict):
        await create(version="1.0", publish=False)


@pytest.mark.asyncio
async def test_publishing_never_leaves_the_policy_unpublished(db):
    """The old order cleared every published flag before writing the new row,
    so for that instant the public endpoint had nothing to serve - and signup
    has to show the terms before anyone has an account."""
    await create(version="1.0")
    await create(version="2.0")

    live = await legal.policy(PRIVACY)
    assert live["version"] == "2.0"
    assert await db.policies.count_documents({"kind": PRIVACY.value,
                                              "published": True}) == 1


# ------------------------------------------------------------------- editing
@pytest.mark.asyncio
async def test_a_draft_can_be_edited(db):
    draft = await create(version="2.0", publish=False)
    updated = await legal.update_policy(
        draft["id"], legal.PolicyUpdate(body_markdown="Revised text"), Admin()
    )
    assert updated["body_markdown"] == "Revised text"


@pytest.mark.asyncio
async def test_a_published_version_cannot_be_edited(db):
    """The rule the whole module rests on."""
    live = await create(version="1.0")
    with pytest.raises(Conflict):
        await legal.update_policy(
            live["id"], legal.PolicyUpdate(body_markdown="Quietly rewritten"), Admin()
        )
    assert (await legal.policy(PRIVACY))["body_markdown"] == "Original text"


@pytest.mark.asyncio
async def test_editing_only_touches_the_fields_that_were_sent(db):
    draft = await create(version="2.0", publish=False)
    updated = await legal.update_policy(
        draft["id"], legal.PolicyUpdate(title="Privacy Notice"), Admin()
    )
    assert updated["title"] == "Privacy Notice"
    assert updated["body_markdown"] == "Original text"


@pytest.mark.asyncio
async def test_renaming_a_draft_onto_an_existing_version_is_refused(db):
    await create(version="1.0")
    draft = await create(version="2.0", publish=False)
    with pytest.raises(Conflict):
        await legal.update_policy(draft["id"], legal.PolicyUpdate(version="1.0"), Admin())


# --------------------------------------------------------------- duplicating
@pytest.mark.asyncio
async def test_duplicating_copies_the_text_into_an_unpublished_draft(db):
    live = await create(version="1.0", body="The text people accepted")
    draft = await legal.duplicate_policy(live["id"], version="2.0", user=Admin())

    assert draft["published"] is False
    assert draft["body_markdown"] == "The text people accepted"
    assert draft["duplicated_from"] == live["id"]
    # The live policy is untouched while the draft is being written.
    assert (await legal.policy(PRIVACY))["version"] == "1.0"


@pytest.mark.asyncio
async def test_the_full_update_route_for_a_live_policy(db):
    """Duplicate, edit, publish - and the old version survives, because that is
    what existing acceptance records point at."""
    live = await create(version="1.0", body="Version one text")
    draft = await legal.duplicate_policy(live["id"], version="2.0", user=Admin())
    await legal.update_policy(
        draft["id"], legal.PolicyUpdate(body_markdown="Version two text"), Admin()
    )
    await legal.publish_policy(draft["id"], Admin())

    assert (await legal.policy(PRIVACY))["body_markdown"] == "Version two text"
    old = await db.policies.find_one({"_id": ObjectId(live["id"])})
    assert old["body_markdown"] == "Version one text"
    assert old["published"] is False


@pytest.mark.asyncio
async def test_duplicating_onto_an_existing_version_is_refused(db):
    live = await create(version="1.0")
    with pytest.raises(Conflict):
        await legal.duplicate_policy(live["id"], version="1.0", user=Admin())


# ------------------------------------------------------------------ deleting
@pytest.mark.asyncio
async def test_a_draft_can_be_discarded(db):
    draft = await create(version="2.0", publish=False)
    await legal.delete_policy(draft["id"], Admin())
    assert await db.policies.count_documents({"_id": ObjectId(draft["id"])}) == 0


@pytest.mark.asyncio
async def test_the_live_policy_cannot_be_deleted(db):
    """Deleting it would leave signup with no terms to display."""
    live = await create(version="1.0")
    with pytest.raises(Conflict):
        await legal.delete_policy(live["id"], Admin())


@pytest.mark.asyncio
async def test_a_missing_version_is_a_404_not_a_crash(db):
    missing = str(ObjectId())
    for call in (
        legal.get_policy_version(missing, Admin()),
        legal.update_policy(missing, legal.PolicyUpdate(title="x"), Admin()),
        legal.delete_policy(missing, Admin()),
        legal.publish_policy(missing, Admin()),
    ):
        with pytest.raises(NotFound):
            await call


# --------------------------------------------------------------------- audit
@pytest.mark.asyncio
async def test_policy_changes_are_written_to_the_audit_trail(db):
    """Changing the Terms of Service is legally significant; it should not be
    possible to do it without a trace of who and when."""
    live = await create(version="1.0")
    draft = await legal.duplicate_policy(live["id"], version="2.0", user=Admin())
    await legal.publish_policy(draft["id"], Admin())

    entries = [e async for e in db.audit_log.find({})]
    assert len(entries) >= 3
    assert all(e["actor_email"] == "superadmin@webimove.com" for e in entries)


# ----------------------------------------------------------- terms of service
@pytest.mark.asyncio
async def test_the_kinds_are_independent(db):
    """Publishing Terms must not unpublish the Privacy Policy."""
    await create(version="1.0", kind=PolicyKind.PRIVACY_POLICY)
    await create(version="1.0", kind=PolicyKind.TERMS_OF_SERVICE)

    assert (await legal.policy(PolicyKind.PRIVACY_POLICY))["version"] == "1.0"
    assert (await legal.policy(PolicyKind.TERMS_OF_SERVICE))["version"] == "1.0"
