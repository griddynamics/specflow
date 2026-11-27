"""The caller's Notion connection (docs/notion/oauth.md "Routes"). Stream C1 owns this file.

The web app's route handler forwards Notion's ``code`` and ``state`` (callback) here; ``state`` binds it to the user who
asked for the URL. Events carry the user id and a code only.
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from api.deps import RuntimeDep, UserDep
from api.notion import notion_connections, notion_error
from api.runtime import Runtime
from api.schemas import NotionCallbackIn, NotionCallbackOut, NotionConnectionOut, NotionReturnToIn, NotionUrlOut
from logger.logger import INFO, get_logger, log_event
from notion.connections import ConnectionView
from notion.reasons import NotionError

logger = get_logger("api.routers.notion")
router = APIRouter(prefix="/integrations/notion", tags=["notion"])


def _out(runtime: Runtime, view: ConnectionView | None) -> NotionConnectionOut:
    return NotionConnectionOut(
        enabled=runtime.notion is not None,
        connected=view is not None,
        status=view.status if view is not None else None,
        workspace_name=view.workspace_name if view is not None else None,
        connected_at=view.connected_at if view is not None else None,
        last_used_at=view.last_used_at if view is not None else None,
    )


def _refused(user_id: object, error: NotionError, step: str) -> NotionError:
    log_event(logger, INFO, "api.notion.refused", user_id=str(user_id), step=step, code=error.code.value)
    return error


@router.get("", response_model=NotionConnectionOut)
async def get_notion_connection(runtime: RuntimeDep, user: UserDep) -> NotionConnectionOut:
    view = await runtime.notion.get(user.id) if runtime.notion is not None else None
    return _out(runtime, view)


@router.post("/authorize", response_model=NotionUrlOut)
async def authorize_notion(body: NotionReturnToIn, runtime: RuntimeDep, user: UserDep) -> NotionUrlOut:
    """Notion's consent page of the connection; its ``state`` holds the user, ``return_to`` and a 10-minute expiry."""

    return NotionUrlOut(url=notion_connections(runtime).authorize_url(user.id, body.return_to))


@router.post("/callback", response_model=NotionCallbackOut)
async def notion_callback(body: NotionCallbackIn, runtime: RuntimeDep, user: UserDep) -> NotionCallbackOut:
    """Check ``state``, exchange ``code`` and keep the connection."""

    connections = notion_connections(runtime)
    try:
        view, return_to = await connections.connect(user.id, code=body.code, state=body.state)
    except NotionError as error:
        raise notion_error(_refused(user.id, error, "callback")) from None
    return NotionCallbackOut(connection=_out(runtime, view), return_to=return_to)


@router.delete("", status_code=204)
async def disconnect_notion(runtime: RuntimeDep, user: UserDep) -> Response:
    """Delete the caller's connection and revoke the grant on Notion (best effort)."""

    await notion_connections(runtime).disconnect(user.id)
    return Response(status_code=204)
