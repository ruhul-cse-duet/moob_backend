from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
    require_partner,
)
from app.core.enums import TaskAssigneeType, TaskStatus
from app.modules.tasks import schemas as s
from app.modules.tasks import service
from app.schemas.common import PageParams
from app.services import storage

router = APIRouter(prefix="/tasks", tags=["Tasks (Partner & Client)"],
                   dependencies=[Depends(require_active_tenant)])


# ─── Partner Home Dashboard ────────────────────────────────────────────
@router.get("/partner/dashboard", summary="Partner home dashboard with task summary")
async def partner_dashboard(user: CurrentUser = Depends(require_partner),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_partner_dashboard(db, user)


# ─── Task CRUD ─────────────────────────────────────────────────────────
@router.post("", response_model=s.TaskOut, status_code=status.HTTP_201_CREATED,
             summary="Assign a partner (or client) task")
async def create_task(payload: s.TaskCreate,
                      user: CurrentUser = Depends(require_consultant),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.create_task(db, user, payload)


@router.get("", summary="Delegated work / My tasks")
async def list_tasks(status_filter: Optional[TaskStatus] = Query(None, alias="status"),
                     case_id: Optional[str] = Query(None),
                     assignee_type: Optional[TaskAssigneeType] = Query(None),
                     mine: bool = Query(False),
                     consultant_id: Optional[str] = Query(
                         None, description="Only this consultant's tasks"),
                     params: PageParams = Depends(page_params),
                     user: CurrentUser = Depends(get_current_user),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_tasks(db, user, params, status_filter, case_id,
                                    assignee_type, mine, consultant_id)


@router.get("/{task_id}", response_model=s.TaskOut)
async def get_task(task_id: str,
                   user: CurrentUser = Depends(get_current_user),
                   db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_task(db, user, task_id)


@router.patch("/{task_id}", response_model=s.TaskOut)
async def update_task(task_id: str, payload: s.TaskUpdate,
                      user: CurrentUser = Depends(get_current_user),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.update_task(db, user, task_id, payload)


@router.post("/{task_id}/status", response_model=s.TaskOut)
async def set_status(task_id: str, payload: s.TaskStatusUpdate,
                     user: CurrentUser = Depends(get_current_user),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.set_status(db, user, task_id, payload)


# ─── Partner: Mark as Completed ───────────────────────────────────────
@router.post("/{task_id}/complete", response_model=s.TaskOut,
             summary="Partner marks task as completed with delivery notes")
async def mark_completed(task_id: str,
                         payload: s.TaskComplete,
                         user: CurrentUser = Depends(require_partner),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.mark_completed(db, user, task_id, payload)


# ─── Deliverables ─────────────────────────────────────────────────────
@router.post("/{task_id}/deliverables", response_model=s.TaskOut,
             summary="Partner uploads the finished deliverable")
async def add_deliverable(task_id: str, file: UploadFile = File(...),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.add_deliverable(db, user, task_id, file)


@router.get("/{task_id}/deliverables/{file_id}",
            summary="Download a deliverable from GridFS")
async def download_deliverable(task_id: str, file_id: str,
                               user: CurrentUser = Depends(get_current_user),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    meta = await service.get_deliverable(db, user, task_id, file_id)
    filename = meta.get("original_name") or "deliverable"
    return StreamingResponse(
        storage.stream_file(db, file_id,
                            meta.get("bucket", storage.DELIVERABLES_BUCKET)),
        media_type=meta.get("mime_type") or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

