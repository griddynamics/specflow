"""The caller's Linear connection (plans/LINEAR/LINEAR-PLAN.md, stream S1): status, authorize, callback, disconnect.

The web app's route handler forwards the consent's ``code`` and ``state`` here; ``state`` binds the callback to the
member who asked for the URL. No Linear text, token, code, ``state`` or name is logged or answered in an error: events
carry the user id and a code.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Response

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError
from api.linear.errors import linear_connections
from api.linear.schemas import (
    LinearAuthorizeIn,
    LinearAuthorizeOut,
    LinearCallbackIn,
    LinearCallbackOut,
    LinearConnectionOut,
    LinearWorkspaceOut,
)
from api.runtime import Runtime
from linear.connections import ConnectFailure, ConnectionView, LinearConnectError, LinearConnections
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.linear")
router = APIRouter(prefix="/integrations/linear", tags=["linear"])

_CALLBACK_ERRORS: Final[dict[ConnectFailure, tuple[int, str]]] = {
    ConnectFailure.STATE_INVALID: (400, "The sign-in to Linear expired or belongs to another session. Try again."),
    ConnectFailure.CODE_INVALID: (400, "Linear did not accept the sign-in. Try again."),
    ConnectFailure.UNREACHABLE: (502, "Linear could not be reached. Try again later."),
    ConnectFailure.AUTH_FAILED: (502, "Linear refused the request of this server."),
}


def _out(runtime: Runtime, view: ConnectionView | None) -> LinearConnectionOut:
    available = runtime.linear is not None
    if view is None:
        return LinearConnectionOut(available=available, status="none", user_name=None, workspace=None)
    workspace = LinearWorkspaceOut(
        id=view.organization_id, name=view.organization_name, url_key=view.organization_url_key
    )
    return LinearConnectionOut(available=available, status=view.status, user_name=view.user_name, workspace=workspace)


def _connections(runtime: Runtime) -> LinearConnections:
    connections: LinearConnections = linear_connections(runtime)
    return connections


@router.get("", response_model=LinearConnectionOut)
async def get_linear_connection(runtime: RuntimeDep, user: UserDep) -> LinearConnectionOut:
    view = await _connections(runtime).get(user.id) if runtime.linear is not None else None
    return _out(runtime, view)


@router.post("/authorize", response_model=LinearAuthorizeOut)
async def authorize_linear(body: LinearAuthorizeIn, runtime: RuntimeDep, user: UserDep) -> LinearAuthorizeOut:
    """Linear's consent URL; its ``state`` holds the user, ``return_to`` and a 10-minute expiry."""

    return LinearAuthorizeOut(url=_connections(runtime).authorize_url(user.id, body.return_to))


@router.post("/callback", response_model=LinearCallbackOut)
async def linear_callback(body: LinearCallbackIn, runtime: RuntimeDep, user: UserDep) -> LinearCallbackOut:
    """Check ``state``, exchange ``code`` (with the PKCE verifier), read the viewer and keep the connection."""

    connections = _connections(runtime)
    try:
        view, return_to = await connections.connect(user.id, code=body.code, state=body.state)
    except LinearConnectError as error:
        status, message = _CALLBACK_ERRORS[error.code]
        log_event(logger, INFO, "api.linear.callback.refused", user_id=str(user.id), code=error.code.value)
        raise ApiError(status, error.code.value, message) from None
    return LinearCallbackOut(connection=_out(runtime, view), return_to=return_to)


@router.delete("", status_code=204)
async def disconnect_linear(runtime: RuntimeDep, user: UserDep) -> Response:
    """Delete the caller's connection and revoke the tokens at Linear (best effort)."""

    await _connections(runtime).disconnect(user.id)
    return Response(status_code=204)
