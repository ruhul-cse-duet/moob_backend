import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ACCESS = "access"
REFRESH = "refresh"
ONBOARDING = "onboarding"   # short-lived token that carries an in-progress signup


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return pwd_context.verify(raw, hashed)


def _encode(payload: Dict[str, Any], expires: timedelta, token_type: str) -> str:
    now = datetime.now(timezone.utc)
    to_encode = {**payload, "iat": now, "exp": now + expires, "type": token_type}
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(*, user_id: str, role: str, tenant_id: Optional[str], email: str) -> str:
    return _encode(
        {"sub": user_id, "role": role, "tenant_id": tenant_id, "email": email},
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        ACCESS,
    )


def create_refresh_token(*, user_id: str, tenant_id: Optional[str]) -> str:
    # jti keeps two tokens minted in the same second distinct. Without it the
    # payload is identical down to `iat`, and refresh_tokens.token is unique -
    # so a double-clicked login used to fail on a duplicate key.
    return _encode(
        {"sub": user_id, "tenant_id": tenant_id, "jti": secrets.token_urlsafe(8)},
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        REFRESH,
    )


def create_onboarding_token(*, signup_id: str, step: str) -> str:
    return _encode({"sub": signup_id, "step": step}, timedelta(hours=6), ONBOARDING)


def decode_token(token: str, expected_type: Optional[str] = None) -> Dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc
    if expected_type and payload.get("type") != expected_type:
        raise ValueError(f"Expected a {expected_type} token")
    return payload
