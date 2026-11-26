"""The API errors of the Linear codes (plans/LINEAR/LINEAR-PLAN.md): fixed text, no Linear text, token, code, ``state``
or user name ever reaches an error. Shared by every Linear route; the codes are listed in ``archkit.toml``."""

from __future__ import annotations

from typing import Any, Final

from api.errors import ApiError
from api.runtime import Runtime
from linear.errors import LinearCode, LinearError, LinearInvalidError, LinearRateLimitedError

_ERRORS: Final[dict[LinearCode, tuple[int, str, str]]] = {
    LinearCode.UNAUTHORIZED: (
        409,
        "linear_reconnect_required",
        "Your Linear connection expired. Connect Linear again.",
    ),
    LinearCode.FORBIDDEN: (409, "linear_forbidden", "Your Linear account is not allowed to do this in Linear."),
    LinearCode.NOT_FOUND: (
        409,
        "linear_not_found",
        "The Linear object does not exist or is not visible to the member.",
    ),
    LinearCode.RATE_LIMITED: (429, "linear_rate_limited", "Linear is limiting requests. Try again in a moment."),
    LinearCode.INVALID: (422, "linear_invalid", "Linear did not accept the input."),
    LinearCode.UNAVAILABLE: (502, "linear_unreachable", "Linear could not be reached. Try again later."),
}


def linear_unavailable() -> ApiError:
    return ApiError(503, "linear_unavailable", "The Linear integration is not configured on this server.")


def linear_not_connected() -> ApiError:
    return ApiError(409, "linear_not_connected", "Connect your Linear account first.")


def linear_error(error: LinearError) -> ApiError:
    """The API error of a Linear failure; a code without its own answer (``already_exists``) is ``linear_invalid``."""

    status, code, message = _ERRORS.get(error.code, _ERRORS[LinearCode.INVALID])
    headers: dict[str, str] = {}
    if isinstance(error, LinearRateLimitedError) and error.retry_after is not None:
        headers["Retry-After"] = str(max(1, round(error.retry_after)))
    details: dict[str, Any] = {}
    if isinstance(error, LinearInvalidError) and error.field:
        details["field"] = error.field
    return ApiError(status, code, message, details=details, headers=headers)


def linear_connections(runtime: Runtime) -> Any:
    """The runtime's Linear connections (stream S1); ``503 linear_unavailable`` when the integration is off."""

    connections = runtime.linear
    if connections is None:
        raise linear_unavailable()
    return connections
