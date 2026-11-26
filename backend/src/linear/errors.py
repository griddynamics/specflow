"""The Linear failures as a closed hierarchy. ``str(error)`` is the code: no Linear text, token, header or URL is ever
inside an error (the HTTP client maps GraphQL ``extensions.type`` and HTTP statuses onto these classes)."""

from __future__ import annotations

from enum import StrEnum


class LinearCode(StrEnum):
    UNAUTHORIZED = "linear_unauthorized"  # the token is expired, revoked or malformed: reconnect
    FORBIDDEN = "linear_forbidden"  # the member's role or the app's scopes do not allow the call
    NOT_FOUND = "linear_not_found"  # no such object, or the member cannot see it
    RATE_LIMITED = "linear_rate_limited"  # 429 or a complexity refusal; ``retry_after`` says when to try again
    INVALID = "linear_invalid"  # Linear refused the input (validation); ``field`` names it when known
    ALREADY_EXISTS = "linear_already_exists"  # the client id (or a unique name) is taken: a retried create
    UNAVAILABLE = "linear_unreachable"  # a network failure, a timeout or a 5xx


class LinearError(Exception):
    """A Linear failure as one closed code; ``str(e)`` is the code."""

    code: LinearCode = LinearCode.UNAVAILABLE

    def __init__(self, *, http_status: int | None = None) -> None:
        super().__init__(self.code.value)
        self.http_status = http_status


class LinearUnauthorizedError(LinearError):
    code = LinearCode.UNAUTHORIZED


class LinearForbiddenError(LinearError):
    code = LinearCode.FORBIDDEN


class LinearNotFoundError(LinearError):
    code = LinearCode.NOT_FOUND


class LinearRateLimitedError(LinearError):
    code = LinearCode.RATE_LIMITED

    def __init__(self, retry_after: float | None = None, *, http_status: int | None = 429) -> None:
        super().__init__(http_status=http_status)
        self.retry_after = retry_after  # seconds, from ``Retry-After`` or the reset header; None = unknown


class LinearInvalidError(LinearError):
    code = LinearCode.INVALID

    def __init__(self, field: str | None = None, *, http_status: int | None = None) -> None:
        super().__init__(http_status=http_status)
        self.field = field  # the input name (``estimate``, ``title``...), never Linear's message


class LinearAlreadyExistsError(LinearError):
    code = LinearCode.ALREADY_EXISTS


class LinearUnavailableError(LinearError):
    code = LinearCode.UNAVAILABLE
