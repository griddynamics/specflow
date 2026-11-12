"""Story state over one loaded plan: claim units, claimability, the people's story states, what a story waits on
and unblocks ([lifecycle.md](lifecycle.md) "Story claims"). Pure: it reads a ``LoadedPlan`` and writes nothing, so the
claim calls (inside their locked transaction) and the people's reads (from one plan read) apply the same rules.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Final

from graph.plan.dag import strongly_connected, topological_order
from graph.plan.invariants import CLOSED, OPEN
from graph.plan.models import CurrentTask, StoryInfo
from graph.plan.names import StoryState
from graph.plan.reader import LoadedPlan

STATE_ORDER: Final[tuple[StoryState, ...]] = ("ready", "in_progress", "waiting", "blocked", "empty", "completed")
ALTERNATIVES: Final = 3


@dataclass(frozen=True)
class Verdict:
    """What a claim of one story would do now: ``ok`` or ``repeat`` (the holder claims again), or the refusal code.

    ``unit`` is the claim unit (story ids, sorted); ``tasks`` the tasks a claim takes (``ok``) or holds (``repeat``) in
    topological order; ``fields`` what the refusal names (``claimed_by``, ``claimed_at``, ``task_id``, ``status``) and
    ``blockers`` the unfinished prerequisites outside the unit."""

    code: str
    unit: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    fields: Mapping[str, str | None] = field(default_factory=dict)
    blockers: tuple[str, ...] = ()


class StoryBoard:
    """The stories of a plan with their units and states; ``applying`` is whether a plan persist is in progress."""

    def __init__(self, loaded: LoadedPlan, *, applying: bool | None = None) -> None:
        self.loaded = loaded
        self.applying = loaded.applying if applying is None else applying
        self.stories: dict[str, StoryInfo] = {story.story_id: story for story in loaded.current.stories}
        self._open: dict[str, list[CurrentTask]] = {}
        for task in loaded.current.tasks:
            if task.status in OPEN:
                self._open.setdefault(task.story_id, []).append(task)
        self._states: dict[str, StoryState] = {}
        self._outside: dict[tuple[str, ...], tuple[str, ...]] = {}

    @cached_property
    def story_edges(self) -> tuple[tuple[str, str], ...]:
        """``(waiting story, prerequisite story)`` over the open tasks, one per pair."""

        ids = [task.task_id for tasks in self._open.values() for task in tasks]
        by_id = self.loaded.tasks
        return tuple(
            sorted(
                {
                    (by_id[source].story_id, by_id[target].story_id)
                    for source, target in self.loaded.edges(ids)
                    if by_id[source].story_id != by_id[target].story_id
                }
            )
        )

    @cached_property
    def _units(self) -> dict[str, tuple[str, ...]]:
        groups = strongly_connected(self._open, self.story_edges)
        return {story_id: group for group in groups for story_id in group}

    def unit_of(self, story_id: str) -> tuple[str, ...]:
        """The story and every story it is in a cycle with in the story order, over open tasks (sorted)."""

        return self._units.get(story_id, (story_id,))

    def open_tasks(self, unit: Iterable[str]) -> list[CurrentTask]:
        """The open tasks of the stories, ordered by task id."""

        return sorted((task for story_id in unit for task in self._open.get(story_id, ())), key=_task_id)

    def ordered(self, task_ids: Iterable[str]) -> tuple[str, ...]:
        """``task_ids`` in topological order; ids on a wait cycle (a broken plan) follow, sorted."""

        ids = set(task_ids)
        order = topological_order(ids, self.loaded.edges(ids))
        return (*order, *sorted(ids - set(order)))

    def held(self, unit: Iterable[str]) -> list[StoryInfo]:
        return [
            self.stories[story_id]
            for story_id in unit
            if story_id in self.stories and self.stories[story_id].claimed_by
        ]

    def verdict(self, story_id: str, caller: str) -> Verdict:
        """What ``caller`` claiming ``story_id`` would do now (the refusals in the order the lifecycle lists)."""

        story = self.stories.get(story_id)
        if story is None:
            return Verdict("unknown_story")
        if story.status != "active":
            return Verdict("wrong_state", fields={"status": story.status})
        unit = self.unit_of(story_id)
        if self.applying:
            return Verdict("plan_applying", unit)
        held = self.held(unit)
        if held:
            return self._held(unit, held, caller)
        return self._assess(unit)

    def claimable(self, story_id: str) -> bool:
        """Whether the story's unit can be claimed now (``ready``)."""

        story = self.stories.get(story_id)
        if story is None or story.status != "active" or self.applying:
            return False
        unit = self.unit_of(story_id)
        return not self.held(unit) and self._assess(unit).code == "ok"

    def state(self, story_id: str) -> StoryState:
        """The story state; the first matching row wins: empty, completed, in_progress, blocked, ready, waiting."""

        found = self._states.get(story_id)
        if found is None:
            found = self._states[story_id] = self._state(story_id)
        return found

    def cycle_with(self, story_id: str) -> tuple[str, ...]:
        """The other stories of its claim unit: the stories it is in a wait cycle with (sorted; usually none)."""

        return tuple(other for other in self.unit_of(story_id) if other != story_id)

    def blocking_stories(self, story_id: str) -> tuple[str, ...]:
        """Every story outside the unit with unfinished work the unit waits for, directly or through other tasks, in
        task order: the story's ``waits_on``. Worked out once per unit: a list asks it for every story."""

        unit = self.unit_of(story_id)
        found = self._outside.get(unit)
        if found is None:
            found = self._outside[unit] = self._outside_stories(unit)
        return found

    def unblocks(self, story_id: str) -> tuple[str, ...]:
        """The stories that wait for it: every story whose ``blocking_stories`` names it, sorted. One definition for
        both sides, so a story's ``unblocks`` count equals the number of stories listing it in ``waits_on``."""

        return self._waiters.get(story_id, ())

    @cached_property
    def _waiters(self) -> dict[str, tuple[str, ...]]:
        found: dict[str, set[str]] = {}
        for waiter in self._open:
            for prerequisite in self.blocking_stories(waiter):
                found.setdefault(prerequisite, set()).add(waiter)
        return {story_id: tuple(sorted(waiters)) for story_id, waiters in found.items()}

    def alternatives(self, story_id: str) -> tuple[str, ...]:
        """Up to three claimable stories of the story's area, in the order of the ``open`` list."""

        story = self.stories.get(story_id)
        if story is None:
            return ()
        unit = set(self.unit_of(story_id))
        ready = [
            other.story_id
            for other in self.stories.values()
            if other.area_id == story.area_id and other.story_id not in unit and self.state(other.story_id) == "ready"
        ]
        return tuple(sorted(ready, key=self.rank_key)[:ALTERNATIVES])

    def rank_key(self, story_id: str) -> tuple[int, bool, str, int, str]:
        """The order of the ``open`` list: state (ready first), release, stories it unblocks (more first), id."""

        release = self.stories[story_id].release
        return (
            STATE_ORDER.index(self.state(story_id)),
            release is None,
            release or "",
            -len(self.unblocks(story_id)),
            story_id,
        )

    def unfinished(self, task_ids: Iterable[str]) -> tuple[str, ...]:
        """The unfinished transitive prerequisites of the tasks (a closed one included: it never completes), in
        topological order."""

        waits, tasks = self.loaded.waits, self.loaded.tasks
        seen: set[str] = set()
        frontier = list(task_ids)
        while frontier:
            for target in waits.get(frontier.pop(), ()):
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        return self.ordered(task_id for task_id in seen if tasks[task_id].status != "completed")

    def _held(self, unit: tuple[str, ...], held: list[StoryInfo], caller: str) -> Verdict:
        other = next((story for story in held if story.claimed_by != caller), None)
        if other is not None:
            return Verdict("story_claimed", unit, fields={"claimed_by": other.claimed_by, "claimed_at": _iso(other)})
        tasks = self.open_tasks(unit)
        if len(held) == len(unit) and held[0].claim_token is not None:
            token = held[0].claim_token
            return Verdict(
                "repeat", unit, self.ordered(task.task_id for task in tasks if _claimed(task, token)), fields={}
            )
        busy = next((task for task in tasks if task.status == "in_progress"), None)
        return Verdict("partly_claimed", unit, fields={"task_id": busy.task_id if busy else None, "claimed_by": caller})

    def _assess(self, unit: tuple[str, ...]) -> Verdict:
        tasks = self.open_tasks(unit)
        busy = next((task for task in tasks if task.status == "in_progress"), None)
        if busy is not None:
            return Verdict("partly_claimed", unit, fields={"task_id": busy.task_id, "claimed_by": busy.claimed_by})
        claim = [task.task_id for task in tasks if task.status in ("pending", "failed")]
        if not claim or any(task.status == "blocked" for task in tasks):
            return Verdict("not_available", unit)
        own, by_id = set(claim), self.loaded.tasks
        outside = any(
            by_id[target].status != "completed" and target not in own
            for task_id in claim
            for target in self.loaded.waits.get(task_id, ())
        )
        if outside:
            blockers = tuple(task_id for task_id in self.unfinished(claim) if task_id not in own)
            return Verdict("not_available", unit, blockers=blockers)
        return Verdict("ok", unit, self.ordered(claim))

    def _state(self, story_id: str) -> StoryState:
        tasks = self.loaded.story_tasks(story_id)
        story = self.stories[story_id]
        if all(task.status in CLOSED for task in tasks):
            return "empty"
        if self.loaded.progress(story_id) == "completed":
            return "completed"
        if story.claimed_by or any(task.status == "in_progress" for task in tasks):
            return "in_progress"
        if any(task.status == "blocked" for task in tasks):
            return "blocked"
        return "ready" if self.claimable(story_id) else "waiting"

    def _outside_stories(self, unit: tuple[str, ...]) -> tuple[str, ...]:
        own = self.open_tasks(unit)
        inside = set(unit)
        found = [
            self.loaded.tasks[task_id].story_id
            for task_id in self.unfinished(task.task_id for task in own)
            if self.loaded.tasks[task_id].story_id not in inside
        ]
        return tuple(dict.fromkeys(found))


def _claimed(task: CurrentTask, token: str) -> bool:
    return task.status == "in_progress" and task.claim_token == token


def _task_id(task: CurrentTask) -> str:
    return task.task_id


def _iso(story: StoryInfo) -> str | None:
    return story.claimed_at.isoformat() if story.claimed_at is not None else None
