"""
Security.tsx — active sessions, login history, two-factor.  [INFERRED]

2FA is email-OTP rather than TOTP: the OTP service already exists with expiry,
resend cooldown and attempt caps. Adding an authenticator app would mean a second
parallel mechanism. Swap in pyotp later if the design calls for it.
"""
from datetime import timedelta


from fastapi import APIRouter, Body, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import CurrentUser, get_current_user, get_tenant_db, page_params
from app.core.enums import OtpPurpose
from app.core.exceptions import BadRequest
from app.core.utils import oid, utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message, PageParams
from app.services import otp as otp_service
from app.services.pagination import paginate

router = APIRouter(prefix="/security", tags=["Security"])


@router.get("/overview", summary="Security posture for the account settings screen")
async def overview(user: CurrentUser = Depends(get_current_user),
                   db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    pdb = platform_db()
    return {
        "two_factor_enabled": bool(user.raw.get("two_factor_enabled")),
        "email_verified": bool(user.raw.get("email_verified")),
        "password_changed_at": user.raw.get("password_changed_at"),
        "active_sessions": await pdb.refresh_tokens.count_documents({"user_id": user.id}),
        "last_login_at": user.raw.get("last_login_at"),
        "failed_logins_7d": await pdb.audit_log.count_documents({
            "actor_email": user.email, "action": "login_failed",
            "created_at": {"$gte": utcnow() - timedelta(days=7)}}),
    }


@router.get("/sessions", summary="Devices currently signed in")
async def sessions(user: CurrentUser = Depends(get_current_user)):
    rows = []
    async for t in platform_db().refresh_tokens.find({"user_id": user.id}).sort("created_at", -1):
        rows.append({
            "id": str(t["_id"]),
            "device": t.get("device"),
            "ip": t.get("ip"),
            "user_agent": t.get("user_agent"),
            "created_at": t.get("created_at"),
            "expires_at": t.get("expires_at"),
        })
    return {"items": rows}


@router.delete("/sessions/{session_id}", response_model=Message, summary="Sign out one device")
async def revoke_session(session_id: str, user: CurrentUser = Depends(get_current_user)):
    result = await platform_db().refresh_tokens.delete_one(
        {"_id": oid(session_id), "user_id": user.id})
    if not result.deleted_count:
        raise BadRequest("That session does not exist or is not yours")
    return {"detail": "Session revoked"}


@router.post("/sessions/revoke-all", response_model=Message,
             summary="Sign out everywhere (this device included)")
async def revoke_all(user: CurrentUser = Depends(get_current_user)):
    result = await platform_db().refresh_tokens.delete_many({"user_id": user.id})
    return {"detail": f"{result.deleted_count} session(s) revoked"}


@router.get("/login-history", summary="Recent sign-in attempts")
async def login_history(params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await paginate(db, "login_history", {"user_id": user.id}, params,
                          sort=[("created_at", -1)])


@router.post("/2fa/enable", summary="Step 1 — send the code that turns 2FA on")
async def start_enable_2fa(user: CurrentUser = Depends(get_current_user)):
    if user.raw.get("two_factor_enabled"):
        raise BadRequest("Two-factor is already enabled")
    issued = await otp_service.issue_otp(email=user.email, purpose=OtpPurpose.LOGIN,
                                         tenant_id=user.tenant_id)
    return {"detail": "Enter the code sent to your email to finish enabling 2FA", **issued}


@router.post("/2fa/confirm", response_model=Message, summary="Step 2 — confirm and enable")
async def confirm_enable_2fa(code: str = Body(embed=True),
                             user: CurrentUser = Depends(get_current_user),
                             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await otp_service.verify_otp(email=user.email, purpose=OtpPurpose.LOGIN, code=code)
    await db.users.update_one(
        {"_id": oid(user.id)},
        {"$set": {"two_factor_enabled": True, "two_factor_method": "email",
                  "two_factor_enabled_at": utcnow(), "updated_at": utcnow()}})
    return {"detail": "Two-factor authentication is on"}


@router.post("/2fa/disable", response_model=Message,
             summary="Turn 2FA off (requires the current password)")
async def disable_2fa(password: str = Body(embed=True),
                      user: CurrentUser = Depends(get_current_user),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    from app.core.security import verify_password
    if not verify_password(password, user.raw.get("password_hash") or ""):
        raise BadRequest("Password is incorrect")
    await db.users.update_one({"_id": oid(user.id)},
                              {"$set": {"two_factor_enabled": False,
                                        "updated_at": utcnow()}})
    return {"detail": "Two-factor authentication is off"}
