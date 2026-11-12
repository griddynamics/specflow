"""The plan invariants over one plan state: Diff checks the target plan, the plan checks the persisted one.

Every check returns the ids that break it, sorted. A state is built from plain values, so it does not
care whether it came from planner artifacts or from Neo4j rows.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from graph.plan.dag import find_cycle
from graph.plan.names import StoryProgress

CheckName = Literal["membership", "task_area", "coverage", "dependencies", "task_state", "stories", "lost_target"]
CHECKS: Final[tuple[CheckName, ...]] = (
    "membership",
    "task_area",
    "coverage",
    "dependencies",
    "task_state",
    "stories",
    "lost_target",
)
OPEN: Final = frozenset({"pending", "in_progress", "failed", "blocked"})
CLOSED: Final = frozenset({"superseded", "retired"})
_ANCHORED: Final = frozenset({"feature", "interface", "entity"})
IN_AREA, IN_STORY, IMPLEMENTS, WAITS_FOR = "IN_AREA", "IN_STORY", "IMPLEMENTS", "WAITS_FOR"


@dataclass(frozen=True)
class TaskState:
    area_id: str
    story_id: str
    status: str
    implements: tuple[str, ...] = ()
    commit: str | None = None
    claimed: bool = False
    claim_token: str | None = None
    progress: str | None = None
    blocked_by: str | None = None
    completed_by: str | None = None


@dataclass(frozen=True)
class StoryState:
    area_id: str
    status: str
    kind: str
    anchor: str | None = None
    held: bool = False
    claim_token: str | None = None
    claim_stamped: bool = False


@dataclass(frozen=True)
class PlanState:
    """A plan as the invariants see it; ``links`` are ``(type, source id, target id)``, one per relationship
    (two relationships with the same ends, as a build merge leaves them, are two entries)."""

    plannable: frozenset[str]
    required: frozenset[str]
    areas: Mapping[str, str]
    stories: Mapping[str, StoryState]
    tasks: Mapping[str, TaskState]
    links: tuple[tuple[str, str, str], ...]
    not_buildable: frozenset[str] = frozenset()
    conventions: frozenset[str] = frozenset()


def problems(state: PlanState, resolve: Callable[[str], str] | None = None) -> dict[CheckName, tuple[str, ...]]:
    """The ids breaking each check; ``resolve`` maps a merged spec id to its survivor."""

    resolved = resolve or (lambda spec_id: spec_id)
    return {
        "membership": _membership(state),
        "task_area": tuple(
            sorted(
                task_id
                for task_id, task in state.tasks.items()
                if task.status in OPEN and state.areas.get(task.area_id) != "active"
            )
        ),
        "coverage": _coverage(state, resolved),
        "dependencies": _dependencies(state),
        "task_state": _task_state(state),
        "stories": _stories(state),
        "lost_target": tuple(
            sorted(
                task_id
                for task_id, task in state.tasks.items()
                if task.status in OPEN and any(resolved(spec_id) not in state.plannable for spec_id in task.implements)
            )
        ),
    }


def _membership(state: PlanState) -> tuple[str, ...]:
    counts: dict[str, int] = {}
    for kind, source, target in state.links:
        if kind == IN_AREA and state.areas.get(target) == "active":
            counts[source] = counts.get(source, 0) + 1
    wrong = {spec_id for spec_id in state.plannable if counts.get(spec_id, 0) != 1}
    wrong |= {source for kind, source, _ in state.links if kind == IN_AREA and source not in state.plannable}
    return tuple(sorted(wrong))


def _coverage(state: PlanState, resolve: Callable[[str], str]) -> tuple[str, ...]:
    implemented = {
        resolve(spec_id)
        for task in state.tasks.values()
        if task.status in OPEN or task.status == "completed"
        for spec_id in task.implements
    }
    covered = implemented | state.not_buildable | state.conventions
    return tuple(sorted(state.required - covered))


def _dependencies(state: PlanState) -> tuple[str, ...]:
    live = {task_id for task_id, task in state.tasks.items() if task.status not in CLOSED}
    waits = [(source, target) for kind, source, target in state.links if kind == WAITS_FOR]
    broken = {
        source
        for source, target in waits
        if source in live
        and state.tasks[source].status != "in_progress"
        and target in state.tasks
        and state.tasks[target].status in CLOSED
    }
    # Waits of claimed tasks are frozen (a plan run never touches them), so they take no part in the cycle check.
    planned = {task_id for task_id in live if state.tasks[task_id].status != "in_progress"}
    cycle = find_cycle(planned, [(source, target) for source, target in waits if source in planned and target in live])
    return tuple(sorted(broken | set(cycle)))


def _task_state(state: PlanState) -> tuple[str, ...]:
    """PD5 (claim, ``progress``, ``blocked_by``, commit and ``completed_by`` fields against the status) and PD8 (story
    claim fields against their tasks)."""

    wrong = set()
    for task_id, task in state.tasks.items():
        if task.status == "completed" and not task.commit:
            wrong.add(task_id)
        if (task.status == "in_progress") != task.claimed:
            wrong.add(task_id)
        if (
            (task.progress and task.status != "in_progress")
            or (task.blocked_by and task.status != "blocked")
            or (task.completed_by and not task.commit)
        ):
            wrong.add(task_id)
    return tuple(sorted(wrong | _story_claims(state)))


def _story_claims(state: PlanState) -> set[str]:
    """PD8: a held story has its token and time and a task carrying the token; every task carrying a held story's token
    is an ``in_progress`` task of a story held with it; an ``in_progress`` task of a held story carries its token."""

    held = {story_id: story for story_id, story in state.stories.items() if story.held}
    tokens = {story.claim_token for story in held.values() if story.claim_token}
    carried: dict[str, int] = {}
    wrong = {story_id for story_id, story in held.items() if not story.claim_token or not story.claim_stamped}
    for task_id, task in state.tasks.items():
        story = held.get(task.story_id)
        stray = task.claim_token in tokens and (
            task.status != "in_progress" or story is None or story.claim_token != task.claim_token
        )
        foreign = task.status == "in_progress" and story is not None and task.claim_token != story.claim_token
        if stray or foreign:
            wrong.add(task_id)
        elif story is not None and task.status == "in_progress":
            carried[task.story_id] = carried.get(task.story_id, 0) + 1
    return wrong | {story_id for story_id in held if story_id not in carried}


def _stories(state: PlanState) -> tuple[str, ...]:
    homes: dict[str, list[str]] = {}
    anchors: dict[str, list[str]] = {}
    for kind, source, target in state.links:
        if kind == IN_STORY:
            homes.setdefault(source, []).append(target)
        elif kind == IMPLEMENTS and source in state.stories:
            anchors.setdefault(source, []).append(target)
    wrong = {
        task_id
        for task_id, task in state.tasks.items()
        if task.status in OPEN and not _homed(state, task_id, task, homes)
    }
    claimed = {task.story_id for task in state.tasks.values() if task.status == "in_progress"}
    for story_id, story in state.stories.items():
        if story.status != "active" or story.kind not in _ANCHORED:
            continue
        # A story kept active for claimed work after its anchor was deleted has no anchor to implement.
        if story.anchor not in state.plannable and story_id in claimed:
            continue
        if anchors.get(story_id) != [story.anchor]:
            wrong.add(story_id)
    return tuple(sorted(wrong))


def _homed(state: PlanState, task_id: str, task: TaskState, homes: Mapping[str, list[str]]) -> bool:
    """The task is ``IN_STORY`` exactly its own active story of its own area."""

    story = state.stories.get(task.story_id)
    return (
        homes.get(task_id) == [task.story_id]
        and story is not None
        and story.status == "active"
        and story.area_id == task.area_id
    )


def story_progress(statuses: Iterable[str]) -> StoryProgress:
    """A story's derived progress from its tasks' statuses; closed tasks are ignored, the first row wins."""

    live = [status for status in statuses if status not in CLOSED]
    if live and all(status == "completed" for status in live):
        return "completed"
    if "blocked" in live:
        return "blocked"
    if "in_progress" in live or "completed" in live:
        return "in_progress"
    return "pending"


def ready_tasks(state: PlanState, *, applying: bool = False) -> tuple[str, ...]:
    """Pending tasks of an active area and story whose every prerequisite is completed; none while applying."""

    if applying:
        return ()
    waits: dict[str, list[str]] = {}
    for kind, source, target in state.links:
        if kind == WAITS_FOR:
            waits.setdefault(source, []).append(target)
    found = []
    for task_id, task in state.tasks.items():
        story = state.stories.get(task.story_id)
        if (
            task.status == "pending"
            and state.areas.get(task.area_id) == "active"
            and story is not None
            and story.status == "active"
            and all(
                (prerequisite := state.tasks.get(target)) is not None and prerequisite.status == "completed"
                for target in waits.get(task_id, ())
            )
        ):
            found.append(task_id)
    return tuple(sorted(found))


def failing(found: Mapping[CheckName, Collection[str]]) -> tuple[CheckName, ...]:
    """The checks with at least one problem, in check order."""

    return tuple(check for check in CHECKS if found.get(check))
