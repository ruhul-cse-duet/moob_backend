from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
)
from app.modules.partners import schemas as s
from app.modules.partners import service
from app.schemas.common import Message, PageParams

router = APIRouter(prefix="/partners", tags=["Partner Management"])


@router.post("", response_model=s.PartnerOut, status_code=status.HTTP_201_CREATED,
             summary="Create a partner and email them an invitation")
async def invite_partner(payload: s.PartnerInvite,
                         user: CurrentUser = Depends(require_consultant),
                         tenant: dict = Depends(require_active_tenant),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """The inviting consultant owns this partner - they sign in under that consultant."""
    return await service.invite_partner(db, user, tenant, payload)


@router.get("", summary="Partner directory for this organization")
async def list_partners(search: Optional[str] = Query(None),
                        consultant_id: Optional[str] = Query(
                            None, description="Partners this consultant has delegated to"),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(require_consultant),
                        tenant: dict = Depends(require_active_tenant),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_partners(db, params, search, consultant_id)


@router.get("/seats", response_model=s.SeatUsage, summary="Seat usage against the plan")
async def seats(user: CurrentUser = Depends(require_consultant),
                tenant: dict = Depends(require_active_tenant),
                db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.seat_usage(db, tenant)


@router.post("/{partner_id}/resend", response_model=Message)
async def resend(partner_id: str,
                 user: CurrentUser = Depends(require_consultant),
                 tenant: dict = Depends(require_active_tenant),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.resend_invite(db, tenant, partner_id)


@router.post("/{partner_id}/suspend", response_model=s.PartnerOut)
async def suspend(partner_id: str, suspend: bool = Query(True),
                  user: CurrentUser = Depends(require_consultant),
                  tenant: dict = Depends(require_active_tenant),
                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.suspend_partner(db, partner_id, suspend)


@router.delete("/{partner_id}", response_model=Message, summary="Revoke partner access")
async def revoke(partner_id: str,
                 user: CurrentUser = Depends(require_consultant),
                 tenant: dict = Depends(require_active_tenant),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.revoke_partner(db, partner_id)
