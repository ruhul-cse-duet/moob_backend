from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import language as request_language
from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_client,
    require_consultant,
    require_consultant_or_partner,
)
from app.core.enums import RequestStatus
from app.modules.requests import schemas as s
from app.modules.requests import service
from app.schemas.common import Message, PageParams
from app.services import storage

router = APIRouter(prefix="/requests", tags=["Immigration Requests"],
                   dependencies=[Depends(require_active_tenant)])


@router.get("/client/dashboard", response_model=s.ClientDashboardSummary, summary="Get Client Home Dashboard overview")
async def client_dashboard(user: CurrentUser = Depends(require_client),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                           lang: str = Depends(request_language)):
    return await service.get_client_dashboard(db, user, lang)


@router.get("/consultant/dashboard", response_model=s.ConsultantDashboardSummary, summary="Get Consultant Home Dashboard overview")
async def consultant_dashboard(user: CurrentUser = Depends(require_consultant),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                               lang: str = Depends(request_language)):
    return await service.get_consultant_dashboard(db, user, lang)


@router.get("/client/categories",
            summary="Procedures this organization offers, by process area")
async def client_categories(user: CurrentUser = Depends(get_current_user),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                            lang: str = Depends(request_language)):
    """Read-only for a client: it labels their case. The consultant is the one
    who assigns a procedure."""
    return await service.get_client_categories(db, lang)


@router.post("", status_code=status.HTTP_201_CREATED,
             summary="Open a request for a client")
async def create_request(payload: s.RequestCreate,
                         user: CurrentUser = Depends(require_consultant),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """The consultant opens it, names the client, and it enters the queue as `new`.

    Clients cannot: the consultant decides which procedure a client follows, so
    there is nothing for a client to submit here. What they need is raised with
    their consultant, who opens the request.
    """
    return await service.create_request(db, user, payload)


@router.delete("/{request_id}", summary="Withdraw a request")
async def delete_request(request_id: str,
                         user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """A client may remove their own while it is still waiting or declined.

    Once a consultant has taken it on - or opened a case from it - deleting
    would orphan the checklist and files built on it, so that is refused with a
    message pointing at the consultant. A consultant may remove any request in
    their own workspace.
    """
    return await service.delete_request(db, user, request_id)


@router.post("/{request_id}/approve", summary="Take a client's request into the queue")
async def approve_request(request_id: str,
                          user: CurrentUser = Depends(require_consultant),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.approve_request(db, user, request_id)


@router.post("/{request_id}/decline",
             summary="Turn a client's request down, with a reason they can read")
async def decline_request(request_id: str, payload: s.RequestDecline,
                          user: CurrentUser = Depends(require_consultant),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.decline_request(db, user, request_id, payload.reason)


@router.get("", summary="Request queue (tabs: new / waiting / received / review / completed)")
async def list_requests(status_filter: Optional[RequestStatus] = Query(None, alias="status"),
                        search: Optional[str] = Query(None),
                        consultant_id: Optional[str] = Query(
                            None, description="Only this consultant's requests"),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                        lang: str = Depends(request_language)):
    return await service.list_requests(db, user, params, status_filter, search,
                                       consultant_id, lang)


@router.get("/counts", response_model=s.RequestCounts, summary="Badge counts per tab")
async def request_counts(user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.counts(db, user)


@router.get("/{request_id}", response_model=s.RequestOut, summary="Request details with client profile and documents")
async def get_request(request_id: str,
                      user: CurrentUser = Depends(get_current_user),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db),
                      lang: str = Depends(request_language)):
    return await service.get_request(db, user, request_id, lang)


@router.patch("/{request_id}")
async def update_request(request_id: str, payload: s.RequestUpdate,
                         user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.update_request(db, user, request_id, payload)


@router.post("/{request_id}/documents/request", response_model=Message,
             summary="Request the documents this client needs")
async def request_documents(request_id: str, payload: s.RequestDocumentsRequest,
                            user: CurrentUser = Depends(require_consultant_or_partner),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    result = await service.request_documents(db, user, request_id, payload)
    return {"detail": result["detail"]}


@router.get("/{request_id}/documents/suggest",
            summary="AI suggestion for the required document checklist")
async def suggest_documents(request_id: str,
                            user: CurrentUser = Depends(require_consultant_or_partner),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return {"documents": await service.suggest_documents(db, user, request_id)}


@router.put("/{request_id}/review-notes", response_model=Message,
            summary="Private working notes for this consultation")
async def save_notes(request_id: str, payload: s.ReviewNotes,
                     user: CurrentUser = Depends(require_consultant),
                     db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.save_review_notes(db, user, request_id, payload.notes)


@router.post("/{request_id}/attachments", status_code=status.HTTP_201_CREATED,
             summary="Attach supporting material to a request")
async def add_attachment(request_id: str, file: UploadFile = File(...),
                         user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.add_attachment(db, user, request_id, file)


@router.get("/{request_id}/attachments/{file_id}",
            summary="Stream one attachment out of GridFS")
async def download_attachment(request_id: str, file_id: str,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    meta = await service.get_attachment(db, user, request_id, file_id)
    filename = storage.safe_filename(meta.get("original_name"))
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if isinstance(meta.get("size"), int):
        headers["Content-Length"] = str(meta["size"])
    return StreamingResponse(
        storage.stream_file(db, file_id, meta.get("bucket", storage.DOCUMENTS_BUCKET)),
        media_type=meta.get("mime_type") or "application/octet-stream",
        headers=headers,
    )


@router.delete("/{request_id}/attachments/{file_id}", response_model=Message,
               summary="Remove an attachment")
async def delete_attachment(request_id: str, file_id: str,
                            user: CurrentUser = Depends(get_current_user),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await service.remove_attachment(db, user, request_id, file_id)
    return {"detail": "Attachment removed"}


@router.post("/{request_id}/open-case", status_code=status.HTTP_201_CREATED,
             summary="Open a case from this request")
async def open_case(request_id: str, payload: s.OpenCase,
                    user: CurrentUser = Depends(require_consultant_or_partner),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """The consultant takes the work on; documents are collected inside the case.

    Deliberately not gated on the document checklist. `/complete` is the other
    end of the same job - it writes an outcome and does require every requested
    document to be approved - and using that gate to *start* a case left the
    process with nowhere to go.
    """
    return await service.open_case(db, user, request_id, payload)


@router.post("/{request_id}/complete", status_code=status.HTTP_201_CREATED,
             summary="Complete consultation and open the immigration case")
async def complete_consultation(request_id: str,
                                payload: s.CompleteConsultation,
                                user: CurrentUser = Depends(require_consultant_or_partner),
                                db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.complete_consultation(db, user, request_id, payload)
