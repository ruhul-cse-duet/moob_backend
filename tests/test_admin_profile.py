"""
Super Admin · Profile, including the avatar.

The Edit Profile dialog had no backing API at all: nothing could write
`full_name`, `title` or `phone`, `title` was a hard-coded string in the
response, and no endpoint anywhere in the project accepted a profile picture.
These tests cover the surface that replaced it, and in particular the two ways
an email change can quietly break someone's sign-in.
"""
import io

import pytest
from bson import ObjectId
from fastapi import UploadFile
from mongomock_motor import AsyncMongoMockClient, enabled_gridfs_integration
from pydantic import ValidationError

from app.core.exceptions import BadRequest, Conflict
from app.modules.admin import profile as admin_profile
from app.services import storage

ADMIN_ID = ObjectId()
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 200


class Admin:
    """Stands in for the CurrentUser dependency, which the routes only read."""

    id = str(ADMIN_ID)
    email = "superadmin@webimove.com"


def upload(name: str, content_type: str, data: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data),
                      headers={"content-type": content_type})


@pytest.fixture
def db(monkeypatch):
    with enabled_gridfs_integration():
        client = AsyncMongoMockClient()
        pdb = client["webimove_platform"]
        monkeypatch.setattr(admin_profile, "platform_db", lambda: pdb)
        yield pdb


@pytest.fixture
async def seeded(db):
    await db.platform_admins.insert_one({
        "_id": ADMIN_ID,
        "email": "superadmin@webimove.com",
        "full_name": "Ayesha Rahman",
        "status": "active",
    })
    return db


# ------------------------------------------------------------------ profile
@pytest.mark.asyncio
async def test_title_falls_back_but_is_no_longer_hard_coded(seeded):
    """An admin with no title reads as Platform Administrator; one who sets a
    title keeps it. The old endpoint returned the constant either way."""
    assert (await admin_profile.get_profile(Admin()))["title"] == "Platform Administrator"

    await admin_profile.update_profile(
        admin_profile.AdminProfileUpdate(title="Head of Compliance"), Admin()
    )
    assert (await admin_profile.get_profile(Admin()))["title"] == "Head of Compliance"


@pytest.mark.asyncio
async def test_every_dialog_field_persists(seeded):
    result = await admin_profile.update_profile(
        admin_profile.AdminProfileUpdate(
            full_name="Ayesha R. Rahman",
            title="Platform Administrator",
            phone="+880 1711 555 042",
        ),
        Admin(),
    )
    assert result["full_name"] == "Ayesha R. Rahman"
    assert result["phone"] == "+880 1711 555 042"

    stored = await seeded.platform_admins.find_one({"_id": ADMIN_ID})
    assert stored["phone"] == "+880 1711 555 042"


@pytest.mark.asyncio
async def test_a_partial_update_leaves_the_other_fields_alone(seeded):
    """The dialog sends only what changed."""
    await admin_profile.update_profile(
        admin_profile.AdminProfileUpdate(phone="+880 1711 555 042"), Admin()
    )
    result = await admin_profile.update_profile(
        admin_profile.AdminProfileUpdate(title="Compliance Lead"), Admin()
    )
    assert result["phone"] == "+880 1711 555 042"
    assert result["full_name"] == "Ayesha Rahman"


@pytest.mark.asyncio
async def test_blanking_an_optional_field_clears_it(seeded):
    """Sending an empty phone must delete the number, not store "".

    This needs `model_fields_set`: an omitted field and a cleared one both
    arrive as None, so reading the attribute alone cannot tell them apart and
    nothing could ever be cleared.
    """
    await admin_profile.update_profile(
        admin_profile.AdminProfileUpdate(phone="+880 1711 555 042"), Admin()
    )
    await admin_profile.update_profile(
        admin_profile.AdminProfileUpdate(phone="   "), Admin()
    )
    stored = await seeded.platform_admins.find_one({"_id": ADMIN_ID})
    assert stored["phone"] is None


@pytest.mark.asyncio
async def test_the_name_cannot_be_blanked(seeded):
    """"  " is two characters, so min_length lets it through; without the
    validator it would strip to nothing and wipe the name off the account."""
    with pytest.raises(ValidationError):
        admin_profile.AdminProfileUpdate(full_name="  ")

    stored = await seeded.platform_admins.find_one({"_id": ADMIN_ID})
    assert stored["full_name"] == "Ayesha Rahman"


# -------------------------------------------------------------------- email
@pytest.mark.asyncio
async def test_email_can_be_changed(seeded):
    result = await admin_profile.update_profile(
        admin_profile.AdminProfileUpdate(email="ayesha@webimove.com"), Admin()
    )
    assert result["email"] == "ayesha@webimove.com"


@pytest.mark.asyncio
async def test_email_already_used_by_another_administrator_is_refused(seeded):
    await seeded.platform_admins.insert_one(
        {"_id": ObjectId(), "email": "taken@webimove.com", "status": "active"}
    )
    with pytest.raises(Conflict):
        await admin_profile.update_profile(
            admin_profile.AdminProfileUpdate(email="taken@webimove.com"), Admin()
        )


@pytest.mark.asyncio
async def test_email_belonging_to_a_workspace_account_is_refused(seeded):
    """The important one.

    Sign-in checks platform_admins before the tenant directory, so an
    administrator who took a consultant's address would lock that consultant out
    of their own workspace - and nothing would report an error.
    """
    await seeded.user_directory.insert_one(
        {"email": "sarah@jenkinslaw.com", "tenant_id": "t1", "role": "consultant_owner"}
    )
    with pytest.raises(Conflict):
        await admin_profile.update_profile(
            admin_profile.AdminProfileUpdate(email="sarah@jenkinslaw.com"), Admin()
        )
    stored = await seeded.platform_admins.find_one({"_id": ADMIN_ID})
    assert stored["email"] == "superadmin@webimove.com"


# ------------------------------------------------------------------- avatar
@pytest.mark.asyncio
async def test_no_avatar_means_no_url(seeded):
    assert (await admin_profile.get_profile(Admin()))["avatar_url"] is None


@pytest.mark.asyncio
async def test_uploading_an_avatar_exposes_a_url(seeded):
    result = await admin_profile.upload_avatar(upload("me.png", "image/png", PNG), Admin())
    assert result["avatar_url"] == "/api/v1/admin/profile/avatar"

    stored = await seeded.platform_admins.find_one({"_id": ADMIN_ID})
    assert stored["avatar"]["size"] == len(PNG)
    assert stored["avatar"]["bucket"] == storage.AVATARS_BUCKET


@pytest.mark.asyncio
async def test_replacing_an_avatar_deletes_the_previous_blob(seeded):
    """Otherwise every avatar change leaves a dead blob in the bucket forever."""
    await admin_profile.upload_avatar(upload("one.png", "image/png", PNG), Admin())
    first = (await seeded.platform_admins.find_one({"_id": ADMIN_ID}))["avatar"]["file_id"]

    await admin_profile.upload_avatar(upload("two.png", "image/png", PNG + b"x"), Admin())
    second = (await seeded.platform_admins.find_one({"_id": ADMIN_ID}))["avatar"]["file_id"]

    assert first != second
    assert await seeded["avatars.files"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_documents_are_not_valid_profile_pictures(seeded):
    """A PDF is an accepted case document and a nonsense avatar - every caller
    renders this straight into an <img>."""
    with pytest.raises(BadRequest):
        await admin_profile.upload_avatar(
            upload("passport.pdf", "application/pdf", b"%PDF-1.4 scan"), Admin()
        )


@pytest.mark.asyncio
async def test_an_oversized_image_is_refused(seeded):
    too_big = b"x" * ((storage.MAX_AVATAR_MB * 1024 * 1024) + 1)
    with pytest.raises(BadRequest):
        await admin_profile.upload_avatar(upload("huge.png", "image/png", too_big), Admin())


@pytest.mark.asyncio
async def test_an_empty_image_is_refused(seeded):
    with pytest.raises(BadRequest):
        await admin_profile.upload_avatar(upload("empty.png", "image/png", b""), Admin())


@pytest.mark.asyncio
async def test_deleting_an_avatar_removes_the_record_and_the_blob(seeded):
    await admin_profile.upload_avatar(upload("me.png", "image/png", PNG), Admin())
    await admin_profile.delete_avatar(Admin())

    stored = await seeded.platform_admins.find_one({"_id": ADMIN_ID})
    assert "avatar" not in stored
    assert await seeded["avatars.files"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_deleting_when_there_is_nothing_to_delete_is_not_an_error(seeded):
    assert "detail" in await admin_profile.delete_avatar(Admin())
