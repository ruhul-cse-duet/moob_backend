from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.enums import BillingCycle, PlanCode, Role


class PasswordMixin(BaseModel):
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def strong_enough(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Use 8+ characters with a number and symbol")
        if not any(not c.isalnum() for c in v):
            raise ValueError("Use 8+ characters with a number and symbol")
        return v


# ---------- Step 1 of 5 - Personal ----------
class SignupPersonal(PasswordMixin):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    mobile: str = Field(min_length=6, max_length=32)
    confirm_password: str

    @field_validator("confirm_password")
    @classmethod
    def match(cls, v: str, info):
        if info.data.get("password") and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


class OnboardingToken(BaseModel):
    onboarding_token: str
    step: str
    next_step: str


# ---------- Step 2 of 5 - Organization ----------
class SignupOrganization(BaseModel):
    organization_name: str = Field(min_length=2, max_length=160)
    business_type: str = "immigration"
    country: str
    office_address: str
    accept_terms: bool
    accept_privacy: bool

    @field_validator("business_type")
    @classmethod
    def immigration_only(cls, v: str) -> str:
        if v.lower() != "immigration":
            raise ValueError("WebImove supports immigration practices only")
        return v.lower()

    @field_validator("accept_terms", "accept_privacy")
    @classmethod
    def must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("You must accept the Terms and the Privacy Policy")
        return v


# ---------- Step 3 of 5 - Verify email ----------
class OtpRequest(BaseModel):
    email: EmailStr


class OtpVerify(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=8)


class OtpIssued(BaseModel):
    detail: str
    expires_in: int
    resend_in: int
    debug_code: Optional[str] = None


# ---------- Step 4 of 5 - Plan ----------
class SignupPlan(BaseModel):
    plan_code: PlanCode
    billing_cycle: BillingCycle = BillingCycle.MONTHLY


class PlanFeature(BaseModel):
    label: str


class PlanOut(BaseModel):
    code: PlanCode
    name: str
    tagline: str
    monthly_price: float
    annual_price: float
    recommended: bool = False
    consultant_seats: Optional[int]
    partner_seats: Optional[int]
    active_case_limit: Optional[int]
    features: List[str]


class OrderSummary(BaseModel):
    plan_code: PlanCode
    billing_cycle: BillingCycle
    subtotal: float
    estimated_tax: float
    total_due_today: float
    renews_on: datetime


# ---------- Step 5 of 5 - Payment ----------
class SignupPayment(BaseModel):
    name_on_card: str
    card_number: str = Field(min_length=12, max_length=19)
    expiry: str = Field(pattern=r"^(0[1-9]|1[0-2])/\d{2}$")
    cvc: str = Field(min_length=3, max_length=4)


class TenantActivated(BaseModel):
    tenant_id: str
    organization_name: str
    plan_code: PlanCode
    renews_on: datetime
    referral_code: str
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ---------- Login ----------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    trust_device: bool = False


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    role: Role
    tenant_id: Optional[str] = None
    user: dict


class LoginResponse(BaseModel):
    """Either a token pair, or a 2FA challenge. [INFERRED - Security.tsx]"""
    two_factor_required: bool = False
    challenge_token: Optional[str] = None
    method: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: Optional[int] = None
    role: Optional[Role] = None
    tenant_id: Optional[str] = None
    user: Optional[dict] = None


class TwoFactorVerify(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=8)


class RefreshRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(PasswordMixin):
    email: EmailStr
    code: str
    confirm_password: str

    @field_validator("confirm_password")
    @classmethod
    def match(cls, v: str, info):
        if info.data.get("password") and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


class ChangePasswordRequest(PasswordMixin):
    current_password: str


# ---------- Client self-signup: pick a consultant organization ----------
class OrganizationPublic(BaseModel):
    id: str
    name: str
    country: str
    consultant_name: str
    logo_url: Optional[str] = None
    partner_count: int = 0


class ConsultantPublic(BaseModel):
    """A consultant a client can sign up under."""
    id: str
    full_name: Optional[str] = None
    title: Optional[str] = None
    avatar_url: Optional[str] = None
    is_owner: bool = False
    organization: Optional[dict] = None
    active_clients: int = 0


class ClientRegister(PasswordMixin):
    """The client picks a consultant; the organization follows from that choice."""
    consultant_id: Optional[str] = Field(
        None, description="Chosen consultant. Required unless organization_id is given.")
    organization_id: Optional[str] = Field(
        None, description="Optional. If given without consultant_id, the workspace "
                          "owner becomes the consultant.")
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    mobile: str
    confirm_password: Optional[str] = None
    nationality: Optional[str] = None
    language: Optional[str] = None
    country_of_residence: Optional[str] = None

    @field_validator("confirm_password")
    @classmethod
    def match(cls, v: Optional[str], info):
        if v is not None and info.data.get("password") and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


class InvitePreview(BaseModel):
    """What the accept-invitation screen shows before asking for a password."""
    email: EmailStr
    full_name: Optional[str] = None
    role: Role
    partner_role: Optional[str] = None
    title: Optional[str] = None
    organization: Optional[dict] = None
    invited_by: Optional[dict] = None
    invited_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class AcceptInvite(PasswordMixin):
    token: str
    confirm_password: str

    @field_validator("confirm_password")
    @classmethod
    def match(cls, v: str, info):
        if info.data.get("password") and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v
