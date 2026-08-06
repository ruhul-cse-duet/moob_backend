from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import CurrentUser, get_current_user, require_owner
from app.core.exceptions import NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db

router = APIRouter(prefix="/organization", tags=["Organization"])


class OrganizationUpdate(BaseModel):
    name: Optional[str] = None
    country: Optional[str] = None
    office_address: Optional[str] = None
    logo_url: Optional[str] = None
    website: Optional[str] = None
    phone: Optional[str] = None


@router.get("", summary="My organization")
async def get_organization(user: CurrentUser = Depends(get_current_user)):
    tenant = await platform_db().tenants.find_one({"_id": oid(user.tenant_id)})
    if not tenant:
        raise NotFound("Organization not found")
    return serialize(tenant)


@router.patch("", summary="Update organization details (owner only)")
async def update_organization(payload: OrganizationUpdate,
                              user: CurrentUser = Depends(require_owner)):
    data = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    data["updated_at"] = utcnow()
    await platform_db().tenants.update_one({"_id": oid(user.tenant_id)}, {"$set": data})
    return serialize(await platform_db().tenants.find_one({"_id": oid(user.tenant_id)}))
