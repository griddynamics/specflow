"""The caller's Box connection (docs/box/oauth.md "Routes", docs/mission-control/integrations-api.md).

The web app's route handler forwards Box's ``code`` and ``state`` (callback) here; ``state`` binds it to the user who
asked for the URL. Events carry the user id and a code only.
"""

from __future__ import annotations

from fastapi import APIRouter

from api.box import box_connections, box_error
from api.deps import RuntimeDep, UserDep
from api.runtime import Runtime
from api.schemas import BoxCallbackIn, BoxCallbackOut, BoxConnectionOut, BoxReturnToIn, BoxUrlOut
from box.codes import BoxError
from box.connections import ConnectionView
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.box")
router = APIRouter(prefix="/integrations/box", tags=["box"])


def _out(runtime: Runtime, view: ConnectionView | None) -> BoxConnectionOut:
    return BoxConnectionOut(
        enabled=runtime.box is not None,
        connected=view is not None,
        status=view.status if view is not None else None,
        name=view.name if view is not None else None,
        enterprise_name=view.enterprise_name if view is not None else None,
        connected_at=view.connected_at if view is not None else None,
        last_used_at=view.last_used_at if view is not None else None,
    )


def _refused(user_id: object, error: BoxError, step: str) -> BoxError:
    log_event(logger, INFO, "api.box.refused", user_id=str(user_id), step=step, code=error.code.value)
    return error


@router.get("", response_model=BoxConnectionOut)
async def get_box_connection(runtime: RuntimeDep, user: UserDep) -> BoxConnectionOut:
    view = await runtime.box.get(user.id) if runtime.box is not None else None
    return _out(runtime, view)


@router.post("/authorize", response_model=BoxUrlOut)
async def authorize_box(body: BoxReturnToIn, runtime: RuntimeDep, user: UserDep) -> BoxUrlOut:
    """Box's sign-in page of the app; its ``state`` holds the user, ``return_to`` and a 10-minute expiry."""

    return BoxUrlOut(url=box_connections(runtime).authorize_url(user.id, body.return_to))


@router.post("/callback", response_model=BoxCallbackOut)
async def box_callback(body: BoxCallbackIn, runtime: RuntimeDep, user: UserDep) -> BoxCallbackOut:
    """Check ``state``, exchange ``code``, read the account and keep the connection."""

    connections = box_connections(runtime)
    try:
        view, return_to = await connections.connect(user.id, code=body.code, state=body.state)
    except BoxError as error:
        raise box_error(_refused(user.id, error, "callback")) from None
    return BoxCallbackOut(connection=_out(runtime, view), return_to=return_to)
