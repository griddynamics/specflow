"""The gaps of a project for people (docs/graph/gap/api.md "Web routes"): the read routes and the gap runs.

``GET /gaps`` and ``GET /gaps/{gap_id}`` are ``GapService.list_gaps`` and ``get_gap``. ``POST /gap/runs`` queues a
``full`` gap run in this process (``api.gap.runner``; ``202`` at once, then poll ``GET /gap/runs/latest``, as the plan),
and ``GET /gap/runs/{run_id}/noise`` reads a finished run's noise report. Project membership is the access rule: a
non-member gets ``404 project_not_found``, as on every project route. ``503 gap_unavailable`` while gap runs are not
configured (no graph build settings, or ``GAP_ENABLED`` off). Logs carry ids, counts and codes only (NFR-GSEC-02).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Annotated, Final

from fastapi import APIRouter, Path, Query

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError, invalid_arguments, invalid_cursor
from api.gap.state import GapRunnerReads, GapRunState
from api.gap_models import MAX_ID, GapListQuery, GapRunStatus
from api.plan.state import NoGraphBuildError
from api.routers.documents import require_project
from api.routers.plan import no_graph_build
from api.runtime import Runtime
from graph.gap import AnswerInvalidError, GapNotFoundError, GapPage, GapView, NoiseReport
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.routers.gaps")
router = APIRouter(prefix="/projects/{pid}", tags=["gaps"])

RUN_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
CURSOR: Final = "cursor"


def gap_unavailable() -> ApiError:
    return ApiError(503, "gap_unavailable", "Gap runs are not configured.")


def gap_not_found() -> ApiError:
    return ApiError(404, "gap_not_found", "The gap does not exist.")


def gap_run_not_found() -> ApiError:
    return ApiError(404, "gap_run_not_found", "The gap run does not exist.")


def _runner(runtime: Runtime) -> GapRunnerReads:
    if runtime.gaps is None:
        log_event(logger, WARNING, "api.gap.unavailable")
        raise gap_unavailable()
    runner: GapRunnerReads = runtime.gaps
    return runner


def _out(state: GapRunState) -> GapRunStatus:
    return GapRunStatus(**{item.name: getattr(state, item.name) for item in dataclasses.fields(state)})


def _refused(error: AnswerInvalidError) -> ApiError:
    return invalid_cursor() if error.field == CURSOR else invalid_arguments(error.field)


@router.get("/gaps", response_model=GapPage)
async def list_gaps(
    pid: uuid.UUID, runtime: RuntimeDep, user: UserDep, query: Annotated[GapListQuery, Query()]
) -> GapPage:
    """The project's gaps: open, answered and applying ones unless ``status`` says otherwise; blockers first, then
    newest first. ``about`` keeps gaps about the spec ids (aliases resolve), ``story_id`` those about the nodes the
    story's tasks implement, ``document`` those citing or about the document lineage. ``held_back=false`` is the
    inbox's default view: it leaves out the new questions a run held back beyond its limit until the next run."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    try:
        return await service.list_gaps(str(pid), **query.model_dump())
    except AnswerInvalidError as error:
        raise _refused(error) from None


@router.get("/gaps/{gap_id}", response_model=GapView)
async def get_gap(
    pid: uuid.UUID,
    gap_id: Annotated[str, Path(min_length=1, max_length=MAX_ID)],
    runtime: RuntimeDep,
    user: UserDep,
) -> GapView:
    """One gap with its options, history and reach."""

    await require_project(runtime, user.id, pid)
    service = await _runner(runtime).service()
    try:
        return await service.get_gap(str(pid), gap_id)
    except GapNotFoundError:
        raise gap_not_found() from None


@router.post("/gap/runs", status_code=202, response_model=GapRunStatus)
async def start_gap_run(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> GapRunStatus:
    """Queue a full gap run on the latest finished build (a run already queued or running is joined)."""

    await require_project(runtime, user.id, pid)
    runner = _runner(runtime)
    project_id = str(pid)
    try:
        state = await runner.start_full(project_id)
    except NoGraphBuildError:
        raise no_graph_build() from None
    log_event(
        logger,
        INFO,
        "api.gap.requested",
        user_id=str(user.id),
        project_id=project_id,
        mode="full",
        status=state.status,
        queued=state.queued,
    )
    return _out(state)


@router.get("/gap/runs/latest", response_model=GapRunStatus)
async def latest_gap_run(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> GapRunStatus:
    """The project's running or latest gap run, with the latest finished run's result."""

    await require_project(runtime, user.id, pid)
    return _out(await _runner(runtime).status(str(pid)))


@router.get("/gap/runs/{run_id}/noise", response_model=NoiseReport)
async def gap_run_noise(
    pid: uuid.UUID,
    run_id: Annotated[str, Path(pattern=RUN_ID_PATTERN)],
    runtime: RuntimeDep,
    user: UserDep,
) -> NoiseReport:
    """The sections a finished run judged tracking or mixed, and the nodes they removed from structural seeds."""

    await require_project(runtime, user.id, pid)
    report = await _runner(runtime).noise(str(pid), run_id)
    if report is None:
        raise gap_run_not_found()
    return report
