from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import language as request_language
from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
    require_consultant_or_partner,
)
from app.core.enums import CaseStage
from app.modules.cases import schemas as s
from app.modules.cases import service
from app.schemas.common import PageParams

router = APIRouter(prefix="/cases", tags=["Immigration Cases"],
                   dependencies=[Depends(require_active_tenant)])


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_case(payload: s.CaseCreate,
                      user: CurrentUser = Depends(require_consultant),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.create_case(db, user, payload)


@router.get("", summary="Case list, filterable by workflow stage and consultant")
async def list_cases(stage: Optional[CaseStage] = Query(None),
                     search: Optional[str] = Query(None),
                     consultant_id: Optional[str] = Query(
                         None, description="Only this consultant's cases"),
                     params: PageParams = Depends(page_params),
                     user: CurrentUser = Depends(get_current_user),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                     lang: str = Depends(request_language)):
    return await service.list_cases(db, user, params, stage, search, consultant_id, lang)


@router.get("/stage-counts", summary="Counts for the stage filter chips")
async def stage_counts(consultant_id: Optional[str] = Query(None),
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.stage_counts(db, user, consultant_id)


@router.get("/{case_id}", summary="Case detail with documents, tasks and timeline")
async def get_case(case_id: str,
                   user: CurrentUser = Depends(get_current_user),
                   db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                   lang: str = Depends(request_language)):
    return await service.get_case(db, user, case_id, lang)


@router.patch("/{case_id}", response_model=s.CaseOut)
async def update_case(case_id: str, payload: s.CaseUpdate,
                      # Case configuration is consultant-only - a partner works
                      # through the task they were delegated, never the case
                      # itself. See app/modules/documents/router.py's own note
                      # on the same product review item (5.1).
                      user: CurrentUser = Depends(require_consultant),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.update_case(db, user, case_id, payload)


@router.get("/{case_id}/timeline", summary="Immigration timeline with completion state")
async def case_timeline(case_id: str,
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                        lang: str = Depends(request_language)):
    return {"timeline": await service.timeline(db, case_id, lang)}


@router.post("/{case_id}/advance", response_model=s.CaseOut, summary="Advance stage")
async def advance_stage(case_id: str, payload: s.AdvanceStage,
                        # Case configuration is consultant-only - see the note
                        # on `update_case` above.
                        user: CurrentUser = Depends(require_consultant),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.advance_stage(db, user, case_id, payload)


@router.post("/{case_id}/authority-request",
             summary="Log a request for more information from the authority (repeatable)")
async def authority_request(case_id: str, payload: s.AuthorityRequest,
                            user: CurrentUser = Depends(require_consultant),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.authority_request(db, user, case_id, payload)


@router.get("/{case_id}/history", summary="Full case history - every action, dated and attributed")
async def case_history(case_id: str,
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                       lang: str = Depends(request_language)):
    return {"events": await service.case_history(db, user, case_id, lang)}


@router.post("/{case_id}/ai-form",
             summary="Pre-fill the case's intake form from its documents (item 7.3)")
async def ai_fill_form(case_id: str,
                       user: CurrentUser = Depends(require_consultant_or_partner),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.ai_fill_form(db, user, case_id)


@router.patch("/{case_id}/form/{field_key}",
             summary="Fill or correct one field of the case's intake form")
async def update_form_field(case_id: str, field_key: str, payload: s.FormFieldUpdate,
                            # Open to the client for their own case (item C2) -
                            # `service.update_form_field` checks ownership and
                            # tags who answered it.
                            user: CurrentUser = Depends(get_current_user),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.update_form_field(db, user, case_id, field_key, payload)


@router.post("/{case_id}/ai-guidance",
             summary="Generate AI guidance and auto-create client tasks")
async def ai_guidance(case_id: str, create_tasks: bool = Query(True),
                      user: CurrentUser = Depends(require_consultant_or_partner),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.generate_guidance(db, user, case_id, create_tasks)
