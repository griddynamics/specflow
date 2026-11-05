"""What the plan routes need from a plan runner, without loading the graph library.

``api.plan.runner`` imports the graph library and the graph store; the HTTP layer only needs the state
shape and the runner calls, so the routes type against this module (as ``api.graph_build.state``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from api.schemas import PlanContent, PlanPreview

Status = Literal["idle", "running", "succeeded", "partially_succeeded", "failed"]


class NoGraphBuildError(Exception):
    """The project has no finished graph build to plan from."""


@dataclass(frozen=True)
class PlanState:
    """What ``GET .../plan`` answers: ids, step names, counts and codes only."""

    status: Status
    has_plan: bool = False
    run_id: str | None = None
    mode: str | None = None
    replan: bool | None = None
    step: str | None = None
    steps_done: int = 0
    steps_total: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    unchanged: bool | None = None
    error_code: str | None = None


class PlanRunner(Protocol):
    """The calls of ``api.plan.runner.GraphPlanRunner`` the routes make."""

    def running(self, project_id: str) -> bool: ...

    async def start(self, project_id: str, *, replan: bool = False) -> PlanState: ...

    async def status(self, project_id: str) -> PlanState: ...

    async def content(self, project_id: str) -> PlanContent | None: ...

    async def preview(self, project_id: str) -> PlanPreview: ...
