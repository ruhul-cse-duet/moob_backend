"""
File storage — MongoDB GridFS, inside the tenant's own database.

Files never touch local disk. Because every organization has its own database,
a tenant's documents are stored, backed up and deleted with that tenant as one
unit; there is nothing to orphan.

GridFS (not a binary field) because BSON documents cap at 16 MB and uploads are
allowed up to MAX_UPLOAD_MB. GridFS chunks the payload across fs.chunks.
"""
from typing import Any, AsyncIterator, Dict, Optional

from bson import ObjectId
from fastapi import UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase, AsyncIOMotorGridFSBucket

from app.core.config import settings
from app.core.exceptions import BadRequest, NotFound

ALLOWED = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}

DOCUMENTS_BUCKET = "documents"
DELIVERABLES_BUCKET = "deliverables"


def bucket(db: AsyncIOMotorDatabase, name: str = DOCUMENTS_BUCKET) -> AsyncIOMotorGridFSBucket:
    return AsyncIOMotorGridFSBucket(db, bucket_name=name)


async def save_upload(
    db: AsyncIOMotorDatabase,
    file: UploadFile,
    *,
    bucket_name: str = DOCUMENTS_BUCKET,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Streams the upload into GridFS. Returns the file metadata to embed on the record."""
    if file.content_type not in ALLOWED:
        raise BadRequest(f"Unsupported file type: {file.content_type}")

    data = await file.read()
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if not data:
        raise BadRequest("The uploaded file is empty")
    if len(data) > max_bytes:
        raise BadRequest(f"File is larger than {settings.MAX_UPLOAD_MB} MB")

    ext = ALLOWED[file.content_type]
    kind = "PDF" if ext == "pdf" else "Image" if ext in {"jpg", "png", "webp"} else "Document"

    file_id = await bucket(db, bucket_name).upload_from_stream(
        file.filename or f"upload.{ext}",
        data,
        metadata={
            "content_type": file.content_type,
            "extension": ext,
            "kind": kind,
            **(metadata or {}),
        },
    )
    return {
        "file_id": str(file_id),
        "bucket": bucket_name,
        "original_name": file.filename,
        "size": len(data),
        "kind": kind,
        "mime_type": file.content_type,
    }


async def read_bytes(db: AsyncIOMotorDatabase, file_id: str,
                     bucket_name: str = DOCUMENTS_BUCKET) -> bytes:
    """Whole-file read. Used by the AI analysis pass, which needs the full payload."""
    try:
        stream = await bucket(db, bucket_name).open_download_stream(ObjectId(file_id))
    except Exception as exc:  # noqa: BLE001 - NoFile and friends
        raise NotFound("Stored file not found") from exc
    return await stream.read()


async def stream_file(db: AsyncIOMotorDatabase, file_id: str,
                      bucket_name: str = DOCUMENTS_BUCKET,
                      chunk_size: int = 256 * 1024) -> AsyncIterator[bytes]:
    """Chunked read for downloads, so a 25 MB file never sits in memory twice."""
    try:
        stream = await bucket(db, bucket_name).open_download_stream(ObjectId(file_id))
    except Exception as exc:  # noqa: BLE001
        raise NotFound("Stored file not found") from exc
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break
        yield chunk


async def delete_file(db: AsyncIOMotorDatabase, file_id: str,
                      bucket_name: str = DOCUMENTS_BUCKET) -> None:
    try:
        await bucket(db, bucket_name).delete(ObjectId(file_id))
    except Exception:  # noqa: BLE001 - deleting an already-missing file is fine
        pass
