from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Header, Query, Request, status

from app.core.deps import CurrentUser, get_current_user
from app.core.enums import BillingCycle, PlanCode
from app.modules.auth import schemas as s
from app.modules.auth import service
from app.modules.subscriptions.plans import PLANS, order_summary
from app.schemas.common import Message

router = APIRouter(prefix="/auth", tags=["Auth & Onboarding"])


def _bearer(authorization: Optional[str] = Header(None)) -> str:
    return authorization.split(" ", 1)[1] if authorization and " " in authorization else ""


# ------------------------- consultant signup (6 steps) -------------------------
@router.post("/signup/personal", response_model=s.OnboardingToken,
             status_code=status.HTTP_201_CREATED,
             summary="Step 1 of 6 · Personal Info")
async def signup_personal(payload: s.SignupPersonal):
    return await service.start_signup(payload)


@router.post("/signup/organization", summary="Step 2 of 6 · Organization / Professional Credentials")
async def signup_organization(payload: s.SignupOrganization, token: str = Depends(_bearer)):
    return await service.set_organization(token, payload)


@router.post("/signup/verify", response_model=s.OnboardingToken,
             summary="Step 3 of 6 · Verify email")
async def signup_verify(code: str = Body(embed=True), token: str = Depends(_bearer)):
    return await service.verify_signup_email(token, code)


@router.post("/signup/password", response_model=s.OnboardingToken,
             summary="Step 4 of 6 · Protect your workspace (Password)")
async def signup_password(payload: s.SignupPassword, token: str = Depends(_bearer)):
    return await service.set_password(token, payload)


@router.post("/signup/verify/resend", response_model=s.OtpIssued,
             summary="Resend the six-digit code")
async def signup_resend(token: str = Depends(_bearer)):
    issued = await service.resend_signup_otp(token)
    return {"detail": "A new code is on its way", **issued}


@router.get("/plans", response_model=List[s.PlanOut], summary="Step 5 of 6 · Plan catalogue")
async def list_plans():
    return list(PLANS.values())


@router.get("/plans/{plan_code}/summary", response_model=s.OrderSummary,
            summary="Order summary for a plan and cycle")
async def plan_summary(plan_code: PlanCode, billing_cycle: BillingCycle = Query(BillingCycle.MONTHLY)):
    from datetime import timedelta
    from app.core.utils import utcnow
    renews = utcnow() + (timedelta(days=365) if billing_cycle == BillingCycle.ANNUAL else timedelta(days=30))
    return {"plan_code": plan_code, "billing_cycle": billing_cycle,
            "renews_on": renews, **order_summary(plan_code, billing_cycle)}


@router.post("/signup/plan", summary="Step 4 of 5 · Choose a plan")
async def signup_plan(payload: s.SignupPlan, token: str = Depends(_bearer)):
    return await service.choose_plan(token, payload)


@router.post("/signup/payment", response_model=s.TenantActivated,
             summary="Step 5 of 5 · Pay and activate the tenant")
async def signup_payment(payload: s.SignupPayment, token: str = Depends(_bearer)):
    return await service.complete_payment(token, payload)


# ------------------------- client self-registration (7-Step Flow) -------------------------
@router.post("/client/signup/step1", response_model=s.ClientOnboardingToken,
             status_code=status.HTTP_201_CREATED,
             summary="Step 1 of 7 (Client) · Account")
async def client_signup_step1(payload: s.ClientSignupStep1):
    return await service.start_client_signup(payload)


@router.post("/client/signup/verify", response_model=s.ClientOnboardingToken,
             summary="Step 2 of 7 (Client) · Verification")
async def client_signup_verify(payload: s.ClientSignupVerify, token: str = Depends(_bearer)):
    return await service.verify_client_signup_email(token, payload.code)


@router.post("/client/signup/verify/resend", response_model=s.OtpIssued,
             summary="Resend verification code for client signup")
async def client_signup_resend(token: str = Depends(_bearer)):
    issued = await service.resend_client_signup_otp(token)
    return {"detail": "A new verification code has been sent to your email", **issued}


@router.post("/client/signup/password", response_model=s.ClientOnboardingToken,
             summary="Step 3 of 7 (Client) · Create Password")
async def client_signup_password(payload: s.ClientSignupPassword, token: str = Depends(_bearer)):
    return await service.set_client_password(token, payload)


@router.post("/client/signup/immigration", response_model=s.ClientOnboardingToken,
             summary="Step 4 of 7 (Client) · Immigration Profile")
async def client_signup_immigration(payload: s.ClientSignupImmigration, token: str = Depends(_bearer)):
    return await service.set_client_immigration(token, payload)


@router.post("/client/signup/consultant", response_model=s.ClientOnboardingToken,
             summary="Step 5 of 7 (Client) · Consultant Selection")
async def client_signup_consultant(payload: s.ClientSignupConsultant, token: str = Depends(_bearer)):
    return await service.set_client_consultant(token, payload)


@router.post("/client/signup/confirm", response_model=s.ClientOnboardingToken,
             summary="Step 6 of 7 (Client) · Confirm and Submit")
async def client_signup_confirm(payload: s.ClientSignupConfirm, token: str = Depends(_bearer)):
    return await service.confirm_client_signup(token, payload)


@router.post("/client/signup/agreements", response_model=s.ClientSignupCompleteResponse,
             summary="Step 7 of 7 (Client) · Agreements & Complete Account")
async def client_signup_agreements(payload: s.ClientSignupAgreements, request: Request, token: str = Depends(_bearer)):
    return await service.finalize_client_signup(token, payload, _session(request))


@router.get("/consultants", response_model=List[s.ConsultantPublic],
            summary="Consultants a client can sign up under (public)")
async def public_consultants(search: Optional[str] = Query(None),
                             organization_id: Optional[str] = Query(None)):
    """Step 1 of client signup: choose a consultant. Spans every active organization."""
    return await service.list_public_consultants(search, organization_id)


@router.get("/organizations", response_model=List[s.OrganizationPublic],
            summary="Organizations a client can join (public)")
async def organizations(search: Optional[str] = Query(None)):
    return await service.list_organizations(search)


@router.post("/register/client", status_code=status.HTTP_201_CREATED,
             summary="Legacy client registration endpoint")
async def register_client(payload: s.ClientRegister):
    """Sends an email OTP. The account cannot sign in until it is verified."""
    return await service.register_client(payload)


@router.post("/verify-email", response_model=s.TokenPair,
             summary="Legacy client verify email endpoint")
async def verify_email(payload: s.OtpVerify):
    return await service.verify_account_email(payload.email, payload.code)


@router.post("/verify-email/resend", response_model=s.OtpIssued,
             summary="Send a fresh verification code")
async def resend_verification(payload: s.OtpRequest):
    issued = await service.resend_verification(payload.email)
    return {"detail": "If that account exists, a new code has been sent", **issued}



@router.get("/invite/{token}", response_model=s.InvitePreview,
            summary="Inspect an invitation before accepting it")
async def invite_preview(token: str):
    """Lets the accept screen show the organization, role and who invited them."""
    return await service.preview_invite(token)


@router.post("/invite/accept", response_model=s.TokenPair,
             summary="Accept an invitation, set a password, and sign in")
async def accept_invite(payload: s.AcceptInvite, request: Request):
    """Single-use link. The invitee lands under the consultant who invited them."""
    return await service.accept_invite(payload.token, payload.password,
                                       _session(request))


# ------------------------------- sessions -------------------------------
def _session(request: Request) -> dict:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "device": request.headers.get("x-device-name"),
    }


@router.post("/login", response_model=s.LoginResponse,
             summary="Sign in (all roles). Returns a 2FA challenge when 2FA is on.")
async def login(payload: s.LoginRequest, request: Request):
    return await service.login(payload.email, payload.password, _session(request))


@router.post("/login/2fa", response_model=s.TokenPair,
             summary="Second leg of a two-factor sign-in")
async def login_2fa(payload: s.TwoFactorVerify, request: Request):
    return await service.verify_login_2fa(payload.email, payload.code, _session(request))


@router.post("/refresh", response_model=s.TokenPair)
async def refresh(payload: s.RefreshRequest):
    return await service.refresh(payload.refresh_token)


@router.post("/logout", response_model=Message)
async def logout(payload: s.RefreshRequest):
    await service.logout(payload.refresh_token)
    return {"detail": "Signed out"}


@router.post("/forgot-password", response_model=s.OtpIssued)
async def forgot_password(payload: s.ForgotPasswordRequest):
    issued = await service.forgot_password(payload.email)
    return {"detail": "If that email exists, a reset code has been sent", **issued}


@router.post("/reset-password", response_model=Message)
async def reset_password(payload: s.ResetPasswordRequest):
    await service.reset_password(payload.email, payload.code, payload.password)
    return {"detail": "Password updated. You can sign in now."}


@router.post("/change-password", response_model=Message)
async def change_password(payload: s.ChangePasswordRequest,
                          user: CurrentUser = Depends(get_current_user)):
    await service.change_password(user, payload.current_password, payload.password)
    return {"detail": "Password changed"}


@router.get("/me", summary="Current session profile")
async def me(user: CurrentUser = Depends(get_current_user)):
    from app.core.utils import serialize
    return {"role": user.role, "tenant_id": user.tenant_id,
            "user": serialize({k: v for k, v in user.raw.items() if k != "password_hash"})}
