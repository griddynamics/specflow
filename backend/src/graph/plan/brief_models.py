"""The story brief: the task briefs of one story merged ([lifecycle.md](lifecycle.md) "Story brief")."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from graph.plan.findings_port import OpenFinding
from graph.plan.models import (
    BriefContract,
    BriefNeighbour,
    BriefNode,
    BriefPlacement,
    BriefQuestion,
    DoneWhen,
    PlanModel,
    PlanStory,
    PlanTask,
    WorkAreaInfo,
)


class StoryBriefTask(PlanModel):
    """One open or completed task of the story with its ``done_when`` items and their sources."""

    task: PlanTask
    done_when: tuple[DoneWhen, ...] = ()


class StoryBrief(PlanModel):
    """What one coding agent needs for one story: the story with its anchor, the area and project parts once, every task
    in topological order, the implemented nodes, open questions and neighbours merged by ``spec_id``, the contracts of
    completed prerequisites outside the story, and the open findings about the story's nodes (added by the host's
    findings port, blocking first)."""

    story: PlanStory
    anchor: dict[str, Any] | None = None
    area: WorkAreaInfo | None = None
    profile: dict[str, Any] = Field(default_factory=dict)
    tasks: tuple[StoryBriefTask, ...] = ()
    implements: tuple[BriefNode, ...] = ()
    contracts: tuple[BriefContract, ...] = ()
    open_questions: tuple[BriefQuestion, ...] = ()
    neighbours: tuple[BriefNeighbour, ...] = ()
    placement: tuple[BriefPlacement, ...] = ()
    spec_changed: bool = False
    missing: tuple[str, ...] = ()
    findings: tuple[OpenFinding, ...] = ()
