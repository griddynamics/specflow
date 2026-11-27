"""The caller's Dropbox connection (docs/dropbox/oauth.md "Routes", docs/mission-control/integrations-api.md).

The web app's route handler forwards Dropbox's ``code`` and ``state`` (callback) here; ``state`` binds it to the user
who asked for the URL. Events carry the user id and a code only.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from api.deps import RuntimeDep, UserDep
from api.dropbox import dropbox_connections, dropbox_error
from api.runtime import Runtime
from api.schemas import DropboxCallbackIn, DropboxCallbackOut, DropboxConnectionOut, DropboxReturnToIn, DropboxUrlOut
from dropbox.codes import DropboxError
from dropbox.connections import ConnectionView
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.dropbox")
router = APIRouter(prefix="/integrations/dropbox", tags=["dropbox"])


def _out(runtime: Runtime, view: ConnectionView | None) -> DropboxConnectionOut:
    return DropboxConnectionOut(
        enabled=runtime.dropbox is not None,
        connected=view is not None,
        status=view.status if view is not None else None,
        name=view.name if view is not None else None,
        team_name=view.team_name if view is not None else None,
        connected_at=view.connected_at if view is not None else None,
        last_used_at=view.last_used_at if view is not None else None,
    )


def _refused(user_id: object, error: DropboxError, step: str) -> DropboxError:
    log_event(logger, INFO, "api.dropbox.refused", user_id=str(user_id), step=step, code=error.code.value)
    return error


@router.get("", response_model=DropboxConnectionOut)
async def get_dropbox_connection(runtime: RuntimeDep, user: UserDep) -> DropboxConnectionOut:
    view = await runtime.dropbox.get(user.id) if runtime.dropbox is not None else None
    return _out(runtime, view)


@router.post("/authorize", response_model=DropboxUrlOut)
async def authorize_dropbox(body: DropboxReturnToIn, runtime: RuntimeDep, user: UserDep) -> DropboxUrlOut:
    """Dropbox's sign-in page of the app; its ``state`` holds the user, ``return_to`` and a 10-minute expiry."""

    return DropboxUrlOut(url=dropbox_connections(runtime).authorize_url(user.id, body.return_to))


@router.post("/callback", response_model=DropboxCallbackOut)
async def dropbox_callback(body: DropboxCallbackIn, runtime: RuntimeDep, user: UserDep) -> DropboxCallbackOut:
    """Check ``state``, exchange ``code``, read the account and keep the connection."""

    connections = dropbox_connections(runtime)
    try:
        view, return_to = await connections.connect(user.id, code=body.code, state=body.state)
    except DropboxError as error:
        raise dropbox_error(_refused(user.id, error, "callback")) from None
    return DropboxCallbackOut(connection=_out(runtime, view), return_to=return_to)


@router.delete("", status_code=204)
async def disconnect_dropbox(runtime: RuntimeDep, user: UserDep) -> Response:
    """Delete the caller's connection and revoke the app's grant at Dropbox (best effort)."""

    await dropbox_connections(runtime).disconnect(user.id)
    return Response(status_code=204)
