"""The Linear tools' routes: ``sync_to_linear`` (``POST``) and ``linear_sync_status`` (``GET``), the twins of the Jira
pair (``api.routers.agent_jira``).

Same ``AgentRoute``, auth and membership-first dependency chain as ``api.routers.agent``; any member may call both.
``user_id`` goes only to the runner's log. The status report is the runner's status plus one page of its items; the
cursor holds the sort key of the last item (kind order, node id), so a page after a change still continues.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter

from agent_contracts import DEFAULT_LIMIT, Limit, LinearSyncReport, LinearSyncStatus
from agent_contracts.linear_models import LinearSyncItem
from api.deps import AgentProjectDep, AgentUserDep, RuntimeDep
from api.errors import ApiError, invalid_cursor
from api.linear.errors import linear_unavailable
from api.linear_plan.runner import LinearPlanClosedError
from api.linear_plan.sync import NOT_CONFIGURED
from api.routers.agent import AgentRoute, cursor_within_limit, decode_feature_cursor, encode_feature_cursor
from api.routers.linear_plan import plan_runner
from db.models import LINEAR_ISSUE_KINDS

router = APIRouter(prefix="/agent", tags=["agent"], route_class=AgentRoute)

_KIND_ORDER: Final = {kind: index for index, kind in enumerate(LINEAR_ISSUE_KINDS)}


def linear_not_configured() -> ApiError:
    return ApiError(409, NOT_CONFIGURED, "The project has no enabled Linear target.")


def _key(item: LinearSyncItem) -> tuple[int, str]:
    return _KIND_ORDER[item.kind], item.node_id


def _cursor_key(cursor: str) -> tuple[int, str]:
    """The sort key a cursor holds (``<kind order>:<node id>``), else ``invalid_cursor``."""

    order, _, node_id = decode_feature_cursor(cursor).partition(":")
    if not order.isdecimal() or int(order) not in _KIND_ORDER.values() or not node_id:
        raise invalid_cursor()
    return int(order), node_id


@router.post("/linear-sync", response_model=LinearSyncStatus)
async def start_linear_sync(project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep) -> LinearSyncStatus:
    """Queue a pass now (``queued`` when it merges into one); no enabled target is 409 ``linear_not_configured``;
    a runner that is closing is 503 ``linear_unavailable``."""

    try:
        status = await plan_runner(runtime).start(str(project.id), "mcp", user_id=str(user.id))
    except LinearPlanClosedError:
        raise linear_unavailable() from None
    if status.error_code == NOT_CONFIGURED:
        raise linear_not_configured()
    return status


@router.get("/linear-sync-status", response_model=LinearSyncReport)
async def read_linear_sync_status(
    project: AgentProjectDep,
    runtime: RuntimeDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> LinearSyncReport:
    """The last run and one page of the map items; a disabled target is shown, no target is 409."""

    runner = plan_runner(runtime)
    after = _cursor_key(cursor_within_limit(cursor)) if cursor is not None else None
    status = await runner.status(str(project.id))
    if status is None:
        raise linear_not_configured()
    items = await runner.items(str(project.id))
    remaining = items if after is None else [item for item in items if _key(item) > after]
    page = remaining[:limit]
    last = page[-1] if page else None
    more = last is not None and len(remaining) > len(page)
    next_cursor = encode_feature_cursor("{}:{}".format(*_key(last))) if more and last is not None else None
    return LinearSyncReport(**status.model_dump(), items=page, next_cursor=next_cursor)
