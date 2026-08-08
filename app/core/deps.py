from dataclasses import dataclass
from typing import Optional, Sequence

from fastapi import Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.enums import CONSULTANT_ROLES, Role, TenantStatus
from app.core.exceptions import Forbidden, PaymentRequired, Unauthorized
from app.core.security import ACCESS, decode_token
from app.core.utils import oid
from app.db.mongo import platform_db, tenant_db
from app.schemas.common import PageParams

bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    id: str
    email: str
    role: Role
    tenant_id: Optional[str]
    raw: dict

    @property
    def consultant_id(self) -> Optional[str]:
        if self.role in CONSULTANT_ROLES:
            return self.id
        return self.raw.get("consultant_id")

    @property
    def is_consultant(self) -> bool:
        return self.role in CONSULTANT_ROLES

    @property
    def is_owner(self) -> bool:
        return self.role == Role.CONSULTANT_OWNER


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> CurrentUser:
    if creds is None:
        raise Unauthorized("Authorization header missing")
    try:
        payload = decode_token(creds.credentials, ACCESS)
    except ValueError as exc:
        raise Unauthorized(str(exc)) from exc

    tenant_id = payload.get("tenant_id")
    role = Role(payload["role"])

    if role == Role.SUPER_ADMIN:
        doc = await platform_db().platform_admins.find_one({"_id": oid(payload["sub"])})
    else:
        if not tenant_id:
            raise Unauthorized("Token is not bound to a workspace")
        doc = await tenant_db(tenant_id).users.find_one({"_id": oid(payload["sub"])})

    if not doc:
        raise Unauthorized("Account no longer exists")
    if doc.get("status") == "suspended":
        raise Forbidden("This account is suspended")

    return CurrentUser(
        id=str(doc["_id"]), email=doc["email"], role=role, tenant_id=tenant_id, raw=doc
    )


def require_roles(*roles: Role):
    allowed: Sequence[Role] = roles

    async def _guard(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if user.role not in allowed:
            raise Forbidden("Your role cannot access this resource")
        return user

    return _guard


require_consultant = require_roles(Role.CONSULTANT_OWNER, Role.CONSULTANT)
require_owner = require_roles(Role.CONSULTANT_OWNER)
require_partner = require_roles(Role.PARTNER)
require_client = require_roles(Role.CLIENT)
require_super_admin = require_roles(Role.SUPER_ADMIN)


async def get_tenant_db(user: CurrentUser = Depends(get_current_user)) -> AsyncIOMotorDatabase:
    if not user.tenant_id:
        raise Forbidden("No workspace bound to this account")
    return tenant_db(user.tenant_id)


async def require_active_tenant(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Blocks the whole workspace when the subscription is not paid."""
    tenant = await platform_db().tenants.find_one({"_id": oid(user.tenant_id)})
    if not tenant:
        raise Unauthorized("Workspace not found")
    if tenant["status"] != TenantStatus.ACTIVE.value:
        raise PaymentRequired("This workspace is not active. Complete the subscription payment.")
    return tenant


def page_params(
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)
) -> PageParams:
    return PageParams(page=page, page_size=page_size)
