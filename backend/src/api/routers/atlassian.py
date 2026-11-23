"""The caller's Atlassian connection (docs/jira-sync/oauth.md "Connect flow", "Routes").

The web app's route handler forwards the consent's ``code`` and ``state`` here; ``state`` binds the callback to the
user who asked for the URL. No Atlassian text, token, code, ``state``, account id or name is logged or answered in
an error: events carry the user id and a code.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, Response

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError
from api.jira.connections import AtlassianConnections, ConnectionView
from api.jira.runner import JiraSyncRunner
from api.runtime import Runtime
from api.schemas import (
    AtlassianAuthorizeIn,
    AtlassianAuthorizeOut,
    AtlassianCallbackIn,
    AtlassianCallbackOut,
    AtlassianConnectionOut,
)
from jira.oauth import OAuthCode, OAuthError
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.atlassian")
router = APIRouter(prefix="/integrations/atlassian", tags=["atlassian"])

_CALLBACK_ERRORS: Final[dict[OAuthCode, tuple[int, str]]] = {
    OAuthCode.STATE_INVALID: (400, "The sign-in to Atlassian expired or belongs to another session. Try again."),
    OAuthCode.CODE_INVALID: (400, "Atlassian did not accept the sign-in. Try again."),
    OAuthCode.SITE_NOT_GRANTED: (403, "The Atlassian account was not granted access to this Jira site."),
    OAuthCode.UNREACHABLE: (502, "Atlassian could not be reached. Try again later."),
}


def atlassian_oauth_unavailable() -> ApiError:
    return ApiError(503, "atlassian_oauth_unavailable", "Atlassian connections are not configured on this server.")


def _runner(runtime: Runtime) -> JiraSyncRunner | None:
    runner: JiraSyncRunner | None = runtime.jira
    return runner


def _connections(runtime: Runtime) -> AtlassianConnections:
    runner = _runner(runtime)
    if runner is None or runner.connections is None:
        raise atlassian_oauth_unavailable()
    return runner.connections


def _out(runtime: Runtime, view: ConnectionView | None) -> AtlassianConnectionOut:
    runner = _runner(runtime)
    mode: Literal["oauth", "token"] | None = None
    if runner is not None:
        mode = "oauth" if runner.connections is not None else "token"
    return AtlassianConnectionOut(
        mode=mode,
        connected=view is not None,
        status=view.status if view is not None else None,
        account_name=view.account_name if view is not None else None,
        connected_at=view.connected_at if view is not None else None,
        last_used_at=view.last_used_at if view is not None else None,
    )


@router.get("", response_model=AtlassianConnectionOut)
async def get_atlassian_connection(runtime: RuntimeDep, user: UserDep) -> AtlassianConnectionOut:
    runner = _runner(runtime)
    view = await runner.connections.get(user.id) if runner is not None and runner.connections is not None else None
    return _out(runtime, view)


@router.post("/authorize", response_model=AtlassianAuthorizeOut)
async def authorize_atlassian(body: AtlassianAuthorizeIn, runtime: RuntimeDep, user: UserDep) -> AtlassianAuthorizeOut:
    """The Atlassian consent URL; its ``state`` holds the user, ``return_to`` and a 10-minute expiry."""

    return AtlassianAuthorizeOut(url=_connections(runtime).authorize_url(user.id, body.return_to))


@router.post("/callback", response_model=AtlassianCallbackOut)
async def atlassian_callback(body: AtlassianCallbackIn, runtime: RuntimeDep, user: UserDep) -> AtlassianCallbackOut:
    """Check ``state``, exchange ``code`` and keep the connection when the grant includes this Jira site."""

    connections = _connections(runtime)
    try:
        view, return_to = await connections.connect(user.id, code=body.code, state=body.state)
    except OAuthError as error:
        status, message = _CALLBACK_ERRORS.get(error.code, _CALLBACK_ERRORS[OAuthCode.CODE_INVALID])
        code = error.code if error.code in _CALLBACK_ERRORS else OAuthCode.CODE_INVALID
        log_event(logger, INFO, "api.atlassian.callback.refused", user_id=str(user.id), code=code.value)
        raise ApiError(status, code.value, message) from None
    return AtlassianCallbackOut(connection=_out(runtime, view), return_to=return_to)


@router.delete("", status_code=204)
async def disconnect_atlassian(runtime: RuntimeDep, user: UserDep) -> Response:
    """Delete the caller's connection; Atlassian's own grant is removed at id.atlassian.com/manage-profile/apps."""

    await _connections(runtime).disconnect(user.id)
    return Response(status_code=204)
