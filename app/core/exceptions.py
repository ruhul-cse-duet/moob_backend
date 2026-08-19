from typing import Any, Optional

from fastapi import HTTPException, status


class AppError(HTTPException):
    """Typed API error. Always returned as a consistent JSON envelope."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"

    def __init__(
        self,
        detail: str,
        code: str | None = None,
        *,
        errors: Optional[list[dict[str, Any]]] = None,
        headers: Optional[dict[str, str]] = None,
    ):
        super().__init__(status_code=self.status_code, detail=detail, headers=headers)
        self.code = code or self.code
        self.errors = errors or []


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class Unauthorized(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"

    def __init__(
        self,
        detail: str = "Authentication required",
        code: str | None = None,
        *,
        errors: Optional[list[dict[str, Any]]] = None,
        headers: Optional[dict[str, str]] = None,
    ):
        merged = {"WWW-Authenticate": "Bearer", **(headers or {})}
        super().__init__(detail, code=code, errors=errors, headers=merged)


class Forbidden(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class BadRequest(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"


class PaymentRequired(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "payment_required"


class ValidationFailed(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"


class TooManyRequests(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "too_many_requests"

    def __init__(
        self,
        detail: str = "Too many requests. Please slow down.",
        code: str | None = None,
        *,
        retry_after: Optional[int] = None,
        errors: Optional[list[dict[str, Any]]] = None,
        headers: Optional[dict[str, str]] = None,
    ):
        merged = dict(headers or {})
        if retry_after is not None:
            merged["Retry-After"] = str(int(retry_after))
        super().__init__(detail, code=code, errors=errors, headers=merged)
        self.retry_after = retry_after


class ServiceUnavailable(AppError):
    """A dependency we do not own is down - database, mail, Stripe, OpenAI."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
