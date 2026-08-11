"""One-time setup endpoints for the platform.

Allows creation of an initial platform super admin when no platform admins exist.
"""
from fastapi import APIRouter, status
from pydantic import BaseModel, EmailStr, Field

from app.core.enums import UserStatus
from app.core.exceptions import Conflict
from app.core.security import hash_password
from app.core.utils import utcnow
from app.db.indexes import ensure_platform_indexes
from app.db.mongo import platform_db


router = APIRouter(prefix="/setup", tags=["Platform setup"])


class SuperAdminCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(default="Platform Admin")


@router.post("/super-admin", status_code=status.HTTP_201_CREATED,
             summary="Create initial platform super admin (one-time)")
async def create_initial_super_admin(payload: SuperAdminCreate):
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
