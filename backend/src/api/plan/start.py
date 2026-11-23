"""``start_plan_run``: start a project's plan run as the Plan screen's button does (``POST /projects/{pid}/plan``,
docs/graph/plan/api.md), for that route and for the Slack command ``/specflow plan``. The caller has checked that the
user is a member of the project.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from api.plan.state import NoGraphBuildError, PlanState
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import GRAPH_BUILD_SWITCHES

logger = get_logger("api.routers.plan")

PLAN_UNAVAILABLE: Final = "plan_unavailable"
GRAPH_BUILD_RUNNING: Final = "graph_build_running"
NO_GRAPH_BUILD: Final = "no_graph_build"


class PlanStartRefusedError(Exception):
    """No plan run was started: ``code`` is ``plan_unavailable``, ``graph_build_running`` or ``no_graph_build``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def start_plan_run(runtime: Any, project_id: str, user_id: uuid.UUID, *, replan: bool) -> PlanState:
    """Start the plan run (or join the one that runs); a plan is made after a build has returned, never during one."""

    runner = runtime.plans
    if runner is None:
        log_event(logger, WARNING, "api.plan.unavailable", missing=list(GRAPH_BUILD_SWITCHES))
        raise PlanStartRefusedError(PLAN_UNAVAILABLE)
    if runtime.graph_builds is not None and runtime.graph_builds.running(project_id):
        raise PlanStartRefusedError(GRAPH_BUILD_RUNNING)
    already = runner.running(project_id)
    try:
        state: PlanState = await runner.start(project_id, replan=replan)
    except NoGraphBuildError:
        raise PlanStartRefusedError(NO_GRAPH_BUILD) from None
    log_event(
        logger,
        INFO,
        "api.plan.requested",
        user_id=str(user_id),
        project_id=project_id,
        already_running=already,
        replan=replan,
    )
    return state
