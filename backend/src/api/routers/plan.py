"""Implementation plan of a project: ``POST`` starts a plan run in the background, ``GET`` reads its state.

The run happens in this process (``api.plan.runner``); the contract matches the graph build (``202`` at once,
then poll), so it can move to Temporal unchanged. ``/plan/tasks`` reads the persisted plan.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Final

from fastapi import APIRouter

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError
from api.plan.start import (
    GRAPH_BUILD_RUNNING,
    NO_GRAPH_BUILD,
    PLAN_UNAVAILABLE,
    PlanStartRefusedError,
    start_plan_run,
)
from api.plan.state import PlanRunner, PlanState
from api.routers.documents import require_project
from api.runtime import Runtime
from api.schemas import PlanContent, PlanStartRequest, PlanStatus
from logger.logger import WARNING, get_logger, log_event
from settings import GRAPH_BUILD_SWITCHES

logger = get_logger("api.routers.plan")
router = APIRouter(prefix="/projects/{pid}/plan", tags=["plan"])


def plan_unavailable() -> ApiError:
    return ApiError(503, "plan_unavailable", "The plan is not configured.")


def no_graph_build() -> ApiError:
    return ApiError(409, "no_graph_build", "The project has no finished graph build.")


def graph_build_running() -> ApiError:
    return ApiError(409, "graph_build_running", "The graph of the project is being built.")


_REFUSED: Final = {
    PLAN_UNAVAILABLE: plan_unavailable,
    GRAPH_BUILD_RUNNING: graph_build_running,
    NO_GRAPH_BUILD: no_graph_build,
}


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
    replan = body.replan if body is not None else False
    try:
        state = await start_plan_run(runtime, str(pid), user.id, replan=replan)
    except PlanStartRefusedError as error:
        refused = _REFUSED[error.code]()
        raise refused from None
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
