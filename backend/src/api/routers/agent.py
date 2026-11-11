"""The agent routes: the reads behind the MCP tools (architecture.md, API agent routes).

Auth is the MCP policy (``AgentUserDep``); every route checks membership first and answers with an
``agent_contracts`` model. Errors keep the order of the former tool handlers: a plan read error by its own code, a
graph outage, an unreachable database, then a bug (the generic 500). Nothing here writes SQL or Cypher.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Annotated, Any, Final, override

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from agent_contracts import (
    DEFAULT_LIMIT,
    MAX_CURSOR_CHARS,
    FeatureItem,
    FeatureList,
    FeatureTaskList,
    GetTaskBriefArgs,
    Limit,
    ListTasksArgs,
    ProjectItem,
    ProjectList,
    TaskBriefResult,
    TaskItem,
    TaskList,
    TaskPath,
    TaskStatusFilter,
)
from api.agent_reads import FEATURE_TYPE, agent_reads
from api.deps import AgentProjectDep, AgentUserDep, RuntimeDep
from api.errors import (
    ApiError,
    feature_not_found,
    invalid_cursor,
    no_plan,
    task_not_found,
)
from api.runtime import agent_error
from auth.errors import AuthError
from db import repositories as repo
from graph.plan import PlanReadError
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.routers.agent")

# The bounds and defaults are the contract models' own (agent_contracts is the single source).
_NEIGHBOURS = GetTaskBriefArgs.model_fields["neighbours"]
NeighboursQuery = Annotated[int, *_NEIGHBOURS.metadata]
DEFAULT_NEIGHBOURS: Final[int] = _NEIGHBOURS.get_default()
DEFAULT_READY_ONLY: Final[bool] = ListTasksArgs.model_fields["ready_only"].get_default()
CURSOR_VERSION: Final = 1
_READ_ERRORS: Final[dict[str, Callable[[], ApiError]]] = {
    "no_plan": no_plan,
    "feature_not_found": feature_not_found,
    "task_not_found": task_not_found,
}
_PASSED_ON = (ApiError, AuthError, RequestValidationError, StarletteHTTPException, ClientDisconnect)


class AgentRoute(APIRoute):
    """A route whose failures, in the dependency chain or the handler alike, end in an envelope of the table."""

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except _PASSED_ON:
                raise
            except Exception as error:
                mapped = agent_error(error)
                if mapped is None:
                    raise
                log_event(logger, WARNING, "api.agent.failed", code=mapped.code, error_type=type(error).__name__)
                raise mapped from None

        return route_handler


router = APIRouter(prefix="/agent", tags=["agent"], route_class=AgentRoute)


def cursor_within_limit(cursor: str) -> str:
    """``cursor`` unchanged, or ``invalid_cursor`` when it is over ``MAX_CURSOR_CHARS``."""

    if len(cursor) > MAX_CURSOR_CHARS:
        raise invalid_cursor()
    return cursor


def encode_feature_cursor(after: str) -> str:
    """Opaque cursor: base64url without padding of ``{"v":1,"after":<spec_id>}``."""

    raw = json.dumps({"v": CURSOR_VERSION, "after": after}, separators=(",", ":"), ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")


def decode_feature_cursor(cursor: str) -> str:
    """The ``spec_id`` the cursor holds, else ``invalid_cursor``.

    Only the canonical form of ``encode_feature_cursor`` is accepted: other padding, key order, extra
    keys, another version or an empty position are all invalid.
    """

    cursor_within_limit(cursor)
    try:
        data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        after = data["after"]
        canonical = isinstance(after, str) and bool(after) and encode_feature_cursor(after) == cursor
    except (
        ValueError,
        TypeError,
        KeyError,
        binascii.Error,
    ):  # a lone surrogate fails the re-encode (UnicodeEncodeError)
        raise invalid_cursor() from None
    if not canonical:
        raise invalid_cursor()
    return str(after)


async def _read[T](call: Callable[[], Awaitable[T]]) -> T:
    """``call``'s answer; a plan read that finds nothing becomes the error of the same code."""

    try:
        return await call()
    except PlanReadError as error:
        mapped = _READ_ERRORS.get(error.code)
        if mapped is None:
            raise
        raise mapped() from None


def _cursor_for(page: Sequence[TaskItem], *, more: bool) -> str | None:
    """The cursor that continues after the last item of ``page``, when ``more`` items follow."""

    return encode_feature_cursor(page[-1].task_id) if more else None


def _page(items: Sequence[TaskItem], cursor: str | None, limit: int) -> tuple[list[TaskItem], str | None]:
    """Task path paging: the items after the item the cursor names (a cursor that names none is refused), at most
    ``limit``, and the next cursor."""

    first = 0
    if cursor is not None:
        named = decode_feature_cursor(cursor)
        position = next((index for index, item in enumerate(items) if item.task_id == named), None)
        if position is None:
            raise invalid_cursor()
        first = position + 1
    window = list(items[first : first + limit])
    return window, _cursor_for(window, more=first + len(window) < len(items))


def _after(items: Sequence[TaskItem], cursor: str | None, limit: int) -> tuple[list[TaskItem], str | None]:
    """List paging: the items whose ``task_id`` sorts after the cursor's (which need not be listed any more), at most
    ``limit``, and the next cursor."""

    floor = decode_feature_cursor(cursor) if cursor is not None else None
    remaining = items if floor is None else [item for item in items if item.task_id > floor]
    window = list(remaining[:limit])
    return window, _cursor_for(window, more=len(remaining) > len(window))


@router.get("/projects", response_model=ProjectList)
async def list_projects(
    runtime: RuntimeDep,
    user: AgentUserDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> ProjectList:
    """One page through ``db.repositories.list_projects``; its cursor is passed through unchanged."""

    checked = cursor_within_limit(cursor) if cursor is not None else None
    try:
        async with runtime.transaction() as session:
            page = await repo.list_projects(session, user.id, limit, checked)
    except repo.InvalidCursorError:
        raise invalid_cursor() from None
    items = [ProjectItem(project_id=str(p.id), name=p.name, created_at=p.created_at) for p in page.items]
    return ProjectList(items=items, next_cursor=page.next_cursor)


@router.get("/features", response_model=FeatureList)
async def list_features(
    project: AgentProjectDep,
    runtime: RuntimeDep,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> FeatureList:
    """Count and one page of ``feature`` nodes; the graph gets ``str(project.id)``, never the argument."""

    after = decode_feature_cursor(cursor) if cursor is not None else None
    reads = await agent_reads(runtime)
    feature = reads.ontology.node_types[FEATURE_TYPE]
    labels = [feature.label]
    stored = {name for name, spec in feature.attributes.items() if spec.store}
    graph_id = str(project.id)
    total = await reads.reader.count_nodes(graph_id, labels)
    page = await reads.reader.nodes_page(graph_id, labels, limit=limit, after=after)
    items = [
        FeatureItem(
            spec_id=node.spec_id,
            name=node.name,
            attributes={key: value for key, value in node.attributes.items() if key in stored},
        )
        for node in page.items
    ]
    next_cursor = encode_feature_cursor(page.next_after) if page.next_after is not None else None
    return FeatureList(project_id=graph_id, total_count=total, items=items, next_cursor=next_cursor)


@router.get("/tasks", response_model=TaskList)
async def list_tasks(
    project: AgentProjectDep,
    runtime: RuntimeDep,
    *,
    ready_only: bool = DEFAULT_READY_ONLY,
    status: TaskStatusFilter | None = None,
    area_id: str | None = None,
    story_id: str | None = None,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> TaskList:
    """Count and one page of the plan's tasks after the filters; the plan gets ``str(project.id)``."""

    graph_id = str(project.id)
    plan = (await agent_reads(runtime)).plan
    items = await _read(
        lambda: plan.list_tasks(graph_id, ready_only=ready_only, status=status, area_id=area_id, story_id=story_id)
    )
    page, next_cursor = _after(items, cursor, limit)
    return TaskList(project_id=graph_id, total_count=len(items), items=page, next_cursor=next_cursor)


@router.get("/feature-tasks", response_model=FeatureTaskList)
async def list_feature_tasks(
    project: AgentProjectDep,
    runtime: RuntimeDep,
    feature_id: str,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> FeatureTaskList:
    """Every open or completed task of one feature, ordered by ``task_id``."""

    graph_id = str(project.id)
    plan = (await agent_reads(runtime)).plan
    found = await _read(lambda: plan.feature_tasks(graph_id, feature_id))
    page, next_cursor = _after(found.items, cursor, limit)
    return FeatureTaskList(
        project_id=graph_id,
        feature=found.feature,
        total_count=len(found.items),
        items=page,
        next_cursor=next_cursor,
    )


@router.get("/task-path", response_model=TaskPath)
async def get_task_path(
    project: AgentProjectDep,
    runtime: RuntimeDep,
    task_id: str,
    limit: Limit = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> TaskPath:
    """The prerequisites of one task in build order, then the task. The cursor is the ``task_id`` of the last item."""

    graph_id = str(project.id)
    plan = (await agent_reads(runtime)).plan
    path = await _read(lambda: plan.task_path(graph_id, task_id))
    page, next_cursor = _page(path.items, cursor, limit)
    return TaskPath(
        project_id=graph_id,
        task_id=path.task_id,
        total_count=len(path.items),
        completed=path.completed,
        remaining=len(path.items) - 1 - path.completed,
        items=page,
        next_cursor=next_cursor,
    )


@router.get("/task-brief", response_model=TaskBriefResult)
async def get_task_brief(
    project: AgentProjectDep,
    runtime: RuntimeDep,
    task_id: str,
    neighbours: NeighboursQuery = DEFAULT_NEIGHBOURS,
) -> TaskBriefResult:
    """The plan library's brief of one open or completed task."""

    graph_id = str(project.id)
    plan = (await agent_reads(runtime)).plan
    brief = await _read(lambda: plan.task_brief(graph_id, task_id, neighbours=neighbours))
    return TaskBriefResult(project_id=graph_id, brief=brief)
