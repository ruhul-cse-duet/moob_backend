"""
Super Admin · Profile.

Backs the Profile screen and its Edit Profile dialog: name, email, title, phone
and the avatar.

The administrator record lives in the **platform** database, not in a tenant, so
the avatar goes to a GridFS bucket on the platform database and is served back
through this router. There is no object store and no static directory - the same
reasoning as case documents: the file lives with the record it belongs to.
"""
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.deps import CurrentUser, require_super_admin
from app.core.enums import AuditAction
from app.core.exceptions import Conflict, NotFound
from app.core.utils import oid, utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message
from app.services import audit, storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["Super Admin · Profile"])

DEFAULT_TITLE = "Platform Administrator"


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class AdminProfileOut(BaseModel):
    success: bool = True
    message: str = "OK"
    id: str
    email: EmailStr
    full_name: Optional[str] = None
    title: Optional[str] = None
    phone: Optional[str] = None
    admin_role: str = "super_admin"
    status: Optional[str] = None
    must_change_password: bool = False
    # Relative on purpose: the API is reached on different hosts in development,
    # on Render and behind a custom domain, and a stored absolute URL would be
    # wrong on two of the three.
    avatar_url: Optional[str] = None


class AdminProfileUpdate(BaseModel):
    """Every field optional - the dialog sends only what changed."""

    full_name: Optional[str] = Field(None, min_length=2, max_length=120)
    email: Optional[EmailStr] = None
    title: Optional[str] = Field(None, max_length=120)
    phone: Optional[str] = Field(None, max_length=32)

    @field_validator("title", "phone")
    @classmethod
    def strip_optional(cls, v: Optional[str]) -> Optional[str]:
        """Blank clears the field rather than storing empty text.

        A stored "" is not the same as absent: it renders as an empty line in
        the UI and defeats the `or DEFAULT_TITLE` fallback.
        """
        if v is None:
            return None
        return v.strip() or None

    @field_validator("full_name")
    @classmethod
    def require_a_real_name(cls, v: Optional[str]) -> Optional[str]:
        """Unlike title and phone, the name cannot be cleared.

        `min_length` alone does not cover this: "  " is two characters, so it
        passes the constraint and would then strip down to nothing and wipe the
        name off the account.
        """
        if v is None:
            return None
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Full name is required")
        return v


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _serialize(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": str(doc["_id"]),
        "email": doc["email"],
        "full_name": doc.get("full_name"),
        "title": doc.get("title") or DEFAULT_TITLE,
        "phone": doc.get("phone"),
        "admin_role": doc.get("admin_role", "super_admin"),
        "status": doc.get("status"),
        "must_change_password": bool(doc.get("must_change_password")),
        "avatar_url": "/api/v1/admin/profile/avatar" if doc.get("avatar") else None,
    }


async def _load(user: CurrentUser) -> Dict[str, Any]:
    doc = await platform_db().platform_admins.find_one({"_id": oid(user.id)})
    if not doc:
        raise NotFound("Administrator account not found")
    return doc


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
@router.get("", response_model=AdminProfileOut, summary="My administrator profile")
async def get_profile(user: CurrentUser = Depends(require_super_admin)):
    return _serialize(await _load(user))


@router.patch("", response_model=AdminProfileOut, summary="Update my administrator profile")
async def update_profile(payload: AdminProfileUpdate,
                         user: CurrentUser = Depends(require_super_admin)):
    """Save the Edit Profile dialog.

    Changing the email is allowed but guarded twice over: it is the sign-in
    identity, and sign-in checks ``platform_admins`` *before* the tenant
    directory - so an administrator who took an address already belonging to a
    consultant or client would silently lock that person out of their own
    workspace.
    """
    db = platform_db()
    doc = await _load(user)
    updates: Dict[str, Any] = {}

    # `model_fields_set` is what separates "the dialog did not send this field"
    # from "the user cleared it". Reading the attribute alone cannot tell the
    # two apart - both arrive as None - so an omitted field and a deleted phone
    # number would be treated identically and nothing could ever be cleared.
    sent = payload.model_fields_set
    for field in ("full_name", "title", "phone"):
        if field not in sent:
            continue
        value = getattr(payload, field)
        if value != doc.get(field):
            updates[field] = value

    email_changed_from: Optional[str] = None
    if payload.email:
        email = payload.email.strip().lower()
        if email != doc["email"]:
            if await db.platform_admins.find_one({"email": email, "_id": {"$ne": doc["_id"]}}):
                raise Conflict("Another administrator already uses this email")
            if await db.user_directory.find_one({"email": email}):
                raise Conflict(
                    "That email belongs to a workspace account. Using it here "
                    "would lock that person out of their own sign-in."
                )
            updates["email"] = email
            email_changed_from = doc["email"]

    if not updates:
        return _serialize(doc)

    updates["updated_at"] = utcnow()
    await db.platform_admins.update_one({"_id": doc["_id"]}, {"$set": updates})

    if email_changed_from:
        # The one change here that alters how someone signs in, so it belongs in
        # the audit trail rather than only in the document.
        await audit.record(
            action=AuditAction.ADMIN_ACTION, actor_id=str(doc["_id"]),
            actor_email=updates["email"], actor_role="super_admin",
            subject="Administrator email changed",
            detail=f"{email_changed_from} -> {updates['email']}",
        )

    return _serialize({**doc, **updates})


# --------------------------------------------------------------------------- #
# Avatar
# --------------------------------------------------------------------------- #
@router.post("/avatar", response_model=AdminProfileOut,
             status_code=status.HTTP_201_CREATED,
             summary="Upload or replace my profile picture")
async def upload_avatar(file: UploadFile = File(...),
                        user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    doc = await _load(user)
    previous = (doc.get("avatar") or {}).get("file_id")

    saved = await storage.save_avatar(
        db, file, owner_id=str(doc["_id"]), replaces=previous,
        metadata={"scope": "platform_admin"},
    )
    await db.platform_admins.update_one(
        {"_id": doc["_id"]}, {"$set": {"avatar": saved, "updated_at": utcnow()}}
    )
    return _serialize({**doc, "avatar": saved})


@router.get("/avatar", summary="My profile picture")
async def get_avatar(user: CurrentUser = Depends(require_super_admin)):
    doc = await _load(user)
    meta = doc.get("avatar")
    if not meta:
        raise NotFound("No profile picture has been uploaded")

    headers = {
        "Content-Disposition":
            f'inline; filename="{storage.safe_filename(meta.get("original_name"), "avatar")}"',
        # The bytes only change when the file id changes, and the id is in the
        # URL's own record - so a short cache saves re-reading GridFS for every
        # avatar render without ever serving a stale picture after a replacement.
        "Cache-Control": "private, max-age=300",
    }
    if isinstance(meta.get("size"), int):
        headers["Content-Length"] = str(meta["size"])

    return StreamingResponse(
        storage.stream_file(platform_db(), meta["file_id"],
                            meta.get("bucket", storage.AVATARS_BUCKET)),
        media_type=meta.get("mime_type") or "application/octet-stream",
        headers=headers,
    )


@router.delete("/avatar", response_model=Message,
               summary="Remove my profile picture")
async def delete_avatar(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    doc = await _load(user)
    meta = doc.get("avatar")
    if not meta:
        return {"detail": "No profile picture to remove"}

    await db.platform_admins.update_one(
        {"_id": doc["_id"]}, {"$unset": {"avatar": ""}, "$set": {"updated_at": utcnow()}}
    )
    # Record first, blob second: a failed delete leaves an orphaned blob, which
    # is harmless, where the reverse leaves a profile pointing at nothing.
    await storage.delete_file(db, meta["file_id"],
                              meta.get("bucket", storage.AVATARS_BUCKET))
    return {"detail": "Profile picture removed"}
