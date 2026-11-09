"""Ports of the plan.

State outside Neo4j goes through these async ports; payloads are JSON objects, so an implementation
never needs to know the plan's models. Backend failures raise ``InfrastructureUnavailableError``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Literal, Protocol

from graph.plan.models import MeaningRevisions, PlanRunRecord, PlanStepName, StepState
from graph.plan.names import PlanMode

__all__ = ["PlanAnswerKind", "PlanResultPayload", "PlanStore"]

PlanAnswerKind = Literal["describe", "project", "tasks"]
PlanResultPayload = Mapping[str, Any]


class PlanStore(Protocol):
    """Port: plan runs, per-run artifacts, reusable answers and meaning revisions."""

    async def create_run(self, record: PlanRunRecord) -> None:
        """Store a new run record; its ``run_id`` is new in the project."""
        ...

    async def get_run(self, project_id: str, run_id: str) -> PlanRunRecord | None:
        """One run record."""
        ...

    async def latest_run(
        self,
        project_id: str,
        *,
        finished_only: bool = False,
        modes: Collection[PlanMode] | None = None,
    ) -> PlanRunRecord | None:
        """The most recently started run, by ``(started_at, run_id)``."""
        ...

    async def put_progress(self, project_id: str, run_id: str, step: PlanStepName, state: StepState) -> None:
        """Set one step's state in the run's ``progress``."""
        ...

    async def finish_run(self, result: Mapping[str, Any]) -> None:
        """Store the result and set the run's ``status`` and ``failure`` from it."""
        ...

    async def get_result(self, project_id: str, run_id: str) -> PlanResultPayload | None:
        """The result ``finish_run`` stored; None before the run finished."""
        ...

    async def put_artifact(self, project_id: str, run_id: str, step: PlanStepName, payload: Mapping[str, Any]) -> None:
        """Store one step artifact, replacing an earlier one."""
        ...

    async def get_artifact(self, project_id: str, run_id: str, step: PlanStepName) -> PlanResultPayload | None:
        """One step artifact; None when the step has not stored one."""
        ...

    async def get_answer(
        self, project_id: str, kind: PlanAnswerKind, key: str, reuse_key: str, *, run_id: str | None = None
    ) -> PlanResultPayload | None:
        """A stored planner answer; None on absence, key mismatch or a run mismatch."""
        ...

    async def put_answer(
        self,
        project_id: str,
        kind: PlanAnswerKind,
        key: str,
        reuse_key: str,
        run_id: str,
        *,
        payload: Mapping[str, Any],
    ) -> None:
        """Store a planner answer written by one run, replacing an earlier one."""
        ...

    async def get_revisions(self, project_id: str) -> MeaningRevisions:
        """The meaning revisions of a project; empty when none were stored."""
        ...

    async def put_revisions(self, project_id: str, revisions: MeaningRevisions) -> None:
        """Store a project's meaning revisions, replacing the earlier file."""
        ...
