"""The plan overlay of a trace ([trace.md](trace.md) "Plan overlay"): the graph trace's nodes with their tasks."""

from __future__ import annotations

from typing import Literal

from graph.core.trace import Direction, TraceNode, TraceOwner
from graph.plan.models import BriefQuestion, PlanModel
from graph.plan.names import StoryProgress

PlanState = Literal["completed", "in_progress", "open", "unplanned"]


class TraceTask(PlanModel):
    """An open or completed task of a node: its status, the holder while ``in_progress``, the commit once completed.
    Never a claim token."""

    task_id: str
    story_id: str
    status: str
    claimed_by: str | None = None
    commit: str | None = None


class PlanTraceNode(TraceNode):
    """A node the trace reached with the plan's state: ``plan_state`` and ``tasks`` are null and empty without a plan;
    ``findings`` counts the node's open findings (0 without a findings port)."""

    plan_state: PlanState | None = None
    tasks: tuple[TraceTask, ...] = ()
    findings: int = 0


class PlanTraceOwner(TraceOwner):
    """A context node (an owner of a seed) with the same plan fields."""

    plan_state: PlanState | None = None
    tasks: tuple[TraceTask, ...] = ()
    findings: int = 0


class TraceStory(PlanModel):
    """A story that holds tasks of the traced nodes: its derived progress and the holders of its in-progress tasks."""

    story_id: str
    title: str | None
    area_id: str
    progress: StoryProgress
    holders: tuple[str, ...] = ()


class PlanTrace(PlanModel):
    """A graph trace with the plan's state; ``summary`` counts the reached nodes per ``plan_state``. ``seeds_truncated``
    is true when a story's nodes outnumbered the 50 seeds one walk takes."""

    direction: Direction
    sources: tuple[str, ...] = ()
    context: tuple[PlanTraceOwner, ...] = ()
    nodes: tuple[PlanTraceNode, ...] = ()
    stories: tuple[TraceStory, ...] = ()
    summary: dict[str, int] | None = None
    open_questions: tuple[BriefQuestion, ...] = ()
    unknown: tuple[str, ...] = ()
    truncated: bool = False
    seeds_truncated: bool = False
