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
    # iPhones photograph in HEIC; the picker offers it, so refusing it here
    # would fail the upload only after the person had chosen the file.
    "image/heic": "heic",

    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}

DOCUMENTS_BUCKET = "documents"
DELIVERABLES_BUCKET = "deliverables"
AVATARS_BUCKET = "avatars"

READ_CHUNK = 256 * 1024

# Profile pictures are held to a stricter list than case documents. A PDF or a
# Word file is a perfectly good document and a nonsense avatar, and every place
# that renders one puts it straight into an <img>.
IMAGE_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
}

# An avatar is displayed at a few hundred pixels. MAX_UPLOAD_MB (25) is sized
# for a scanned passport; letting that through here would store 25 MB to render
# a 96 px circle, on every request, out of the database.
MAX_AVATAR_MB = 5


def safe_filename(name: Optional[str], fallback: str = "document") -> str:
    """A filename fit for a Content-Disposition header.

    The value arrives from the uploader, so it can carry quotes, CR/LF or path
    separators. Unescaped, a crafted name breaks out of the quoted header value
    and injects headers of its own.
    """
    cleaned = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() and ch not in '"\\')
    cleaned = cleaned.replace("\r", "").replace("\n", "").strip(". ")
    return cleaned[:180] or fallback


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

    # Read in chunks and stop at the limit. `await file.read()` with no argument
    # buffers the whole body first, so an oversized upload costs us the memory
    # before we get to reject it - a 2 GB POST would take the worker down.
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise BadRequest(f"File is larger than {settings.MAX_UPLOAD_MB} MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise BadRequest("The uploaded file is empty")

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


async def save_avatar(
    db: AsyncIOMotorDatabase,
    file: UploadFile,
    *,
    owner_id: str,
    replaces: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Store a profile picture, replacing the previous one.

    ``replaces`` is the file id currently on the record. Deleting it after the
    new blob is written - never before - means a failed upload leaves the old
    picture intact instead of a profile with none. Skipping it entirely would
    leave a dead blob in the bucket on every single avatar change.
    """
    if file.content_type not in IMAGE_TYPES:
        raise BadRequest(
            "A profile picture must be a JPEG, PNG, WebP or HEIC image"
        )

    max_bytes = MAX_AVATAR_MB * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise BadRequest(f"A profile picture must be smaller than {MAX_AVATAR_MB} MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise BadRequest("The uploaded image is empty")

    ext = IMAGE_TYPES[file.content_type]
    file_id = await bucket(db, AVATARS_BUCKET).upload_from_stream(
        safe_filename(file.filename, f"avatar.{ext}"),
        data,
        metadata={
            "content_type": file.content_type,
            "extension": ext,
            "owner_id": owner_id,
            **(metadata or {}),
        },
    )

    if replaces and str(replaces) != str(file_id):
        await delete_file(db, replaces, AVATARS_BUCKET)

    return {
        "file_id": str(file_id),
        "bucket": AVATARS_BUCKET,
        "original_name": file.filename,
        "size": len(data),
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
