"""One-time setup endpoints for the platform.

Allows creation of an initial platform super admin when no platform admins exist.
"""
from typing import Optional

from fastapi import APIRouter, Header, status
from pydantic import BaseModel, EmailStr, Field

from app.core.config import settings
from app.core.enums import UserStatus
from app.core.exceptions import Conflict, Forbidden
from app.core.security import hash_password
from app.core.utils import constant_time_equals, utcnow
from app.db.indexes import ensure_platform_indexes
from app.db.mongo import platform_db


router = APIRouter(prefix="/setup", tags=["Platform setup"])


class SuperAdminCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(default="Platform Admin")


@router.post("/super-admin", status_code=status.HTTP_201_CREATED,
             summary="Create initial platform super admin (one-time)")
async def create_initial_super_admin(
    payload: SuperAdminCreate,
    x_setup_token: Optional[str] = Header(
        default=None, alias="X-Setup-Token",
        description="Must match PLATFORM_SETUP_TOKEN. The endpoint is disabled when unset.",
    ),
):
    """Bootstrap the first platform administrator.

    Unauthenticated by necessity - there is no account to authenticate as yet.
    That makes it a land grab: whoever calls it first on a fresh deployment owns
    the platform. So it stays shut unless the operator sets PLATFORM_SETUP_TOKEN
    and presents it. With no token configured, use scripts_create_superadmin.py,
    which needs shell access to the server.
    """
    if not settings.PLATFORM_SETUP_TOKEN:
        raise Forbidden(
            "HTTP setup is disabled. Set PLATFORM_SETUP_TOKEN, or run "
            "scripts_create_superadmin.py on the server."
        )
    if not constant_time_equals(x_setup_token or "", settings.PLATFORM_SETUP_TOKEN):
        raise Forbidden("Invalid setup token")

    db = platform_db()
    # Ensure indexes exist (email unique constraint etc.)
    await ensure_platform_indexes()
    existing = await db.platform_admins.count_documents({})
    if existing:
        raise Conflict("Super admin already exists")

    email = payload.email.lower().strip()
    await db.platform_admins.insert_one({
        "email": email,
        "full_name": payload.full_name,
        "password_hash": hash_password(payload.password),
        "status": UserStatus.ACTIVE.value,
        "created_at": utcnow(),
    })
    return {"detail": f"Super admin created: {email}"}
