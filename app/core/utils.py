import random
import re
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
    return "".join(random.choices(string.digits, k=length))


def random_token(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=length))


def slugify_db(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug[:40] or "org"


def build_reference(prefix: str, counter: int) -> str:
    """REQ-204 / CAS-089 style human references seen in the design."""
    return f"{prefix}-{counter:03d}"
