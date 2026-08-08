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


# ---------- Step 1 of 6 - Personal ----------
class SignupPersonal(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    mobile: str = Field(min_length=6, max_length=32)
    dob: Optional[str] = Field(None, description="Date of birth (optional)")
    profile_photo_url: Optional[str] = Field(None, description="Profile photo URL (optional)")


class OnboardingToken(BaseModel):
    onboarding_token: str
    step: str
    next_step: str


# ---------- Step 2 of 6 - Organization / Professional Credentials ----------
class SignupOrganization(BaseModel):
    organization_name: str = Field(min_length=2, max_length=160)
    business_type: str = Field(default="Immigration Consultancy")
    country: str
    office_address: str

    @field_validator("business_type")
    @classmethod
    def normalize_business_type(cls, v: str) -> str:
        if not v:
            return "Immigration Consultancy"
        return v

# ---------- Step 3 of 6 - Verify Email OTP ----------
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


# ---------- Step 4 of 6 - Create Password ----------
class SignupPassword(PasswordMixin):
    confirm_password: str

    @field_validator("confirm_password")
    @classmethod
    def match(cls, v: str, info):
        if info.data.get("password") and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v



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
    consultant_id: Optional[str] = None
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
    passport_number: Optional[str] = None
    nationality: Optional[str] = None
    destination_country: Optional[str] = None
    preferred_immigration_type: Optional[str] = None
    language: Optional[str] = None
    country_of_residence: Optional[str] = None

    @field_validator("confirm_password")
    @classmethod
    def match(cls, v: Optional[str], info):
        if v is not None and info.data.get("password") and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


# ---------- Client Step-by-Step Sign Up (7 Steps) ----------
class ClientOnboardingToken(BaseModel):
    client_onboarding_token: str
    step: str
    next_step: str


# Step 1: Account
class ClientSignupStep1(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    mobile: str = Field(min_length=6, max_length=32)
    dob: Optional[str] = Field(None, description="Date of birth (YYYY-MM-DD or MM/DD/YYYY)")
    accept_terms: bool = Field(..., description="Accept terms and privacy policy")
    profile_photo_url: Optional[str] = Field(None, description="Optional profile photo URL")

    @field_validator("accept_terms")
    @classmethod
    def must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("I agree to the WebImove Terms of Service and Privacy Policy")
        return v


# Step 2: Verification
class ClientSignupVerify(BaseModel):
    code: str = Field(min_length=4, max_length=8)


# Step 3: Create Password
class ClientSignupPassword(PasswordMixin):
    confirm_password: str

    @field_validator("confirm_password")
    @classmethod
    def match(cls, v: str, info):
        if info.data.get("password") and v != info.data["password"]:
            raise ValueError("Passwords do not match")
        return v


# Step 4: Immigration Profile
class ClientSignupImmigration(BaseModel):
    passport_number: Optional[str] = Field(None, description="Passport number")
    nationality: Optional[str] = Field(None, description="Nationality (e.g., Spanish)")
    destination_country: Optional[str] = Field(None, description="Destination country (e.g., USA)")
    preferred_immigration_type: Optional[str] = Field(None, description="Preferred immigration/visa type (e.g., Student Visa)")
    country_of_residence: Optional[str] = Field(None, description="Current country of residence")


# Step 5: Consultant Selection
class ClientSignupConsultant(BaseModel):
    consultant_id: Optional[str] = Field(None, description="Chosen consultant ID")
    organization_id: Optional[str] = Field(None, description="Chosen organization ID")


# Step 6: Confirmation & Submit
class ClientSignupConfirm(BaseModel):
    gdpr_consent: bool = Field(..., description="Consent to process personal data under GDPR")
    accept_terms_conditions: bool = Field(..., description="Accept WebImove Terms & Conditions")

    @field_validator("gdpr_consent", "accept_terms_conditions")
    @classmethod
    def must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("You must check both consents to submit registration")
        return v


# Step 7: Agreements & Finalize
class ClientSignupAgreements(BaseModel):
    terms_and_conditions: bool = True
    privacy_policy: bool = True
    gdpr_data_processing: bool = True
    immigration_case: bool = True
    sensitive_data_processing: bool = True
    whatsapp_notifications: bool = False
    email_notifications: bool = False
    ai_ocr_processing: bool = False
    ai_legal_assistant: bool = False
    partner_data_sharing: bool = False
    marketing_messages: bool = False


class ClientSignupCompleteResponse(BaseModel):
    token_pair: TokenPair
    request_summary: dict




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
