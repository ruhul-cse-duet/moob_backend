from fastapi import HTTPException, status


class AppError(HTTPException):
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, detail: str, code: str | None = None):
        super().__init__(status_code=self.status_code, detail=detail)
        self.code = code


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND


class Conflict(AppError):
    status_code = status.HTTP_409_CONFLICT


class Unauthorized(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED


class Forbidden(AppError):
    status_code = status.HTTP_403_FORBIDDEN


class BadRequest(AppError):
    status_code = status.HTTP_400_BAD_REQUEST


class PaymentRequired(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
