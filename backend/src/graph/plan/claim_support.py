"""What the story claim calls share: the locked transaction they work in and the rules on the locked plan."""

from __future__ import annotations

import hmac
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.plan.errors import TaskStateError
from graph.plan.models import CurrentTask, StoryInfo
from graph.plan.queries import SET_STORIES, SET_TASKS
from graph.plan.reader import LoadedPlan
from graph.plan.snapshot import read_current_plan
from graph.plan.story_board import StoryBoard, Verdict

CLEARED_TASK: Final = {"claim_token": None, "claimed_by": None, "claimed_at": None, "progress": None}
CLEARED_STORY: Final = {"claimed_by": None, "claimed_at": None, "agent_label": None}


def same_token(stored: str | None, given: str | None) -> bool:
    """Whether two claim tokens are the same token, compared in constant time; a missing token matches nothing."""

    return stored is not None and given is not None and hmac.compare_digest(stored.encode(), given.encode())


def persisting(board: StoryBoard) -> None:
    """Refuse (``plan_applying``) while a plan persist is in progress: a persist locks a task before its story, the
    story calls lock stories before tasks, so the two never run at once. A forced release is the host's way out."""

    if board.applying:
        raise TaskStateError("plan_applying")


@dataclass(frozen=True)
class Completed:
    """What ``complete`` did, before the host-side ``spec_changed`` is added: ``implements`` are the spec ids the
    completed tasks implement."""

    completed: tuple[str, ...]
    released: tuple[str, ...]
    now_available: tuple[str, ...]
    implements: tuple[str, ...]
    repeat: bool = False


@dataclass(frozen=True)
class Work:
    """The locked transaction a call works in: its board (the plan as locked), the clock reading and the writers."""

    tx: Any
    project_id: str
    board: StoryBoard
    now: str

    @property
    def loaded(self) -> LoadedPlan:
        return self.board.loaded

    async def set_tasks(self, changes: Mapping[str, Mapping[str, Any]]) -> None:
        if changes:
            rows = [{"id": task_id, "properties": dict(values)} for task_id, values in sorted(changes.items())]
            await self.tx.run(SET_TASKS, rows=rows, now=self.now)

    async def set_stories(self, changes: Mapping[str, Mapping[str, Any]]) -> None:
        if changes:
            rows = [{"id": story_id, "properties": dict(values)} for story_id, values in sorted(changes.items())]
            await self.tx.run(SET_STORIES, rows=rows, now=self.now)

    async def reload(self) -> LoadedPlan:
        """The plan as the transaction now sees it, its own writes included."""

        return LoadedPlan(await read_current_plan(TxReader(self.tx), self.project_id))


class TxReader:
    """The ``read`` of an executor over one open transaction, so the plan reads run inside it."""

    def __init__(self, tx: Any) -> None:
        self._tx = tx

    async def read(self, query: Any, _project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = await self._tx.run(query, **params)
        return rows


def stored_story(board: StoryBoard, story_id: str) -> StoryInfo:
    story = board.stories.get(story_id)
    if story is None:
        raise TaskStateError("unknown_story")
    return story


def held_group(board: StoryBoard, story_id: str) -> tuple[str, ...]:
    """The stories that hold the same claim as ``story_id`` (itself alone when it is not held)."""

    story = board.stories.get(story_id)
    if story is None or story.claimed_by is None or story.claim_token is None:
        return (story_id,)
    return tuple(
        sorted(
            other.story_id
            for other in board.stories.values()
            if other.claimed_by and other.claim_token == story.claim_token
        )
    )


def claimed_tasks(board: StoryBoard, group: Collection[str], token: str | None) -> tuple[str, ...]:
    """The claimed tasks of a claim: the ``in_progress`` tasks of its stories carrying its token, topological order."""

    found = [
        task.task_id
        for story_id in group
        for task in board.loaded.story_tasks(story_id)
        if task.status == "in_progress" and same_token(task.claim_token, token)
    ]
    return board.ordered(found)


def claimable(board: StoryBoard) -> set[str]:
    return {story_id for story_id in board.stories if board.claimable(story_id)}


def blocked_by(tasks: Sequence[CurrentTask], finding_id: str) -> tuple[str, ...]:
    return tuple(sorted(task.task_id for task in tasks if task.status == "blocked" and task.blocked_by == finding_id))


def blocked_ids(board: StoryBoard, finding_id: str) -> tuple[str, ...]:
    return blocked_by(board.loaded.current.tasks, finding_id)


def blocked_stories(board: StoryBoard, finding_id: str) -> set[str]:
    return {board.loaded.tasks[task_id].story_id for task_id in blocked_ids(board, finding_id)}


def completion_repeat(board: StoryBoard, story: StoryInfo, token: str, completed_by: str, commit: str) -> Completed:
    """A repeated completion: the claim ended with this token and the story's tasks carrying the commit were completed
    by the caller; anything else is a mismatch."""

    done = [task for task in board.loaded.story_tasks(story.story_id) if task.commit == commit]
    if not same_token(story.claim_token, token) or not done or any(task.completed_by != completed_by for task in done):
        raise TaskStateError("claim_mismatch")
    ids = board.ordered(task.task_id for task in done)
    waiting = {
        board.loaded.tasks[source].story_id
        for task_id in ids
        for source in board.loaded.waited_by.get(task_id, ())
        if board.loaded.tasks[source].status in ("pending", "failed")
    }
    available = {other for other in waiting if other != story.story_id and board.claimable(other)}
    implements = tuple(dict.fromkeys(spec_id for task_id in ids for spec_id in board.loaded.tasks[task_id].implements))
    return Completed(ids, (), tuple(sorted(available, key=board.rank_key)), implements, repeat=True)


def refusal(board: StoryBoard, story_id: str, verdict: Verdict) -> TaskStateError:
    """The ``TaskStateError`` of a refused claim."""

    fields: dict[str, Any] = dict(verdict.fields)
    if verdict.code == "story_claimed":
        return TaskStateError(verdict.code, alternatives=board.alternatives(story_id), **fields)
    if verdict.code == "not_available":
        return TaskStateError(verdict.code, blockers=verdict.blockers)
    return TaskStateError(verdict.code, **fields)
