"""Implementation plan of a project: ``POST`` starts a plan run in the background, ``GET`` reads its state.

The run happens in this process (``api.plan.runner``); the contract matches the graph build (``202`` at once,
then poll), so it can move to Temporal unchanged. ``/plan/tasks`` reads the persisted plan and ``/plan/preview``
what a run would plan, before it runs.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from fastapi import APIRouter

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError
from api.plan.state import NoGraphBuildError, PlanRunner, PlanState
from api.routers.documents import require_project
from api.runtime import Runtime
from api.schemas import PlanContent, PlanPreview, PlanStartRequest, PlanStatus
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import GRAPH_BUILD_SWITCHES

logger = get_logger("api.routers.plan")
router = APIRouter(prefix="/projects/{pid}/plan", tags=["plan"])


def plan_unavailable() -> ApiError:
    return ApiError(503, "plan_unavailable", "The plan is not configured.")


def no_graph_build() -> ApiError:
    return ApiError(409, "no_graph_build", "The project has no finished graph build.")


def graph_build_running() -> ApiError:
    return ApiError(409, "graph_build_running", "The graph of the project is being built.")


def no_plan() -> ApiError:
    return ApiError(404, "no_plan", "The project has no plan.")


def _runner(runtime: Runtime) -> PlanRunner:
    if runtime.plans is None:
        log_event(logger, WARNING, "api.plan.unavailable", missing=list(GRAPH_BUILD_SWITCHES))
        raise plan_unavailable()
    runner: PlanRunner = runtime.plans
    return runner


def _out(state: PlanState) -> PlanStatus:
    fields: dict[str, Any] = dataclasses.asdict(state)
    return PlanStatus(**fields)


@router.post("", status_code=202, response_model=PlanStatus)
async def start_plan(
    pid: uuid.UUID,
    runtime: RuntimeDep,
    user: UserDep,
    body: PlanStartRequest | None = None,
) -> PlanStatus:
    await require_project(runtime, user.id, pid)
    runner = _runner(runtime)
    project_id = str(pid)
    # A plan is made after a build has returned, never during one (docs/graph/plan/api.md).
    if runtime.graph_builds is not None and runtime.graph_builds.running(project_id):
        raise graph_build_running()
    replan = body.replan if body is not None else False
    already = runner.running(project_id)
    try:
        state = await runner.start(project_id, replan=replan)
    except NoGraphBuildError:
        raise no_graph_build() from None
    log_event(
        logger,
        INFO,
        "api.plan.requested",
        user_id=str(user.id),
        project_id=project_id,
        already_running=already,
        replan=replan,
    )
    return _out(state)


@router.get("", response_model=PlanStatus)
async def plan_status(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> PlanStatus:
    await require_project(runtime, user.id, pid)
    return _out(await _runner(runtime).status(str(pid)))


@router.get("/tasks", response_model=PlanContent)
async def plan_tasks(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> PlanContent:
    await require_project(runtime, user.id, pid)
    content = await _runner(runtime).content(str(pid))
    if content is None:
        raise no_plan()
    return content


@router.get("/preview", response_model=PlanPreview)
async def plan_preview(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> PlanPreview:
    await require_project(runtime, user.id, pid)
    return await _runner(runtime).preview(str(pid))
