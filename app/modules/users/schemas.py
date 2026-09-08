from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.validators import Email, OptionalPhone, Phone

from app.core.enums import Role, UserStatus


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=120)
    mobile: OptionalPhone = None
    title: Optional[str] = None
    language: Optional[str] = None
    nationality: Optional[str] = None
    country_of_residence: Optional[str] = None
    avatar_url: Optional[str] = None


class TeamInvite(BaseModel):
    full_name: str
    email: Email
    mobile: OptionalPhone = None
    title: str = "Consultant"


class ClientCreate(BaseModel):
    full_name: str
    email: Email
    mobile: Phone
    nationality: Optional[str] = None
    language: Optional[str] = None
    country_of_residence: Optional[str] = None


class AssignPartnerPayload(BaseModel):
    partner_id: str = Field(description="Partner who will process every case of this "
                                        "client, acting like a consultant")


class UserOut(BaseModel):
    id: str
    full_name: str
    email: Email
    mobile: OptionalPhone = None
    role: Role
    title: Optional[str] = None
    partner_role: Optional[str] = None
    status: UserStatus
    avatar_url: Optional[str] = None
    language: Optional[str] = None
    consultant_id: Optional[str] = None
    consultant_info: Optional[dict] = None
    organization_info: Optional[dict] = None
    created_at: Optional[datetime] = None
    # Only on the response that creates an invited account: whether the
    # invitation email actually went out, and the code to pass on if it did not.
    invite_token: Optional[str] = None
    invite_email_sent: Optional[bool] = None



class ClientProfileOverview(BaseModel):
    user: UserOut
    menu_workspace: list
    menu_account: list
    menu_support: list


class ConsultantProfileOverview(BaseModel):
    user: UserOut
    menu_workspace: list
    menu_account: list
    menu_support: list


class ClientDetailView(BaseModel):
    id: str
    full_name: str
    status: str = "Active"
    email: Email
    mobile: OptionalPhone = None
    country: Optional[str] = None
    consultant_id: str
    partner_id: Optional[str] = None
    partner_name: Optional[str] = None
    gdpr_consent_status: str = "recorded"
    immigration_cases: List[dict] = []
    # A key, not the sentence. The app holds the wording, in every language it
    # ships - a server-side sentence is English wherever it lands.
    banner_notice_key: str = "cases_created_after_request_approved"



class NotificationSettings(BaseModel):
    push_notifications: bool = True
    case_updates: bool = True


class ResetPasswordPayload(BaseModel):
    current_password: str = Field(min_length=8)
    password: str = Field(min_length=8)
    confirm_password: str = Field(min_length=8)


class GdprConsentView(BaseModel):
    title: str = "Data Processing Consent"
    description: str
    items: List[str]
    status: str = "provided"
    badge_status: str = "active"
    is_active: bool = True
    granted_at: Optional[datetime] = None


class LegalDocumentView(BaseModel):
    title: str
    last_updated: str
    sections: List[str]
    contact_email: Optional[str] = None


