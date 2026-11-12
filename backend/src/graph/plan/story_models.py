"""Answers of the story calls and the story reads for people (api.md "Story reads for people", "Story calls").

A holder is always a user id string; the host turns it into a name. No answer carries a claim token except
``StoryClaim``, the answer to the claim itself.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field

from graph.plan.findings_port import OpenFinding
from graph.plan.models import PlanModel, StoryRef, TaskItem
from graph.plan.names import AreaKind, StoryKind, StoryState, TaskProgress, TaskStatus


class StoryTask(TaskItem):
    """A task item with what a story claim adds: ``progress`` while the task is ``in_progress`` under the claim, and
    the completion record (``commit``, ``completed_by``, ``completed_at``) of a completed one."""

    progress: TaskProgress | None = None
    commit: str | None = None
    completed_by: str | None = None
    completed_at: AwareDatetime | None = None


class StoryClaim(PlanModel):
    """A story claim: the token, the unit's story ids (the asked story first), the claimed tasks in topological
    order, when the claim began, the stored ``agent_label`` and ``branch``, and whether this call only repeated it
    (a repeat writes nothing: the label and branch are the stored ones)."""

    claim_token: str = Field(min_length=1)
    story_ids: tuple[str, ...]
    tasks: tuple[StoryTask, ...]
    claimed_at: AwareDatetime
    agent_label: str | None = None
    branch: str | None = None
    repeat: bool = False


class StoryRelease(PlanModel):
    """The task ids a release or block returned to ``pending`` or ``blocked``; ``repeat`` when nothing changed."""

    released: tuple[str, ...]
    repeat: bool = False


class StoryCompletion(PlanModel):
    """What ``complete_story`` did: the tasks completed and released, the stories now claimable and the spec ids that
    changed since the plan consumed them."""

    completed: tuple[str, ...]
    released: tuple[str, ...]
    now_available: tuple[str, ...]
    spec_changed: tuple[str, ...]
    repeat: bool = False


class AreaSummary(PlanModel):
    """One active area: its stories per state (every state listed), the holders of its held stories and its open
    tasks."""

    area_id: str
    title: str | None
    kind: AreaKind
    stories: dict[str, int]
    holders: tuple[str, ...]
    open_tasks: int


class StoryRow(PlanModel):
    """One story of a list: its area's title, its state, holder, every story it waits on (directly or through other
    stories), the stories it is in a wait cycle with, its tasks (open ones; for a completed story, its completed ones)
    and how many stories wait on it; a completed one adds ``pr_url`` and its last completed task's ``commit``,
    ``completed_by`` and ``completed_at``."""

    rank: int
    story_id: str
    area_id: str
    area_title: str | None = None
    title: str
    kind: StoryKind
    release: str | None
    state: StoryState
    claimed_by: str | None
    claimed_at: AwareDatetime | None
    agent_label: str | None
    waits_on: tuple[StoryRef, ...]
    cycle_with: tuple[str, ...] = ()
    tasks: int
    unblocks: int
    blocked_by: tuple[str, ...]
    pr_url: str | None = None
    commit: str | None = None
    completed_by: str | None = None
    completed_at: AwareDatetime | None = None


class PreviewTask(PlanModel):
    """An open task of a previewed unit."""

    task_id: str
    title: str
    status: TaskStatus
    ready: bool
    complexity: str
    progress: TaskProgress | None


class ClaimRow(PlanModel):
    """One claim of the claims board: a story claim (``kind`` ``story``, with its label, branch and claimed tasks) or
    a task claimed on its own (``kind`` ``task``, a story without a story claim). The holder is a user id; no token."""

    kind: Literal["story", "task"]
    story_id: str
    title: str
    task_id: str | None
    claimed_by: str
    claimed_at: AwareDatetime
    agent_label: str | None = None
    branch: str | None = None
    tasks: int = 1


class StoryBlocker(PlanModel):
    """A story outside the unit with unfinished work the unit waits for."""

    story_id: str
    title: str | None
    state: StoryState
    claimed_by: str | None


class StoryPreview(PlanModel):
    """What a person sees before claiming: the story, its state and claim (never the token), the unit, its open
    tasks in topological order, the stories it waits on, the stories it unblocks and the ids of the findings that
    block its tasks."""

    story_id: str
    area_id: str
    title: str
    kind: StoryKind
    summary: str | None
    release: str | None
    state: StoryState
    claimed_by: str | None
    claimed_at: AwareDatetime | None
    agent_label: str | None
    branch: str | None
    note: str | None
    unit: tuple[str, ...]
    tasks: tuple[PreviewTask, ...]
    blockers: tuple[StoryBlocker, ...]
    unblocks: tuple[StoryRef, ...]
    blocked_by: tuple[str, ...] = ()
    findings: tuple[OpenFinding, ...] = ()
