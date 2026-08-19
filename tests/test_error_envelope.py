"""
Every failure leaves the API in the same shape.

Clients (three mobile apps and the website) branch on `code` and show `message`.
A handler that slips through to FastAPI's default returns `{"detail": ...}` with
none of that, so the app renders a blank error. These tests pin the envelope for
the failure modes that are not raised by our own code: the database driver's.
"""
import pytest
from bson.errors import InvalidId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from app.core.errors import _duplicate_message, register_exception_handlers
from app.core.exceptions import NotFound, TooManyRequests


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom/{kind}")
    async def boom(kind: str):
        raise {
            "invalid-id": lambda: InvalidId("'abc' is not a valid ObjectId"),
            "duplicate": lambda: DuplicateKeyError(
                "E11000 duplicate key error collection: p.user_directory index: email_1",
                11000, {"keyPattern": {"email": 1}},
            ),
            "unreachable": lambda: ServerSelectionTimeoutError("no replica set members"),
            "connection": lambda: ConnectionFailure("connection closed"),
            "operation": lambda: OperationFailure("not authorized on db"),
            "app-error": lambda: NotFound("Case not found"),
            "throttled": lambda: TooManyRequests("Slow down", retry_after=90),
            "unknown": lambda: RuntimeError("something we never anticipated"),
        }[kind]()

    return TestClient(app, raise_server_exceptions=False)


def envelope(response):
    body = response.json()
    # The five keys every client reads, on every error, whatever raised it.
    for key in ("success", "message", "detail", "code", "errors", "status_code"):
        assert key in body, f"{key} missing from {body}"
    assert body["success"] is False
    assert body["status_code"] == response.status_code
    return body


@pytest.mark.parametrize("kind,status,code", [
    ("invalid-id", 400, "bad_request"),
    ("duplicate", 409, "conflict"),
    ("unreachable", 503, "service_unavailable"),
    ("connection", 503, "service_unavailable"),
    ("operation", 500, "database_error"),
    ("app-error", 404, "not_found"),
    ("throttled", 429, "too_many_requests"),
    ("unknown", 500, "internal_error"),
])
def test_driver_errors_keep_the_envelope(client, kind, status, code):
    response = client.get(f"/boom/{kind}")
    assert response.status_code == status
    assert envelope(response)["code"] == code


def test_unreachable_database_asks_the_client_to_retry(client):
    """503 + Retry-After, not 500 - the caller should try again, not give up."""
    response = client.get("/boom/unreachable")
    assert response.headers["Retry-After"] == "5"


def test_throttled_response_carries_retry_after(client):
    assert client.get("/boom/throttled").headers["Retry-After"] == "90"


def test_internal_errors_never_name_the_exception(client):
    """A 500 must not hand the caller our stack detail in a real deployment."""
    from app.core.config import settings

    original = settings.DEBUG
    settings.DEBUG = False
    try:
        body = envelope(client.get("/boom/unknown"))
        assert body["errors"] == []
        assert "RuntimeError" not in body["message"]
    finally:
        settings.DEBUG = original


def test_duplicate_key_is_translated_for_a_human():
    """The raw driver message names an index; a user needs the field."""
    exc = DuplicateKeyError("E11000 ... index: email_1 dup key", 11000,
                            {"keyPattern": {"email": 1}})
    assert _duplicate_message(exc) == "An account with this email already exists"

    # No keyPattern (older servers): fall back to reading the index name.
    exc = DuplicateKeyError("E11000 ... index: slug_1 dup key", 11000, None)
    assert _duplicate_message(exc) == "That name is already taken"

    # Unrecognised index still yields something a user can read.
    exc = DuplicateKeyError("E11000 ... index: whatever_1 dup key", 11000, None)
    assert _duplicate_message(exc) == "This record already exists"
