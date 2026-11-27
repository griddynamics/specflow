"""Shared parts of the Dropbox routes (docs/dropbox/codes.md): the service of the runtime and the API errors of the
Dropbox codes. Messages are fixed text; no Dropbox text, token, code, ``state``, link, path, file name or e-mail reaches
an error."""

from __future__ import annotations

from typing import Final

from api.errors import ApiError
from api.runtime import Runtime
from dropbox.codes import DropboxCode, DropboxError
from dropbox.connections import DropboxConnections

ERRORS: Final[dict[DropboxCode, tuple[int, str]]] = {
    DropboxCode.NOT_CONNECTED: (409, "Connect your Dropbox account first."),
    DropboxCode.RECONNECT_REQUIRED: (409, "Your Dropbox connection expired. Connect Dropbox again."),
    DropboxCode.STATE_INVALID: (400, "The sign-in to Dropbox expired or belongs to another session. Try again."),
    DropboxCode.CODE_INVALID: (400, "Dropbox did not accept the sign-in. Try again."),
    DropboxCode.UNREACHABLE: (502, "Dropbox could not be reached. Try again later."),
    DropboxCode.AUTH_FAILED: (502, "Dropbox refused the request of this server."),
    DropboxCode.LINK_INVALID: (
        422,
        "The link is not a Dropbox folder or file link. Paste the shared link or the address of one folder or file.",
    ),
    DropboxCode.NO_ACCESS: (
        409,
        (
            "The Dropbox account of the member who connected Dropbox cannot read this folder or file. "
            "Add it to that Dropbox first."
        ),
    ),
}


def dropbox_unavailable() -> ApiError:
    return ApiError(503, "dropbox_unavailable", "The Dropbox integration is not configured on this server.")


def dropbox_error(error: DropboxError) -> ApiError:
    """The API error of a Dropbox failure; a code without its own answer is ``dropbox_auth_failed``."""

    code = error.code if error.code in ERRORS else DropboxCode.AUTH_FAILED
    status, message = ERRORS[code]
    return ApiError(status, code.value, message)


def dropbox_connections(runtime: Runtime) -> DropboxConnections:
    """The runtime's Dropbox connections; ``503 dropbox_unavailable`` when the integration is off."""

    connections: DropboxConnections | None = runtime.dropbox
    if connections is None:
        raise dropbox_unavailable()
    return connections
