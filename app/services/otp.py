from datetime import timedelta
from typing import Optional

from app.core.config import settings
from app.core.enums import OtpPurpose
from app.core.exceptions import BadRequest, Conflict
from app.core.utils import constant_time_equals, random_code, utcnow
from app.db.mongo import platform_db
from app.services.email import send_otp_email


def _debug_code_allowed() -> bool:
    return bool(settings.DEBUG) and settings.ENVIRONMENT.lower() in {"development", "local", "test"}


def _now_like(value):
    """Mongo returns naive UTC datetimes; compare against a matching-awareness now()."""
    now = utcnow()
    return now if value.tzinfo else now.replace(tzinfo=None)



async def issue_otp(*, email: str, purpose: OtpPurpose, tenant_id: Optional[str] = None) -> dict:
    db = platform_db()
    email = email.lower().strip()
    existing = await db.otp_codes.find_one({"email": email, "purpose": purpose.value})

    if existing:
        elapsed = (_now_like(existing["created_at"]) - existing["created_at"]).total_seconds()
        if elapsed < settings.OTP_RESEND_COOLDOWN_SECONDS:
            raise Conflict(
                f"Please wait {int(settings.OTP_RESEND_COOLDOWN_SECONDS - elapsed)}s before requesting a new code"
            )
        await db.otp_codes.delete_many({"email": email, "purpose": purpose.value})

    code = random_code(settings.OTP_LENGTH)
    now = utcnow()
    doc = {
        "email": email,
        "purpose": purpose.value,
        "code": code,
        "tenant_id": tenant_id,
        "attempts": 0,
        "created_at": now,
        "expires_at": now + timedelta(minutes=settings.OTP_EXPIRE_MINUTES),
    }
    await db.otp_codes.insert_one(doc)
    await send_otp_email(to=email, code=code, purpose=purpose.value)

    return {
        "expires_in": settings.OTP_EXPIRE_MINUTES * 60,
        "resend_in": settings.OTP_RESEND_COOLDOWN_SECONDS,
        # Echoed back only on a genuine local dev box, so the mobile team can
        # test without SMTP. ENVIRONMENT alone is not enough of a gate: it
        # defaults to "development", so any deployment that forgets to set it
        # would hand out every OTP over the API.
        "debug_code": code if _debug_code_allowed() else None,
    }


async def verify_otp(*, email: str, purpose: OtpPurpose, code: str) -> bool:
    db = platform_db()
    email = email.lower().strip()
    record = await db.otp_codes.find_one({"email": email, "purpose": purpose.value})
    if not record:
        raise BadRequest("No active code for this email. Request a new one.")

    expires = record["expires_at"]
    if expires < _now_like(expires):
        await db.otp_codes.delete_one({"_id": record["_id"]})
        raise BadRequest("This code has expired. Request a new one.")

    if record["attempts"] >= settings.OTP_MAX_ATTEMPTS:
        await db.otp_codes.delete_one({"_id": record["_id"]})
        raise BadRequest("Too many incorrect attempts. Request a new code.")

    if not constant_time_equals(record["code"], code.strip()):
        await db.otp_codes.update_one({"_id": record["_id"]}, {"$inc": {"attempts": 1}})
        raise BadRequest("Incorrect code")

    await db.otp_codes.delete_one({"_id": record["_id"]})
    return True
