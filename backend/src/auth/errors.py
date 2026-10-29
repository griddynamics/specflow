"""The one auth failure type; the API layer maps it onto its error envelope.

Messages are fixed strings chosen here, never built from the token, its
header or its claims, so an ``AuthError`` is always safe to log or return.
"""

from __future__ import annotations

from typing import Final, Literal, override

INVALID_CREDENTIAL_CODE: Final = "invalid_token"
INVALID_TOKEN: Final = INVALID_CREDENTIAL_CODE
EMAIL_NOT_VERIFIED: Final = "email_not_verified"
EMAIL_INVALID: Final = "email_invalid"
DOMAIN_NOT_ALLOWED: Final = "domain_not_allowed"

UNAUTHORIZED: Final = 401
WWW_AUTHENTICATE_BEARER: Final = "Bearer"


class AuthError(Exception):
    """A rejected request: ``status`` 401 (token validity) or 403 (email checks).

    ``code`` is the public error code, ``message`` a fixed public sentence and
    ``reason`` a fixed internal reason for logs (for example ``expired``).
    """

    def __init__(
        self,
        status: Literal[401, 403],
        code: str,
        message: str,
        *,
        reason: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.reason = reason

    @property
    def headers(self) -> dict[str, str]:
        """Response headers: ``WWW-Authenticate: Bearer`` on every 401."""

        if self.status == UNAUTHORIZED:
            return {"WWW-Authenticate": WWW_AUTHENTICATE_BEARER}
        return {}

    @override
    def __repr__(self) -> str:
        return f"AuthError(status={self.status}, code={self.code!r}, reason={self.reason!r})"


def invalid_token(reason: str) -> AuthError:
    """A 401 ``invalid_token`` with the generic public message."""

    return AuthError(401, INVALID_CREDENTIAL_CODE, "The access token is invalid.", reason=reason)


def forbidden(code: str, message: str) -> AuthError:
    """A 403 for a valid token whose email fails the policy; reason = code."""

    return AuthError(403, code, message, reason=code)
