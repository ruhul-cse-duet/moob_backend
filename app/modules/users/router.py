from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

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
from app.core.utils import oid, utcnow
from app.modules.users import schemas as s
from app.modules.users import service
from app.schemas.common import PageParams

router = APIRouter(tags=["Users, Team & Clients"])


@router.get("/client/profile-overview", response_model=s.ClientProfileOverview, summary="Client User Profile Menu Overview")
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


@router.get("/consultant/profile-overview", response_model=s.ConsultantProfileOverview, summary="Consultant User Profile Menu Overview")
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


@router.get("/client/gdpr-consent", response_model=s.GdprConsentView, summary="Get GDPR Data Processing Consent Details")
async def get_client_gdpr_consent(user: CurrentUser = Depends(get_current_user)):
    return {
        "title": "Data Processing Consent",
        "description": "To process your immigration case, WebImove needs your consent to collect, store, and process your personal documents in accordance with GDPR. Your data is encrypted and only accessible to your assigned consultant.",
        "items": [
            "Collection and storage of identity documents",
            "Sharing data with relevant government authorities",
            "Processing sensitive data for your immigration case",
        ],
        "status": "Consent Provided",
        "badge_status": "Active",
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


@router.get("/clients/{client_id}", response_model=s.ClientDetailView, summary="Get Client profile detail for consultant view")
async def get_client_profile_detail(client_id: str,
                                    user: CurrentUser = Depends(require_consultant),
                                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_client_profile_detail(db, user, client_id)
