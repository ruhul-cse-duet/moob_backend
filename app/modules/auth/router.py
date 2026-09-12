from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Header, Query, Request, status

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.deps import language as request_language
from app.core.i18n import translate
from app.core.enums import BillingCycle, PlanCode
from app.core.utils import client_ip
from app.modules.auth import schemas as s
from app.modules.auth import service
from app.modules.subscriptions.plans import order_summary, plan_catalogue
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
    return list((await plan_catalogue()).values())


@router.get("/payment-config", response_model=s.PaymentConfig,
            summary="What the client needs to tokenise a card")
async def payment_config():
    """The publishable key is meant to be public — it can only create tokens.

    When card_tokenization is false the client must not collect a card at all:
    Stripe refuses raw card numbers, so the form would only ever fail.
    """
    from app.services import stripe_service
    return stripe_service.client_config()


@router.get("/plans/{plan_code}/summary", response_model=s.OrderSummary,
            summary="Order summary for a plan and cycle")
async def plan_summary(plan_code: PlanCode, billing_cycle: BillingCycle = Query(BillingCycle.MONTHLY)):
    from datetime import timedelta
    from app.core.utils import utcnow
    renews = utcnow() + (timedelta(days=365) if billing_cycle == BillingCycle.ANNUAL else timedelta(days=30))
    return {"plan_code": plan_code, "billing_cycle": billing_cycle,
            "renews_on": renews, **(await order_summary(plan_code, billing_cycle))}


@router.post("/signup/plan", summary="Step 4 of 5 · Choose a plan")
async def signup_plan(payload: s.SignupPlan, token: str = Depends(_bearer)):
    return await service.choose_plan(token, payload)


@router.post("/signup/payment", response_model=s.TenantActivated,
             summary="Step 5 of 5 · Pay and activate the tenant")
async def signup_payment(payload: s.SignupPayment, token: str = Depends(_bearer)):
    return await service.complete_payment(token, payload)


# ------------------------- clients join by invitation only -------------------------
#
# The public client signup used to live here: eight steps under
# /auth/client/signup/... that let anyone create a client account, search a
# public directory of consultancies, pick one (or skip), and choose their own
# immigration procedure.
#
# All three are wrong for what this product is. WebImove's customer is the
# consultancy; an end client is not a self-serve user, and the consultant - not
# the client - decides what procedure a case follows. A client who could sign up
# alone could also exist with no consultancy at all, which is an account nobody
# owns and no workspace contains.
#
# The path that replaces it is already here:
#
#   POST /api/v1/users/clients   consultant creates the client, invitation sent
#   GET  /api/v1/auth/invite/{token}   the client sees who invited them
#   POST /api/v1/auth/invite/accept    they set a password and are signed in
#
# Personal and document data - passport number among it - is collected after
# that, on the profile, which is after consent and inside the consultancy's
# space rather than before either exists.


# The public consultant and organization directories used to be here. A client
# never browses tenants: which consultancy they belong to comes from the
# invitation, and listing every firm's name, city, rating and client count to
# anonymous callers published one customer's business data to the next.


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
        "ip": client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "device": request.headers.get("x-device-name"),
    }


@router.get("/login/roles", summary="Select Your Role — options before sign-in")
async def login_roles(lang: str = Depends(request_language)):
    """The role chips shown before sign-in.

    Ids only. This screen is the first thing an unauthenticated user sees, so
    it is also the first place a server-side English label would show through -
    and the picker is the one screen where the app definitely already knows
    every option, because it has to draw an icon for each.
    """
    return {
        "success": True,
        "roles": [
            {
                "id": role_id,
                "label": translate(f"role.{role_id}", lang),
                "description": translate(f"role.{role_id}.description", lang),
            }
            for role_id in ("consultant", "partner", "client")
        ],
    }


@router.post("/platform/login", response_model=s.LoginResponse,
             summary="Platform administration sign-in (super admin)")
async def platform_login(payload: s.PlatformLoginRequest, request: Request):
    return await service.platform_login(
        payload.email, payload.password, payload.trust_device, _session(request)
    )


@router.post("/login", response_model=s.LoginResponse,
             summary="Sign in with an email and password")
async def login(payload: s.LoginRequest, request: Request):
    return await service.login(payload.email, payload.password, payload.role, _session(request))


@router.post("/login/2fa", response_model=s.TokenPair,
             summary="Second leg of a two-factor sign-in")
async def login_2fa(payload: s.TwoFactorVerify, request: Request):
    return await service.verify_login_2fa(
        payload.email, payload.code, payload.role, _session(request)
    )


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
