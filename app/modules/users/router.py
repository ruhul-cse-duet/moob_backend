from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from pydantic import BaseModel

from app.core.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_NAMES,
    Language,
    translate,
)
from app.core.i18n import normalize as normalize_language
from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
    require_owner,
)
from app.core.enums import Role
from app.core.exceptions import NotFound
from app.core.utils import oid, utcnow
from app.modules.users import schemas as s
from app.modules.users import service
from app.schemas.common import Message, PageParams
from app.services import storage

router = APIRouter(tags=["Users, Team & Clients"])


@router.get("/client/profile-overview", response_model=s.ClientProfileOverview,
            summary="Client User Profile Menu Overview")
async def client_profile_overview(user: CurrentUser = Depends(get_current_user),
                                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    profile = await service.me(db, user)
    return {
        "user": profile,
        "menu_workspace": [
            {"id": "new_request", "title": "New Immigration Request", "icon": "plus_square", "route": "/requests/new"},
            {"id": "messages", "title": "Messages", "icon": "chat_bubble", "route": "/messages"},
            {"id": "gdpr_consent", "title": "GDPR Consent", "icon": "shield_check", "route": "/gdpr-consent"},
        ],
        "menu_account": [
            {"id": "notifications", "title": "Notifications", "icon": "bell", "route": "/notifications"},
            {"id": "settings", "title": "Settings", "icon": "cog", "route": "/settings"},
        ],
        "menu_support": [
            {"id": "help_support", "title": "Help & Support", "icon": "question_mark", "route": "/support"},
            {"id": "privacy_policy", "title": "Privacy Policy", "icon": "scale", "route": "/privacy-policy"},
            {"id": "terms_conditions", "title": "Terms & Conditions", "icon": "info_circle", "route": "/terms"},
        ],
    }


@router.get("/consultant/profile-overview", response_model=s.ConsultantProfileOverview,
            summary="Consultant User Profile Menu Overview")
async def consultant_profile_overview(user: CurrentUser = Depends(require_consultant),
                                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    profile = await service.me(db, user)
    return {
        "user": profile,
        "menu_workspace": [
            {"id": "clients", "title": "Clients", "icon": "users_group", "route": "/clients"},
            {"id": "cases", "title": "Cases", "icon": "briefcase", "route": "/cases"},
            {"id": "documents", "title": "Documents", "icon": "document_text", "route": "/documents"},
            {"id": "partner_tasks", "title": "Partner Tasks", "icon": "handshake", "route": "/partner-tasks"},
            {"id": "partner_management", "title": "Partner Management", "icon": "user_plus", "route": "/partners"},
            {"id": "messages", "title": "Messages", "icon": "chat_bubble", "route": "/messages"},
            {"id": "reporting", "title": "Reporting", "icon": "chart_bar", "route": "/reporting"},
        ],
        "menu_account": [
            {"id": "notifications", "title": "Notifications", "icon": "bell", "route": "/notifications"},
            {"id": "settings", "title": "Settings", "icon": "cog", "route": "/settings"},
        ],
        "menu_support": [
            {"id": "help_support", "title": "Help & Support", "icon": "question_mark", "route": "/support"},
            {"id": "privacy_policy", "title": "Privacy Policy", "icon": "scale", "route": "/privacy-policy"},
            {"id": "terms_conditions", "title": "Terms & Conditions", "icon": "info_circle", "route": "/terms"},
        ],
    }


# --------------------------------------------------------------------------- #
# Workspace language — the Settings screen picker
# --------------------------------------------------------------------------- #
class LanguageChoice(BaseModel):
    language: Language


@router.get("/me/language", summary="Workspace language, and the options")
async def get_language(user: CurrentUser = Depends(get_current_user)):
    """What the picker renders.

    Each option is named in its own language: someone looking for Portuguese
    should not have to read English to find it.
    """
    return {
        "language": normalize_language(user.raw.get("language")) or DEFAULT_LANGUAGE,
        "options": [{"code": lang.value, "name": LANGUAGE_NAMES[lang]}
                    for lang in Language],
    }


@router.put("/me/language", summary="Save the workspace language")
async def set_language(payload: LanguageChoice,
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Saved on the account, not just held in the app.

    That is the point of storing it server-side: an email or a push notification
    is written when the app is not running and there is no header to read, so
    without this they would always go out in English.
    """
    await db.users.update_one(
        {"_id": oid(user.id)},
        {"$set": {"language": payload.language.value, "updated_at": utcnow()}},
    )
    return {"success": True, "language": payload.language.value,
            "message": translate("language.saved", payload.language.value)}


@router.get("/client/settings", response_model=s.NotificationSettings, summary="Get client settings toggle states")
async def get_client_settings(user: CurrentUser = Depends(get_current_user),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    raw = user.raw.get("settings", {})
    return {
        "push_notifications": raw.get("push_notifications", True),
        "case_updates": raw.get("case_updates", True),
    }


@router.patch("/client/settings", response_model=s.NotificationSettings, summary="Update client settings toggle states")
async def update_client_settings(payload: s.NotificationSettings,
                                 user: CurrentUser = Depends(get_current_user),
                                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await db.users.update_one(
        {"_id": oid(user.id)},
        {"$set": {"settings": payload.model_dump(), "updated_at": utcnow()}}
    )
    return payload


@router.get("/client/gdpr-consent", response_model=s.GdprConsentView,
            summary="Get GDPR Data Processing Consent Details")
async def get_client_gdpr_consent(user: CurrentUser = Depends(get_current_user)):
    return {
        "title": "Data Processing Consent",
        "description": "To process your immigration case, WebImove needs your consent to collect, store, and process your personal documents in accordance with GDPR. Your data is encrypted and only accessible to your assigned consultant.",
        "items": [
            "Collection and storage of identity documents",
            "Sharing data with relevant government authorities",
            "Processing sensitive data for your immigration case",
        ],
        "status": "provided",
        "badge_status": "active",
        "is_active": True,
        "granted_at": user.raw.get("created_at") or utcnow(),
    }


@router.get("/client/privacy-policy", response_model=s.LegalDocumentView, summary="Get Privacy Policy")
async def get_client_privacy_policy():
    return {
        "title": "Privacy Policy",
        "last_updated": "Last updated: April 2026",
        "sections": [
            "We respect your privacy. WebImove collects only the data needed to help process your immigration case — your location, account details, and submitted documents.",
            "We never sell your personal information to third parties. You can request a copy of your data or delete your account at any time from Privacy & Security settings.",
            "Your data is stored securely in encrypted databases isolated per agency.",
        ],
        "contact_email": "privacy@webimove.com",
    }


@router.get("/client/terms-of-service", response_model=s.LegalDocumentView, summary="Get Terms of Service")
async def get_client_terms_of_service():
    return {
        "title": "Terms of Service",
        "last_updated": "Effective April 2026",
        "sections": [
            "By using WebImove you agree to submit documents and case information in good faith. Services shown in the app are provided by licensed consultants and subject to availability.",
            "You are responsible for the accuracy of any information you submit and for keeping your account credentials secure. We may update these terms periodically and will notify you of material changes.",
            "We never sell your personal information to third parties. You can request a copy of your data or delete your account at any time.",
            "Misuse of system services or fraudulent activity may result in account suspension.",
        ],
        "contact_email": "legal@webimove.com",
    }


@router.get("/me", response_model=s.UserOut, summary="My profile")
async def me(user: CurrentUser = Depends(get_current_user),
             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.me(db, user)


@router.patch("/me", response_model=s.UserOut)
async def update_me(payload: s.ProfileUpdate,
                    user: CurrentUser = Depends(get_current_user),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.update_profile(db, user, payload)


@router.post("/me/avatar", response_model=s.UserOut, status_code=status.HTTP_201_CREATED,
             summary="Upload or replace my profile picture")
async def upload_my_avatar(file: UploadFile = File(...),
                           user: CurrentUser = Depends(get_current_user),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Same GridFS pattern as the super-admin's own avatar (admin/profile.py),
    just scoped to the tenant `users` collection instead of the platform one —
    consultants, partners and clients had the `avatar_url` field on their
    profile already, but nothing that could ever set it to a real picture."""
    doc = await db.users.find_one({"_id": oid(user.id)})
    if not doc:
        raise NotFound("Account not found")
    previous = (doc.get("avatar") or {}).get("file_id")

    saved = await storage.save_avatar(
        db, file, owner_id=user.id, replaces=previous,
        metadata={"scope": "tenant_user"},
    )
    updates = {"avatar": saved, "avatar_url": "/api/v1/me/avatar", "updated_at": utcnow()}
    await db.users.update_one({"_id": doc["_id"]}, {"$set": updates})

    refreshed = CurrentUser(id=user.id, email=user.email, role=user.role,
                            tenant_id=user.tenant_id, raw={**doc, **updates})
    return await service.me(db, refreshed)


@router.get("/me/avatar", summary="My profile picture")
async def get_my_avatar(user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.users.find_one({"_id": oid(user.id)})
    meta = (doc or {}).get("avatar")
    if not meta:
        raise NotFound("No profile picture has been uploaded")

    headers = {
        "Content-Disposition":
            f'inline; filename="{storage.safe_filename(meta.get("original_name"), "avatar")}"',
        "Cache-Control": "private, max-age=300",
    }
    if isinstance(meta.get("size"), int):
        headers["Content-Length"] = str(meta["size"])

    return StreamingResponse(
        storage.stream_file(db, meta["file_id"], meta.get("bucket", storage.AVATARS_BUCKET)),
        media_type=meta.get("mime_type") or "application/octet-stream",
        headers=headers,
    )


@router.delete("/me/avatar", response_model=Message, summary="Remove my profile picture")
async def delete_my_avatar(user: CurrentUser = Depends(get_current_user),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.users.find_one({"_id": oid(user.id)})
    meta = (doc or {}).get("avatar")
    if not meta:
        return {"detail": "No profile picture to remove"}

    await db.users.update_one(
        {"_id": doc["_id"]},
        {"$unset": {"avatar": "", "avatar_url": ""}, "$set": {"updated_at": utcnow()}},
    )
    await storage.delete_file(db, meta["file_id"], meta.get("bucket", storage.AVATARS_BUCKET))
    return {"detail": "Profile picture removed"}


@router.get("/users", summary="Directory (consultants, partners, clients)")
async def list_users(role: Optional[Role] = Query(None), search: Optional[str] = Query(None),
                     consultant_id: Optional[str] = Query(
                         None, description="Clients owned by, or partners working with, "
                                           "this consultant"),
                     params: PageParams = Depends(page_params),
                     user: CurrentUser = Depends(require_consultant),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_users(db, user, params, role, search, consultant_id)


@router.get("/users/{user_id}", response_model=s.UserOut)
async def get_user(user_id: str,
                   user: CurrentUser = Depends(require_consultant),
                   db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_user(db, user_id)


@router.post("/team", response_model=s.UserOut, status_code=status.HTTP_201_CREATED,
             summary="Team & Seats: invite another consultant")
async def invite_team_member(payload: s.TeamInvite,
                             user: CurrentUser = Depends(require_owner),
                             tenant: dict = Depends(require_active_tenant),
                             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.invite_team_member(db, user, tenant, payload)


@router.post("/clients", response_model=s.UserOut, status_code=status.HTTP_201_CREATED,
             summary="Consultant adds a client to the workspace")
async def create_client(payload: s.ClientCreate,
                        user: CurrentUser = Depends(require_consultant),
                        tenant: dict = Depends(require_active_tenant),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """The creating consultant owns this client."""
    return await service.create_client(db, user, tenant, payload)


@router.post("/clients/{client_id}/resend",
             summary="Send a client's invitation again")
async def resend_client_invite(client_id: str,
                               user: CurrentUser = Depends(require_consultant),
                               tenant: dict = Depends(require_active_tenant),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """For a client who never opened their link, or whose link expired. The old
    one stops working immediately."""
    return await service.resend_client_invite(db, tenant, user, client_id)


@router.get("/clients/{client_id}", response_model=s.ClientDetailView,
            summary="Client profile - the consultant's full view, or the part a "
                    "delegated partner needs")
async def get_client_profile_detail(client_id: str,
                                    user: CurrentUser = Depends(get_current_user),
                                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Open to a partner who has been given work on this client.

    They cannot check a document against the person it belongs to without
    knowing who that is and what was applied for. The service narrows what they
    see to the cases they were actually delegated.
    """
    return await service.get_client_profile_detail(db, user, client_id)


@router.post("/clients/{client_id}/partner", response_model=s.ClientDetailView,
             summary="Bulk-assign this client to a partner — the partner then "
                     "processes all of the client's cases like a consultant")
async def assign_partner(client_id: str, payload: s.AssignPartnerPayload,
                         user: CurrentUser = Depends(require_consultant),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.assign_partner(db, user, client_id, payload.partner_id)


@router.delete("/clients/{client_id}/partner", response_model=s.ClientDetailView,
               summary="Unassign the partner from this client — hands the client back "
                       "to the consultant only")
async def unassign_partner(client_id: str,
                           user: CurrentUser = Depends(require_consultant),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.unassign_partner(db, user, client_id)