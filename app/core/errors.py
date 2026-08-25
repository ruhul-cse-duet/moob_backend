"""Consistent API error (and small success helpers) envelopes for mobile + web."""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from bson.errors import BSONError, InvalidId
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import (
    ConnectionFailure,
    DuplicateKeyError,
    ExecutionTimeout,
    NetworkTimeout,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
    WriteError,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.exceptions import AppError

logger = logging.getLogger("app.errors")


def _debug_details_allowed() -> bool:
    """Whether an error body may carry the exception text.

    DEBUG *and* a non-production environment - both, not either. ENVIRONMENT
    defaults to "development", so keying off it alone leaks internals from any
    deployment that forgot to set it.
    """
    return bool(settings.DEBUG) and settings.ENVIRONMENT.lower() not in {
        "production", "prod", "staging",
    }


def _request_id(request: Request) -> Optional[str]:
    return getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID")


# The API answers with JSON and streams back documents somebody else uploaded,
# so it loads nothing and must be allowed to load nothing - that is what stops a
# stored HTML or SVG file executing when it is opened.
_API_CSP = "default-src 'none'; frame-ancestors 'none'"

# Swagger and ReDoc are real HTML applications: they pull their bundle from a
# CDN, run an inline configuration script and fetch the schema. Under _API_CSP
# every one of those is blocked and the page renders blank - the HTML arrives,
# nothing else does. Relaxed only as far as those pages actually need.
_DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' https://fastapi.tiangolo.com data:; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'"
)


def is_docs_path(path: str) -> bool:
    """Whether this request is for the API reference rather than the API."""
    if not settings.docs_enabled:
        return False
    return path.rstrip("/") in {"/docs", "/redoc", "/docs/oauth2-redirect"}


def baseline_headers(request_id: Optional[str] = None, *,
                     docs: bool = False) -> dict[str, str]:
    """Headers every response carries, error or not.

    Defined here rather than only in the middleware because an unhandled
    exception is answered by Starlette's ServerErrorMiddleware, which sits
    *outside* the application middleware - so a 500 would otherwise go out
    without the request id or any of the security headers.
    """
    headers: dict[str, str] = {}
    if request_id:
        headers["X-Request-ID"] = request_id
    if settings.SECURITY_HEADERS:
        headers.update({
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-site",
            "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
            "Content-Security-Policy": _DOCS_CSP if docs else _API_CSP,
        })
        if settings.is_production:
            headers["Strict-Transport-Security"] = (
                f"max-age={settings.HSTS_MAX_AGE}; includeSubDomains"
            )
    return headers

# Friendly labels for common body/query field names shown on login & forms.
_FIELD_LABELS = {
    "role": "Role",
    "email": "Email",
    "password": "Password",
    "code": "Verification code",
    "confirm_password": "Confirm password",
    "subject": "Subject",
    "message": "Message",
    "full_name": "Full name",
    "mobile": "Mobile number",
}


def error_body(
    *,
    message: str,
    code: str = "error",
    errors: Optional[Sequence[dict[str, Any]]] = None,
    status_code: int = 400,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Canonical error JSON every client can parse the same way."""
    body = {
        "success": False,
        "message": message,
        "detail": message,  # FastAPI / older clients
        "code": code,
        "errors": list(errors or []),
        "status_code": status_code,
    }
    # Present on every response via middleware; carried into the body so a user
    # can quote one id in a support ticket and we can find the log line.
    if request_id:
        body["request_id"] = request_id
    return body


def success_body(
    *,
    message: str = "OK",
    data: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    """Optional success envelope for simple action endpoints."""
    body: dict[str, Any] = {
        "success": True,
        "message": message,
        "detail": message,
    }
    if data is not None:
        body["data"] = data
    body.update(extra)
    return body


def _loc_to_field(loc: Sequence[Any]) -> str:
    parts = [str(p) for p in loc if p not in {"body", "query", "path", "header", "cookie"}]
    return ".".join(parts) if parts else "request"


def _humanize_validation_item(err: dict[str, Any]) -> dict[str, str]:
    field = _loc_to_field(err.get("loc", ()))
    label = _FIELD_LABELS.get(field.split(".")[-1], field.replace("_", " ").capitalize())
    err_type = err.get("type", "")
    raw_msg = err.get("msg") or "Invalid value"
    # Strip pydantic's "Value error, " prefix
    if raw_msg.lower().startswith("value error,"):
        raw_msg = raw_msg.split(",", 1)[1].strip()

    if err_type == "missing":
        message = f"{label} is required"
    elif err_type == "enum":
        expected = err.get("ctx", {}).get("expected")
        message = f"{label} must be one of: {expected}" if expected else f"{label} is invalid"
    elif err_type in {"string_too_short", "too_short"}:
        message = f"{label} is too short"
    elif err_type in {"string_too_long", "too_long"}:
        message = f"{label} is too long"
    elif err_type == "value_error":
        message = raw_msg
    elif "email" in err_type:
        message = "Enter a valid email address"
    else:
        message = raw_msg
    return {"field": field, "message": message}


def format_validation_errors(exc: RequestValidationError) -> tuple[str, list[dict[str, str]]]:
    errors = [_humanize_validation_item(e) for e in exc.errors()]
    if not errors:
        return "Validation failed", []
    # Prefer a single clear top-level message (first field)
    if len(errors) == 1:
        return errors[0]["message"], errors
    return "Please fix the highlighted fields", errors


def _status_code_to_default_code(status_code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        402: "payment_required",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "validation_error",
        429: "too_many_requests",
        500: "internal_error",
        502: "bad_gateway",
        503: "service_unavailable",
    }.get(status_code, "error")


def _detail_to_message(detail: Any) -> str:
    if detail is None:
        return "Request failed"
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        # Rare: already-structured FastAPI detail
        parts = []
        for item in detail:
            if isinstance(item, dict) and "msg" in item:
                parts.append(str(item["msg"]))
            else:
                parts.append(str(item))
        return "; ".join(parts) if parts else "Request failed"
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail)
    return str(detail)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    body = error_body(
        message=_detail_to_message(exc.detail),
        code=exc.code,
        errors=getattr(exc, "errors", None),
        status_code=exc.status_code,
        request_id=_request_id(request),
    )
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # AppError subclasses Starlette HTTPException — prefer AppError handler when possible
    if isinstance(exc, AppError):
        return await app_error_handler(request, exc)
    message = _detail_to_message(exc.detail)
    code = _status_code_to_default_code(exc.status_code)
    body = error_body(message=message, code=code, status_code=exc.status_code,
                      request_id=_request_id(request))
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    message, errors = format_validation_errors(exc)
    body = error_body(
        message=message,
        code="validation_error",
        errors=errors,
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        request_id=_request_id(request),
    )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body)


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    body = error_body(
        message=str(exc) or "Invalid value",
        code="bad_request",
        status_code=status.HTTP_400_BAD_REQUEST,
        request_id=_request_id(request),
    )
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=body)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    rid = _request_id(request)
    logger.exception(
        "Unhandled error on %s %s (request_id=%s)", request.method, request.url.path, rid
    )
    message = "Something went wrong. Please try again."
    errors: list[dict[str, Any]] = []
    if _debug_details_allowed():
        errors = [{"field": "exception", "message": f"{type(exc).__name__}: {exc}"}]
    body = error_body(
        message=message,
        code="internal_error",
        errors=errors,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        request_id=rid,
    )
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body,
                        headers=baseline_headers(rid))


# --------------------------------------------------------------------------- #
# Database. Everything in this API is a Mongo round-trip, so an unmapped driver
# error is the difference between "temporarily unavailable" (503, worth a retry)
# and a blanket 500 that reads to the client like our bug.
# --------------------------------------------------------------------------- #

# Mongo reports a violated unique index as a duplicate key error. Which index it
# was is in the message; turn the common ones into something a user can act on.
_DUPLICATE_FIELD_HINTS = {
    "email": "An account with this email already exists",
    "slug": "That name is already taken",
    "reference": "That reference already exists",
    "code": "That code is already in use",
    "name": "That name is already in use",
}


def _duplicate_message(exc: DuplicateKeyError) -> str:
    key_pattern = (getattr(exc, "details", None) or {}).get("keyPattern") or {}
    for field in key_pattern:
        base = str(field).split(".")[-1]
        if base in _DUPLICATE_FIELD_HINTS:
            return _DUPLICATE_FIELD_HINTS[base]
    text = str(exc)
    for field, hint in _DUPLICATE_FIELD_HINTS.items():
        if f"{field}_1" in text:
            return hint
    return "This record already exists"


async def invalid_id_handler(request: Request, exc: BSONError) -> JSONResponse:
    """A malformed ObjectId in a path or body is the caller's mistake, not a 500."""
    message = "Invalid id" if isinstance(exc, InvalidId) else "Malformed request data"
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=error_body(message=message, code="bad_request",
                           status_code=status.HTTP_400_BAD_REQUEST,
                           request_id=_request_id(request)),
    )


async def duplicate_key_handler(request: Request, exc: DuplicateKeyError) -> JSONResponse:
    logger.info("Duplicate key on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=error_body(message=_duplicate_message(exc), code="conflict",
                           status_code=status.HTTP_409_CONFLICT,
                           request_id=_request_id(request)),
    )


async def database_unavailable_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    """Cluster unreachable, election in progress, or timed out - worth retrying."""
    rid = _request_id(request)
    logger.error(
        "Database unavailable on %s %s (request_id=%s): %s: %s",
        request.method, request.url.path, rid, type(exc).__name__,
        str(exc).split(",")[0],
    )
    logger.debug("Database error detail", exc_info=True)
    errors: list[dict[str, Any]] = []
    if _debug_details_allowed():
        errors = [{"field": "exception", "message": f"{type(exc).__name__}: {exc}"}]
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=error_body(
            message="The service is temporarily unavailable. Please try again in a moment.",
            code="service_unavailable", errors=errors,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, request_id=rid,
        ),
        headers={"Retry-After": "5"},
    )


async def database_error_handler(request: Request, exc: PyMongoError) -> JSONResponse:
    """Any other driver error. Still a 500, but logged as a database fault rather
    than disappearing into the generic handler with no context."""
    rid = _request_id(request)
    logger.exception(
        "Database error on %s %s (request_id=%s)", request.method, request.url.path, rid
    )
    errors: list[dict[str, Any]] = []
    if _debug_details_allowed():
        errors = [{"field": "exception", "message": f"{type(exc).__name__}: {exc}"}]
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_body(
            message="Something went wrong while saving your data. Please try again.",
            code="database_error", errors=errors,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, request_id=rid,
        ),
    )


def register_exception_handlers(app) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)

    # Database, registered before ValueError/Exception. InvalidId subclasses
    # ValueError, so without an explicit entry every malformed ObjectId would be
    # answered by value_error_handler with the driver's own wording.
    app.add_exception_handler(InvalidId, invalid_id_handler)
    app.add_exception_handler(BSONError, invalid_id_handler)
    app.add_exception_handler(DuplicateKeyError, duplicate_key_handler)
    for unavailable in (ServerSelectionTimeoutError, ConnectionFailure,
                        NetworkTimeout, ExecutionTimeout):
        app.add_exception_handler(unavailable, database_unavailable_handler)
    for failure in (WriteError, OperationFailure, PyMongoError):
        app.add_exception_handler(failure, database_error_handler)

    app.add_exception_handler(ValueError, value_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
