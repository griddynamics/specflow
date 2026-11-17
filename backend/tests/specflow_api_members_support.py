"""A scripted plan for the removal and leave tests (no tests in here): it lists claims, records forced releases and
can fail the listing or a release, so the claims-release outcome is checked without a graph.

``order`` collects what happened (``claims``, ``release:<story>``) so a test can place the membership delete before
the first graph call.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Literal, cast

from api.agent_reads import AgentReads
from graph.plan import ClaimRow
from graph.plan.errors import PlanReadError


def claim(story_id: str, holder: Any, *, kind: Literal["story", "task"] = "story") -> ClaimRow:
    return ClaimRow(
        kind=kind,
        story_id=story_id,
        title=f"Title of {story_id}",
        task_id=f"TASK::{story_id}" if kind == "task" else None,
        claimed_by=str(holder),
        claimed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class ScriptedPlan:
    def __init__(
        self,
        claims: list[ClaimRow] | None = None,
        *,
        no_plan: bool = False,
        list_error: Exception | None = None,
        fail_on: set[str] | None = None,
        on_call: Callable[[str], None] | None = None,
    ) -> None:
        self.rows = claims or []
        self.no_plan = no_plan
        self.list_error = list_error
        self.fail_on = fail_on or set()
        self.on_call = on_call or (lambda _name: None)
        self.order: list[str] = []
        self.released: list[tuple[str, str, bool]] = []

    async def claims(self, _project_id: str) -> tuple[ClaimRow, ...]:
        self.order.append("claims")
        self.on_call("claims")
        if self.no_plan:
            raise PlanReadError("no_plan")
        if self.list_error is not None:
            raise self.list_error
        return tuple(self.rows)

    async def release_story(self, project_id: str, story_id: str, *, force: bool = False, **_: Any) -> Any:
        self.order.append(f"release:{story_id}")
        self.on_call(f"release:{story_id}")
        if story_id in self.fail_on:
            raise RuntimeError("graph down: secret@example.com")
        self.released.append((project_id, story_id, force))
        self.rows = [row for row in self.rows if row.story_id != story_id]
        return SimpleNamespace(released=("t",))


def install(runtime: Any, plan: ScriptedPlan) -> ScriptedPlan:
    runtime.agent_reads = AgentReads(ontology=cast(Any, None), reader=cast(Any, None), plan=cast(Any, plan))
    return plan
