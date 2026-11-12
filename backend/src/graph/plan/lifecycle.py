"""The task lifecycle coding agents drive: claims and state changes, each one locked statement.

Every change first takes the task node's write lock, then checks its state, then writes, so two agents
never both claim or finish one task. A call that does not apply raises ``TaskStateError`` and changes
nothing; invalid arguments raise ``ConfigurationError`` before any query. Notes are stored, never logged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, get_args

from graph.core.errors import ConfigurationError
from graph.core.ontology import Ontology
from graph.plan.arguments import (
    CLAIMANT_LIMIT,
    COMMIT_PATTERN,
    NOTE_LIMIT,
    check_claimant,
    commit_text,
    note_text,
)
from graph.plan.brief import task_brief
from graph.plan.dag import topological_order
from graph.plan.errors import TaskStateError
from graph.plan.models import Claim, PlanTask, TaskBrief
from graph.plan.names import BlockReason
from graph.plan.queries import CLAIM_TASK, TRANSITION_TASK
from graph.plan.reader import LoadedPlan, PlanReader, executor_of, load_plan
from graph.plan.rules import PlanRules
from graph.plan.story_board import StoryBoard
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.plan")

__all__ = ["CLAIMANT_LIMIT", "COMMIT_PATTERN", "NOTE_LIMIT", "TaskLifecycle"]

_CLAIM_FIELDS: Final = {"claim_token": None, "claimed_by": None, "claimed_at": None, "progress": None}
_OK: Final = "ok"


class TaskLifecycle:
    """Claims and task state changes of one database; ``clock`` gives UTC times (tests inject one)."""

    def __init__(
        self,
        driver_or_executor: Any,
        ontology: Ontology,
        rules: PlanRules,
        *,
        database: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._executor = executor_of(driver_or_executor, database)
        self._reader = PlanReader(self._executor, ontology, rules, database=database)
        self._ontology = ontology
        self._rules = rules
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    async def ready_tasks(
        self,
        project_id: str,
        *,
        story_id: str | None = None,
        area_id: str | None = None,
        limit: int | None = None,
    ) -> list[PlanTask]:
        """Ready tasks ordered by id, optionally of one story or area; none while a plan persists."""

        loaded = await load_plan(self._executor, project_id)
        found = [
            loaded.plan_task(loaded.tasks[task_id])
            for task_id in sorted(loaded.ready())
            if (story_id is None or loaded.tasks[task_id].story_id == story_id)
            and (area_id is None or loaded.tasks[task_id].area_id == area_id)
        ]
        return found[:limit] if limit is not None else found

    async def claim_task(self, project_id: str, task_id: str, *, claimed_by: str) -> Claim:
        """Claim a ready task. A pending task that is not ready raises ``not_ready`` with its unfinished
        transitive prerequisites, in topological order."""

        check_claimant(claimed_by)
        token = uuid.uuid4().hex
        rows = await self._executor.write(
            CLAIM_TASK,
            project_id,
            task_id=task_id,
            claim_token=token,
            claimed_by=claimed_by,
            now=self._now(),
        )
        if not rows:
            raise TaskStateError("unknown_task")
        outcome = rows[0]["outcome"]
        if outcome != _OK:
            log_event(logger, INFO, "graph.plan.claim_refused", task_id=task_id, code=outcome)
            if outcome == "not_ready":
                loaded = await load_plan(self._executor, project_id)
                raise TaskStateError("not_ready", blockers=loaded.unfinished_prerequisites([task_id]))
            if outcome == "story_claimed":
                raise await self._story_claimed(project_id, rows[0])
            raise TaskStateError(outcome, status=rows[0]["status"])
        log_event(logger, INFO, "graph.plan.task_claimed", task_id=task_id)
        return Claim(task=await self._task(project_id, task_id), claim_token=token)

    async def claim_next(self, project_id: str, story_id: str, *, claimed_by: str) -> Claim:
        """Claim the story's first ready task in topological order; a lost race tries the next one."""

        check_claimant(claimed_by)
        loaded = await load_plan(self._executor, project_id)
        story = next((item for item in loaded.current.stories if item.story_id == story_id), None)
        if story is None:
            raise TaskStateError("unknown_story")
        if story.status != "active":
            raise TaskStateError("wrong_state", status=story.status)
        if loaded.applying:
            raise TaskStateError("plan_applying")
        if story.claimed_by is not None:
            raise TaskStateError(
                "story_claimed",
                alternatives=StoryBoard(loaded).alternatives(story_id),
                claimed_by=story.claimed_by,
                claimed_at=story.claimed_at.isoformat() if story.claimed_at else None,
            )
        pending = [task.task_id for task in loaded.story_tasks(story_id) if task.status == "pending"]
        ready = loaded.ready()
        for task_id in topological_order(pending, loaded.edges(pending)):
            if task_id not in ready:
                continue
            try:
                return await self.claim_task(project_id, task_id, claimed_by=claimed_by)
            except TaskStateError as error:
                if error.code in ("plan_applying", "story_claimed"):
                    raise
        raise TaskStateError("nothing_ready", blockers=_story_blockers(loaded, pending))

    async def complete_task(self, project_id: str, task_id: str, *, claim_token: str, commit: str) -> PlanTask:
        """``in_progress`` → ``completed`` with the commit that holds the integrated work."""

        lower = commit_text(commit)
        claimant = (await self._task(project_id, task_id)).claimed_by
        return await self._transition(
            project_id,
            task_id,
            ("in_progress",),
            {
                **_CLAIM_FIELDS,
                "status": "completed",
                "commit": lower,
                "completed_by": claimant,
                "completed_at": self._now(),
            },
            claim_token=claim_token,
        )

    async def fail_task(self, project_id: str, task_id: str, *, claim_token: str, note: str = "") -> PlanTask:
        """``in_progress`` → ``failed``."""

        return await self._transition(
            project_id,
            task_id,
            ("in_progress",),
            {**_CLAIM_FIELDS, "status": "failed", "note": note_text(note)},
            claim_token=claim_token,
        )

    async def block_task(
        self, project_id: str, task_id: str, *, claim_token: str, reason: BlockReason, note: str = ""
    ) -> PlanTask:
        """``in_progress`` → ``blocked`` with a block reason code."""

        if reason not in get_args(BlockReason):
            raise ConfigurationError("invalid_reason")
        return await self._transition(
            project_id,
            task_id,
            ("in_progress",),
            {**_CLAIM_FIELDS, "status": "blocked", "block_reason": reason, "note": note_text(note)},
            claim_token=claim_token,
        )

    async def release_task(
        self,
        project_id: str,
        task_id: str,
        *,
        claim_token: str | None = None,
        force: bool = False,
        note: str = "",
    ) -> PlanTask:
        """``in_progress`` → ``pending``: with the claim token, or ``force`` for crash recovery (a host call)."""

        if claim_token is None and not force:
            raise TaskStateError("claim_mismatch")
        return await self._transition(
            project_id,
            task_id,
            ("in_progress",),
            {**_CLAIM_FIELDS, "status": "pending", "note": note_text(note)},
            claim_token=None if force else claim_token,
        )

    async def retry_task(self, project_id: str, task_id: str) -> PlanTask:
        """``failed`` → ``pending``."""

        return await self._transition(project_id, task_id, ("failed",), {"status": "pending"})

    async def unblock_task(self, project_id: str, task_id: str) -> PlanTask:
        """``blocked`` → ``pending``; the block reason is cleared."""

        return await self._transition(
            project_id,
            task_id,
            ("blocked",),
            {"status": "pending", "block_reason": None, "blocked_by": None},
        )

    async def stale_claims(self, project_id: str, *, older_than: timedelta) -> list[PlanTask]:
        """``in_progress`` tasks claimed longer ago than ``older_than``."""

        loaded = await load_plan(self._executor, project_id)
        limit = self._clock() - older_than
        return [
            loaded.plan_task(task)
            for task in loaded.current.tasks
            if task.status == "in_progress" and task.claimed_at is not None and task.claimed_at < limit
        ]

    async def task_brief(self, project_id: str, task_id: str, *, neighbours: int = 30) -> TaskBrief:
        """What one coding agent needs for an open or completed task (no model call)."""

        loaded = await load_plan(self._executor, project_id)
        return await task_brief(self._reader, loaded, project_id, task_id, rules=self._rules, neighbours=neighbours)

    async def _transition(
        self,
        project_id: str,
        task_id: str,
        from_statuses: Sequence[str],
        properties: Mapping[str, Any],
        *,
        claim_token: str | None = None,
    ) -> PlanTask:
        rows = await self._executor.write(
            TRANSITION_TASK,
            project_id,
            task_id=task_id,
            from_statuses=list(from_statuses),
            check_token=claim_token is not None,
            claim_token=claim_token or "",
            properties=dict(properties),
            now=self._now(),
        )
        if not rows:
            raise TaskStateError("unknown_task")
        outcome = rows[0]["outcome"]
        if outcome == "story_claimed":
            raise await self._story_claimed(project_id, rows[0])
        if outcome != _OK:
            raise TaskStateError(outcome, status=rows[0]["status"])
        log_event(logger, INFO, "graph.plan.task_transition", task_id=task_id, status=rows[0]["status"])
        return await self._task(project_id, task_id)

    async def _story_claimed(self, project_id: str, row: Mapping[str, Any]) -> TaskStateError:
        """The refusal of a task call whose story is held by a story claim."""

        board = StoryBoard(await load_plan(self._executor, project_id))
        return TaskStateError(
            "story_claimed",
            alternatives=board.alternatives(row["task"]["story_id"]),
            claimed_by=row["holder"],
            claimed_at=row["held_at"],
        )

    async def _task(self, project_id: str, task_id: str) -> PlanTask:
        task = await self._reader.task(project_id, task_id)
        if task is None:
            raise TaskStateError("unknown_task")
        return task

    def _now(self) -> str:
        return self._clock().isoformat(timespec="seconds")


def _story_blockers(loaded: LoadedPlan, pending: Sequence[str]) -> tuple[str, ...]:
    own = set(pending)
    return tuple(task_id for task_id in loaded.unfinished_prerequisites(pending) if task_id not in own)
