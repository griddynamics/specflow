"""Shared parts of the Box routes (docs/box/codes.md): the service of the runtime and the API errors of the Box codes.
Messages are fixed text; no Box text, token, code, ``state``, link, file name or e-mail reaches an error."""

from __future__ import annotations

from typing import Final

from api.errors import ApiError
from api.runtime import Runtime
from box.codes import BoxCode, BoxError
from box.connections import BoxConnections

ERRORS: Final[dict[BoxCode, tuple[int, str]]] = {
    BoxCode.NOT_CONNECTED: (409, "Connect your Box account first."),
    BoxCode.RECONNECT_REQUIRED: (409, "Your Box connection expired. Connect Box again."),
    BoxCode.STATE_INVALID: (400, "The sign-in to Box expired or belongs to another session. Try again."),
    BoxCode.CODE_INVALID: (400, "Box did not accept the sign-in. Try again."),
    BoxCode.UNREACHABLE: (502, "Box could not be reached. Try again later."),
    BoxCode.AUTH_FAILED: (502, "Box refused the request of this server."),
    BoxCode.LINK_INVALID: (
        422,
        "The link is not a Box folder, file or shared link. Paste the address of one folder or file.",
    ),
    BoxCode.LINK_PROTECTED: (422, "This shared link has a password. Graphico cannot read it."),
    BoxCode.NO_ACCESS: (409, "The Box account of the member who connected Box cannot read this folder or file."),
}


def box_unavailable() -> ApiError:
    return ApiError(503, "box_unavailable", "The Box integration is not configured on this server.")


def box_error(error: BoxError) -> ApiError:
    """The API error of a Box failure; a code without its own answer is ``box_auth_failed``."""

    code = error.code if error.code in ERRORS else BoxCode.AUTH_FAILED
    status, message = ERRORS[code]
    return ApiError(status, code.value, message)


def box_connections(runtime: Runtime) -> BoxConnections:
    """The runtime's Box connections; ``503 box_unavailable`` when the integration is off."""

    connections: BoxConnections | None = runtime.box
    if connections is None:
        raise box_unavailable()
    return connections
