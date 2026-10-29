"""Graph build of a project: ``POST`` starts it in the background, ``GET`` reads its status.

The build runs in this process (``api.graph_build.runner``); the contract matches the other
long-running endpoints (``202`` at once, then poll), so it can move to Temporal unchanged.
Handlers never touch Neo4j: only the background task does.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from fastapi import APIRouter

from api.deps import RuntimeDep, UserDep
from api.errors import ApiError
from api.graph_build.state import BuildRunner, BuildState
from api.routers.documents import require_project
from api.runtime import Runtime
from api.schemas import GraphBuildStatus
from db import sources as sources_repo
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.routers.graph_build")
router = APIRouter(prefix="/projects/{pid}/graph_build", tags=["graph"])


def graph_build_unavailable() -> ApiError:
    return ApiError(503, "graph_build_unavailable", "The graph build is not configured.")


def no_processed_documents() -> ApiError:
    return ApiError(409, "no_processed_documents", "The project has no processed documents.")


def _runner(runtime: Runtime) -> BuildRunner:
    if runtime.graph_builds is None:
        raise graph_build_unavailable()
    runner: BuildRunner = runtime.graph_builds
    return runner


def _out(state: BuildState) -> GraphBuildStatus:
    fields: dict[str, Any] = dataclasses.asdict(state)
    return GraphBuildStatus(**fields)


@router.post("", status_code=202, response_model=GraphBuildStatus)
async def start_graph_build(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> GraphBuildStatus:
    await require_project(runtime, user.id, pid)
    runner = _runner(runtime)
    async with runtime.transaction() as session:
        processed = await sources_repo.count_processed_documents(session, pid)
    if processed == 0:
        raise no_processed_documents()
    project_id = str(pid)
    already = runner.running(project_id)
    state = await runner.start(project_id)
    log_event(
        logger,
        INFO,
        "api.graph_build.requested",
        user_id=str(user.id),
        project_id=project_id,
        already_running=already,
        documents=processed,
    )
    return _out(state)


@router.get("", response_model=GraphBuildStatus)
async def graph_build_status(pid: uuid.UUID, runtime: RuntimeDep, user: UserDep) -> GraphBuildStatus:
    await require_project(runtime, user.id, pid)
    return _out(await _runner(runtime).status(str(pid)))
