from typing import Optional

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
    require_consultant_or_partner,
)
from app.core.enums import DocumentStatus, Role
from app.core.exceptions import Forbidden, NotFound
from app.modules.documents import schemas as s
from app.modules.documents import service
from app.schemas.common import PageParams
from app.services import storage

router = APIRouter(prefix="/documents", tags=["Documents"],
                   dependencies=[Depends(require_active_tenant)])


@router.get("", summary="Document centre")
async def list_documents(request_id: Optional[str] = Query(None),
                         case_id: Optional[str] = Query(None),
                         status_filter: Optional[DocumentStatus] = Query(None, alias="status"),
                         params: PageParams = Depends(page_params),
                         user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_documents(db, user, params, request_id, case_id, status_filter)


@router.get("/{document_id}", response_model=s.DocumentOut)
async def get_document(document_id: str,
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.get_document(db, user, document_id)


@router.post("/{document_id}/upload", response_model=s.DocumentOut,
             summary="Upload a requested document (runs AI analysis)")
async def upload_document(document_id: str, file: UploadFile = File(...),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.upload(db, user, document_id, file)


#: What may be rendered in the browser rather than saved. Deliberately a short
#: allowlist: an uploaded .svg or .html served inline would run its own script
#: on this API's origin, and "it is only a document" is exactly the assumption
#: that makes that work. Anything else downloads, which is inert.
INLINE_SAFE_TYPES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp",
    "image/heic", "image/heif", "application/pdf", "text/plain",
}


@router.get("/{document_id}/file",
            summary="Stream the stored file out of GridFS")
async def download(document_id: str,
                   disposition: str = Query(
                       "attachment", pattern="^(attachment|inline)$",
                       description="`inline` to preview in a viewer, "
                                   "`attachment` to save the file"),
                   user: CurrentUser = Depends(get_current_user),
                   db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """The same bytes, offered two ways.

    A consultant deciding Approve or Reject needs to *look* at the document, and
    an unconditional `attachment` forces a download for that - a passport scan
    ends up in the Downloads folder instead of on screen. `inline` is what an
    `<img>` or a PDF viewer needs; the default stays `attachment` so nothing
    that already calls this changes behaviour.
    """
    doc = await service.get_document(db, user, document_id)
    if not doc.get("file"):
        raise NotFound("No file uploaded")
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This document is not yours")

    meta = doc["file"]
    media_type = meta.get("mime_type") or "application/octet-stream"
    if disposition == "inline" and media_type not in INLINE_SAFE_TYPES:
        disposition = "attachment"

    filename = storage.safe_filename(meta.get("original_name"))
    headers = {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        # The stored type is what the uploader's client claimed. Without this a
        # browser is free to sniff past it and treat the bytes as something
        # more dangerous than what the allowlist above approved.
        "X-Content-Type-Options": "nosniff",
    }
    # An empty Content-Length is an invalid header, not an absent one.
    if isinstance(meta.get("size"), int):
        headers["Content-Length"] = str(meta["size"])
    return StreamingResponse(
        storage.stream_file(db, meta["file_id"],
                            meta.get("bucket", storage.DOCUMENTS_BUCKET)),
        media_type=media_type,
        headers=headers,
    )


@router.delete("/{document_id}", summary="Withdraw a document request (consultant)")
async def delete_document(document_id: str,
                          user: CurrentUser = Depends(require_consultant),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.delete_document(db, user, document_id)


@router.post("/{document_id}/approve", response_model=s.DocumentOut)
async def approve(document_id: str,
                  user: CurrentUser = Depends(require_consultant_or_partner),
                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.approve(db, user, document_id)


@router.post("/{document_id}/reject", response_model=s.DocumentOut,
             summary="Return to the client for re-upload with feedback")
async def reject(document_id: str, payload: s.RejectPayload,
                 user: CurrentUser = Depends(require_consultant_or_partner),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.reject(db, user, document_id, payload.feedback)


@router.post("/{document_id}/comment", response_model=s.DocumentOut)
async def comment(document_id: str, payload: s.CommentPayload,
                  user: CurrentUser = Depends(get_current_user),
                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.comment(db, user, document_id, payload.comment)


@router.post("/{document_id}/reanalyze", summary="Re-run the AI analysis")
async def reanalyze(document_id: str,
                    user: CurrentUser = Depends(require_consultant_or_partner),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.reanalyze(db, user, document_id)
