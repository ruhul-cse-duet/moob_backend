"""What a consultant needs before pressing Approve or Reject.

Two things were missing from the review screen, and both are here.

The upload endpoint has always been labelled "runs AI analysis" and never ran
one - `ai_analysis` stayed empty until somebody thought to press Re-analyse, so
the consultant reviewed with nothing in front of them.

And the file itself was only ever offered as `attachment`, which is a download.
A passport scan that lands in the Downloads folder is not a document that was
looked at.
"""
import asyncio

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.core.enums import DocumentStatus, Role
from app.modules.documents import router as documents_router
from app.modules.documents import service

DOC_ID = ObjectId()


class _User:
    id = "client-1"
    role = Role.CLIENT
    raw = {"full_name": "Ayesha Rahman"}


class _Upload:
    filename = "passport.pdf"


@pytest.fixture
def db(monkeypatch):
    database = AsyncMongoMockClient()["webimove_tenant_test"]

    async def _save_upload(_db, _file, **kwargs):
        return {"file_id": "gridfs-1", "original_name": "passport.pdf",
                "mime_type": "application/pdf", "size": 1024,
                "bucket": "documents"}

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(service.storage, "save_upload", _save_upload)
    monkeypatch.setattr(service.storage, "delete_file", _noop)
    monkeypatch.setattr(service, "notify", _noop)
    monkeypatch.setattr(service, "log_activity", _noop)
    monkeypatch.setattr(service, "resolve_consultant_id",
                        lambda *a, **k: _returns("consultant-1"))
    return database


async def _returns(value):
    return value


@pytest.fixture
async def document(db):
    await db.documents.insert_one({
        "_id": DOC_ID, "name": "Passport", "category": "identity",
        "status": DocumentStatus.UPLOAD_NEEDED.value,
        "client_id": "client-1", "consultant_id": "consultant-1",
    })
    return db


async def test_an_upload_is_read_before_a_consultant_opens_it(document, monkeypatch):
    seen = {}

    async def _analyze(*, file_bytes, mime_type, document_name, context):
        seen["name"] = document_name
        return {"confidence": 91, "recommendation": "approve",
                "summary": "Valid passport, expires 2031.",
                "extracted_fields": {"expiry_date": "2031-04-02"}, "issues": []}

    monkeypatch.setattr(service, "analyze_document", _analyze)
    monkeypatch.setattr(service.storage, "read_bytes",
                        lambda *a, **k: _returns(b"%PDF-1.4"))

    result = await service.upload(document, _User(), str(DOC_ID), _Upload())

    # The upload answers immediately, without waiting for the model.
    assert result["status"] == DocumentStatus.WITH_CONSULTANT.value
    assert result["ai_analysis"]["status"] == "analysing"

    await asyncio.sleep(0)  # let the background task run
    stored = await document.documents.find_one({"_id": DOC_ID})
    assert seen["name"] == "Passport"
    assert stored["ai_analysis"]["recommendation"] == "approve"
    assert stored["ai_analysis"]["confidence"] == 91


async def test_a_model_that_is_down_does_not_fail_the_upload(document, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("Anthropic is unreachable")

    monkeypatch.setattr(service, "analyze_document", _boom)
    monkeypatch.setattr(service.storage, "read_bytes",
                        lambda *a, **k: _returns(b"%PDF-1.4"))

    result = await service.upload(document, _User(), str(DOC_ID), _Upload())
    assert result["status"] == DocumentStatus.WITH_CONSULTANT.value

    await asyncio.sleep(0)
    stored = await document.documents.find_one({"_id": DOC_ID})
    # The file is stored and reviewable; only the automatic read is missing, and
    # it says so rather than leaving the consultant with a stale verdict.
    assert stored["file"]["file_id"] == "gridfs-1"
    assert stored["ai_analysis"]["status"] == "failed"
    assert stored["ai_analysis"]["recommendation"] == "manual_review"


class TestInlinePreview:
    def test_a_scan_and_a_pdf_can_be_shown_on_screen(self):
        assert "application/pdf" in documents_router.INLINE_SAFE_TYPES
        assert "image/jpeg" in documents_router.INLINE_SAFE_TYPES
        assert "image/webp" in documents_router.INLINE_SAFE_TYPES

    def test_anything_that_could_run_a_script_is_not_on_the_list(self):
        # Served inline, either of these executes on the API's own origin.
        assert "image/svg+xml" not in documents_router.INLINE_SAFE_TYPES
        assert "text/html" not in documents_router.INLINE_SAFE_TYPES
