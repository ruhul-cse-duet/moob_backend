import re
import secrets
import string
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def oid(value: str) -> ObjectId:
    from app.core.exceptions import BadRequest

    if not ObjectId.is_valid(value):
        raise BadRequest(f"'{value}' is not a valid id")
    return ObjectId(value)


def serialize(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Recursively turn ObjectId into str and rename _id -> id."""
    if doc is None:
        return None
    out: Dict[str, Any] = {}
    for key, value in doc.items():
        key = "id" if key == "_id" else key
        out[key] = _clean(value)
    return out


def _clean(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {("id" if k == "_id" else k): _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def random_code(length: int) -> str:
    """Numeric OTP. `secrets`, not `random` - a Mersenne Twister stream is
    reconstructible from a handful of observed outputs, and these codes are the
    only thing standing between an email address and an account."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def random_token(length: int = 32) -> str:
    """URL-safe invite / reset token. Same reasoning as `random_code`: this token
    is the sole credential in an invitation link."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def client_ip(request) -> Optional[str]:
    """The caller's IP, as far as we can honestly tell.

    Behind a reverse proxy every request appears to come from the proxy, which
    would put every user of the platform in one throttling bucket and record the
    same address against every sign-in. X-Forwarded-For fixes that - but only
    when a proxy we control actually sets it, because a direct caller can put
    anything in that header. Hence the TRUST_PROXY_HEADERS gate.
    """
    from app.core.config import settings

    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most entry is the original client; the rest are proxy hops.
            first = forwarded.split(",")[0].strip()
            if first:
                return first
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else None


def constant_time_equals(a: str, b: str) -> bool:
    """Compare secrets without leaking their length or prefix through timing."""
    return secrets.compare_digest((a or "").encode(), (b or "").encode())


def slugify_db(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug[:40] or "org"


def build_reference(prefix: str, counter: int) -> str:
    """REQ-204 / CAS-089 style human references seen in the design."""
    return f"{prefix}-{counter:03d}"
