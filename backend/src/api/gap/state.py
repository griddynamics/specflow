"""What the gap routes and the build and plan runners need from the gap runner (``api.gap.runner.GapRunner``).

The routes type against ``GapRunner``-shaped protocols here, as the plan routes type against ``api.plan.state``;
the build runner calls ``after_build`` and the plan runner ``wait_idle`` through ``GapHooks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from graph.gap import GapRunResult, GapService, NoiseReport

Status = Literal["idle", "queued", "running", "completed", "degraded", "failed"]


@dataclass(frozen=True)
class GapRunState:
    """What ``GET .../gap/runs/latest`` answers: ids, step names, counts and codes only.

    ``step`` is the step that runs (or the last one that ran); ``steps_done`` of ``steps_total`` are done; while a
    step reports items, ``step_items_done`` of ``step_items_total``. ``queued`` counts the runs waiting behind it.
    ``result`` is the stored result of the latest finished run.
    """

    status: Status
    run_id: str | None = None
    mode: str | None = None
    build_id: str | None = None
    step: str | None = None
    steps_done: int = 0
    steps_total: int = 0
    step_items_done: int | None = None
    step_items_total: int | None = None
    queued: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    result: GapRunResult | None = None


class GapHooks(Protocol):
    """The calls the build runner (after a returned build) and the plan runner (before a plan run) make."""

    async def after_build(self, project_id: str, build_id: str) -> None: ...

    async def wait_idle(self, project_id: str) -> None: ...


class GapRunnerReads(Protocol):
    """The calls of ``api.gap.runner.GapRunner`` the routes make."""

    async def start_full(self, project_id: str) -> GapRunState: ...

    async def status(self, project_id: str) -> GapRunState: ...

    async def noise(self, project_id: str, run_id: str) -> NoiseReport | None: ...

    async def service(self) -> GapService: ...

    async def start_site(self, project_id: str, gap_id: str) -> None: ...

    def schedule_apply(self, project_id: str, at: datetime) -> None: ...

    def applied(self, project_id: str) -> None: ...
