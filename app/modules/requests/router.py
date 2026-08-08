from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_client,
    require_consultant,
)
from app.core.enums import RequestStatus
from app.modules.requests import schemas as s
from app.modules.requests import service
from app.schemas.common import Message, PageParams

router = APIRouter(prefix="/requests", tags=["Immigration Requests"],
                   dependencies=[Depends(require_active_tenant)])


@router.get("/client/dashboard", response_model=s.ClientDashboardSummary, summary="Get Client Home Dashboard overview")
async def client_dashboard(user: CurrentUser = Depends(require_client),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_client_dashboard(db, user)


@router.get("/consultant/dashboard", response_model=s.ConsultantDashboardSummary, summary="Get Consultant Home Dashboard overview")
async def consultant_dashboard(user: CurrentUser = Depends(require_consultant),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_consultant_dashboard(db, user)


@router.get("/client/categories", summary="Get list of available immigration request types")
async def client_categories():
    return await service.get_client_categories()


@router.post("", status_code=status.HTTP_201_CREATED, summary="Client submits a request")
async def create_request(payload: s.RequestCreate,
                         user: CurrentUser = Depends(require_client),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.create_request(db, user, payload)


@router.get("", summary="Request queue (tabs: new / waiting / received / review / completed)")
async def list_requests(status_filter: Optional[RequestStatus] = Query(None, alias="status"),
                        search: Optional[str] = Query(None),
                        consultant_id: Optional[str] = Query(
                            None, description="Only this consultant's requests"),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_requests(db, user, params, status_filter, search,
                                       consultant_id)


@router.get("/counts", response_model=s.RequestCounts, summary="Badge counts per tab")
async def request_counts(user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.counts(db, user)


@router.get("/{request_id}", response_model=s.RequestOut, summary="Request details with client profile and documents")
async def get_request(request_id: str,
                      user: CurrentUser = Depends(get_current_user),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_request(db, user, request_id)


@router.patch("/{request_id}")
async def update_request(request_id: str, payload: s.RequestUpdate,
                         user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.update_request(db, user, request_id, payload)


@router.post("/{request_id}/documents/request", response_model=Message,
             summary="Consultant requests the documents this client needs")
async def request_documents(request_id: str, payload: s.RequestDocumentsRequest,
                            user: CurrentUser = Depends(require_consultant),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    result = await service.request_documents(db, user, request_id, payload)
    return {"detail": result["detail"]}


@router.get("/{request_id}/documents/suggest",
            summary="AI suggestion for the required document checklist")
async def suggest_documents(request_id: str,
                            user: CurrentUser = Depends(require_consultant),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return {"documents": await service.suggest_documents(db, request_id)}


@router.put("/{request_id}/review-notes", response_model=Message,
            summary="Private working notes for this consultation")
async def save_notes(request_id: str, payload: s.ReviewNotes,
                     user: CurrentUser = Depends(require_consultant),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.save_review_notes(db, user, request_id, payload.notes)


@router.post("/{request_id}/complete", status_code=status.HTTP_201_CREATED,
             summary="Complete consultation and open the immigration case")
async def complete_consultation(request_id: str,
                                case_type: Optional[str] = Body(None, embed=True),
                                deadline: Optional[datetime] = Body(None, embed=True),
                                user: CurrentUser = Depends(require_consultant),
                                db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.complete_consultation(db, user, request_id, case_type, deadline)
