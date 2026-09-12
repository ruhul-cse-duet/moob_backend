from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    require_consultant,
    require_owner,
)
from app.modules.catalog import schemas as s
from app.modules.catalog import service

router = APIRouter(prefix="/catalog", tags=["Procedure Catalog"])


# --------------------------------------------------------------------------- #
# Process areas
# --------------------------------------------------------------------------- #


@router.get("/areas", response_model=List[s.ProcessAreaOut],
            summary="Process areas this organization works in")
async def list_areas(include_inactive: bool = Query(False),
                     user: CurrentUser = Depends(get_current_user),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Readable by everyone in the workspace, clients included.

    A client sees the area their own case sits in; they never choose one. Read
    access is what lets the app label a case without a second round trip.
    """
    return await service.list_areas(db, include_inactive=include_inactive)


@router.post("/areas", response_model=s.ProcessAreaOut,
             status_code=status.HTTP_201_CREATED,
             summary="Add a process area")
async def create_area(payload: s.ProcessAreaIn,
                      user: CurrentUser = Depends(require_owner),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Owner only: this decides what the whole organization can take on."""
    return await service.create_area(db, user, payload)


@router.patch("/areas/{area_id}", response_model=s.ProcessAreaOut,
              summary="Rename a process area, or switch it on and off")
async def update_area(area_id: str, payload: s.ProcessAreaUpdate,
                      user: CurrentUser = Depends(require_owner),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Switching an area off hides it from the pickers and leaves the procedures
    inside it alone - cases are still running on them."""
    return await service.update_area(db, area_id, payload)


# --------------------------------------------------------------------------- #
# Procedures
# --------------------------------------------------------------------------- #


@router.get("/procedures", response_model=List[s.ProcedureOut],
            summary="Procedures in this organization's catalogue")
async def list_procedures(area: Optional[str] = Query(None, alias="area_key"),
                          include_inactive: bool = Query(False),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_procedures(db, area_key=area,
                                         include_inactive=include_inactive)


@router.get("/procedures/{procedure_id}", response_model=s.ProcedureOut,
            summary="One procedure, with its documents, fields and stages")
async def get_procedure(procedure_id: str,
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_procedure(db, procedure_id)


@router.post("/procedures", response_model=s.ProcedureOut,
             status_code=status.HTTP_201_CREATED,
             summary="Create a procedure")
async def create_procedure(payload: s.ProcedureIn,
                           user: CurrentUser = Depends(require_consultant),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.create_procedure(db, user, payload)


@router.patch("/procedures/{procedure_id}", response_model=s.ProcedureOut,
              summary="Edit a procedure")
async def update_procedure(procedure_id: str, payload: s.ProcedureUpdate,
                           user: CurrentUser = Depends(require_consultant),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Cases already open are unaffected: each copies the checklist it was
    opened with, so edits here apply to the next case, not the last one."""
    return await service.update_procedure(db, procedure_id, payload)


@router.post("/procedures/{procedure_id}/duplicate", response_model=s.ProcedureOut,
             status_code=status.HTTP_201_CREATED,
             summary="Copy a procedure to edit")
async def duplicate_procedure(procedure_id: str, payload: s.DuplicateProcedure,
                              user: CurrentUser = Depends(require_consultant),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.duplicate_procedure(db, user, procedure_id, payload.name)


@router.delete("/procedures/{procedure_id}", response_model=s.ProcedureOut,
               summary="Retire a procedure")
async def deactivate_procedure(procedure_id: str,
                               user: CurrentUser = Depends(require_consultant),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Deactivated, not deleted. A case that names it would otherwise lose the
    record of what it is."""
    return await service.deactivate_procedure(db, procedure_id)


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


@router.get("/templates", response_model=List[s.TemplateOut],
            summary="Starting points a procedure can be copied from")
async def list_templates(area: Optional[str] = Query(None, alias="area_key"),
                         user: CurrentUser = Depends(require_consultant)):
    """Nothing here is in use until a tenant copies it, and the copy is theirs
    to rename or rewrite."""
    return service.list_templates(area)


@router.post("/templates/{template_key}", response_model=s.ProcedureOut,
             status_code=status.HTTP_201_CREATED,
             summary="Create a procedure from a template")
async def create_from_template(template_key: str,
                               payload: s.DuplicateProcedure,
                               user: CurrentUser = Depends(require_consultant),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.create_from_template(db, user, template_key, payload.name)
