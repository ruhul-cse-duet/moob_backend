from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
    require_owner,
)
from app.core.enums import Role
from app.modules.users import schemas as s
from app.modules.users import service
from app.schemas.common import PageParams

router = APIRouter(tags=["Users, Team & Clients"])


@router.get("/me", response_model=s.UserOut, summary="My profile")
async def me(user: CurrentUser = Depends(get_current_user),
             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.me(db, user)


@router.patch("/me", response_model=s.UserOut)
async def update_me(payload: s.ProfileUpdate,
                    user: CurrentUser = Depends(get_current_user),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.update_profile(db, user, payload)


@router.get("/users", summary="Directory (consultants, partners, clients)")
async def list_users(role: Optional[Role] = Query(None), search: Optional[str] = Query(None),
                     consultant_id: Optional[str] = Query(
                         None, description="Clients owned by, or partners working with, "
                                           "this consultant"),
                     params: PageParams = Depends(page_params),
                     user: CurrentUser = Depends(require_consultant),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_users(db, params, role, search, consultant_id)


@router.get("/users/{user_id}", response_model=s.UserOut)
async def get_user(user_id: str,
                   user: CurrentUser = Depends(require_consultant),
                   db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_user(db, user_id)


@router.post("/team", response_model=s.UserOut, status_code=status.HTTP_201_CREATED,
             summary="Team & Seats: invite another consultant")
async def invite_team_member(payload: s.TeamInvite,
                             user: CurrentUser = Depends(require_owner),
                             tenant: dict = Depends(require_active_tenant),
                             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.invite_team_member(db, user, tenant, payload)


@router.post("/clients", response_model=s.UserOut, status_code=status.HTTP_201_CREATED,
             summary="Consultant adds a client to the workspace")
async def create_client(payload: s.ClientCreate,
                        user: CurrentUser = Depends(require_consultant),
                        tenant: dict = Depends(require_active_tenant),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """The creating consultant owns this client."""
    return await service.create_client(db, user, tenant, payload)
