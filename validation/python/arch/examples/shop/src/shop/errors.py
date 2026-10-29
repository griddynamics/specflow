"""Application errors; each maps to one documented `(code, status)` envelope pair."""

from __future__ import annotations


class AppError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"
    default_message = "Internal error."

    def __init__(self, message: str | None = None) -> None:
        self.public_message = message or self.default_message
        super().__init__(self.public_message)


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"

    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__(f"{entity} {entity_id} not found")


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"


class ForbiddenError(AppError):
    status_code = 403
    code = "FORBIDDEN"
    default_message = "Forbidden."


class UnauthenticatedError(AppError):
    status_code = 401
    code = "UNAUTHENTICATED"
    default_message = "Authentication required."
