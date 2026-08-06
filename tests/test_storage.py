"""GridFS storage round-trip and validation, against an in-memory Mongo."""
import pytest
from mongomock_motor import AsyncMongoMockClient, enabled_gridfs_integration

from app.services import storage


class FakeUpload:
    def __init__(self, name: str, content_type: str, data: bytes):
        self.filename, self.content_type, self._data = name, content_type, data

    async def read(self) -> bytes:
        return self._data


@pytest.fixture
def db():
    with enabled_gridfs_integration():
        yield AsyncMongoMockClient()["webimove_tenant_test"]


@pytest.mark.asyncio
async def test_upload_read_stream_delete(db):
    payload = b"%PDF-1.4 passport scan " + b"x" * 50_000
    stored = await storage.save_upload(
        db, FakeUpload("passport-scan.pdf", "application/pdf", payload),
        metadata={"document_id": "doc123", "client_id": "cli9"},
    )
    assert stored["size"] == len(payload)
    assert stored["kind"] == "PDF"
    assert stored["bucket"] == storage.DOCUMENTS_BUCKET

    assert await storage.read_bytes(db, stored["file_id"]) == payload

    chunks = [c async for c in storage.stream_file(db, stored["file_id"], chunk_size=16384)]
    assert b"".join(chunks) == payload
    assert len(chunks) > 1, "large files must stream in chunks, not one blob"

    row = await db["documents.files"].find_one({})
    assert row["metadata"]["document_id"] == "doc123"

    await storage.delete_file(db, stored["file_id"])
    assert await db["documents.files"].count_documents({}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name,ctype,data", [
    ("x.exe", "application/x-msdownload", b"MZ"),
    ("empty.pdf", "application/pdf", b""),
    ("huge.pdf", "application/pdf", b"y" * (26 * 1024 * 1024)),
])
async def test_rejects_bad_uploads(db, name, ctype, data):
    from app.core.exceptions import BadRequest
    with pytest.raises(BadRequest):
        await storage.save_upload(db, FakeUpload(name, ctype, data))


@pytest.mark.asyncio
async def test_deliverables_use_a_separate_bucket(db):
    stored = await storage.save_upload(
        db, FakeUpload("translation.docx",
                       "application/vnd.openxmlformats-officedocument."
                       "wordprocessingml.document", b"PK translated"),
        bucket_name=storage.DELIVERABLES_BUCKET,
        metadata={"task_id": "task1"},
    )
    assert stored["bucket"] == storage.DELIVERABLES_BUCKET
    assert await db["deliverables.files"].count_documents({}) == 1
    assert await db["documents.files"].count_documents({}) == 0
