"""Consistent API error (and small success helpers) envelopes for mobile + web."""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.exceptions import AppError

logger = logging.getLogger("app.errors")

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
) -> dict[str, Any]:
    """Canonical error JSON every client can parse the same way."""
    return {
        "success": False,
        "message": message,
        "detail": message,  # FastAPI / older clients
        "code": code,
        "errors": list(errors or []),
        "status_code": status_code,
    }


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
    )
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # AppError subclasses Starlette HTTPException — prefer AppError handler when possible
    if isinstance(exc, AppError):
        return await app_error_handler(request, exc)
    message = _detail_to_message(exc.detail)
    code = _status_code_to_default_code(exc.status_code)
    body = error_body(message=message, code=code, status_code=exc.status_code)
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
    )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=body)


async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    body = error_body(
        message=str(exc) or "Invalid value",
        code="bad_request",
        status_code=status.HTTP_400_BAD_REQUEST,
    )
    return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=body)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    message = "Something went wrong. Please try again."
    errors: list[dict[str, Any]] = []
    if settings.DEBUG or settings.ENVIRONMENT != "production":
        errors = [{"field": "exception", "message": f"{type(exc).__name__}: {exc}"}]
    body = error_body(
        message=message,
        code="internal_error",
        errors=errors,
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
    return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=body)


def register_exception_handlers(app) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ValueError, value_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
