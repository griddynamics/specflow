"""Story claims: one locked transaction per call ([lifecycle.md](lifecycle.md) "Story claims").

Every call opens one transaction, locks the Story nodes of its claim unit in story id order, then their tasks in
task id order, reads the plan again (so it sees what any concurrent call committed), decides with ``StoryBoard`` and
writes. A call that does not apply raises ``TaskStateError`` or ``ConfigurationError`` and changes nothing. The claim
token, notes, labels and branch names are stored, never logged.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import Any, Final, TypeVar

from graph.core.store import Executor
from graph.plan.arguments import (
    check_branch,
    check_claimant,
    check_done,
    check_label,
    check_note,
    check_pr_url,
    check_printable,
    check_progress,
    check_story_reason,
    commit_text,
)
from graph.plan.claim_support import (
    CLEARED_STORY,
    CLEARED_TASK,
    Completed,
    TxReader,
    Work,
    blocked_by,
    blocked_ids,
    blocked_stories,
    claimable,
    claimed_tasks,
    completion_repeat,
    held_group,
    persisting,
    refusal,
    same_token,
    stored_story,
)
from graph.plan.errors import TaskStateError
from graph.plan.names import BlockReason, TaskProgress
from graph.plan.queries import LOCK_STORIES, LOCK_TASKS
from graph.plan.reader import LoadedPlan
from graph.plan.snapshot import read_current_plan
from graph.plan.story_board import StoryBoard, Verdict
from graph.plan.story_models import StoryClaim, StoryRelease, StoryTask
from graph.plan.task_items import story_task_items
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.plan")

ATTEMPTS: Final = 4
_T = TypeVar("_T")
FINDING_LIMIT: Final = 200


class StoryClaims:
    """The story calls of one database; ``clock`` gives UTC times (tests inject one)."""

    def __init__(self, executor: Executor, *, clock: Callable[[], datetime] | None = None) -> None:
        self._executor = executor
        self._clock = clock or (lambda: datetime.now(UTC))

    async def claim(
        self,
        project_id: str,
        story_id: str,
        *,
        claimed_by: str,
        agent_label: str | None = None,
        branch: str | None = None,
    ) -> StoryClaim:
        """Claim the story's unit: every ``pending`` and ``failed`` task → ``in_progress`` under one new token."""

        check_claimant(claimed_by)
        check_label(agent_label)
        check_branch(branch)

        async def work(run: Work) -> StoryClaim:
            verdict = run.board.verdict(story_id, claimed_by)
            if verdict.code == "repeat":
                return await self._answer(run, story_id, verdict, repeat=True)
            if verdict.code != "ok":
                raise refusal(run.board, story_id, verdict)
            token = uuid.uuid4().hex
            tasks = {task_id: run.loaded.tasks[task_id] for task_id in verdict.tasks}
            await run.set_stories(
                {
                    unit_id: {
                        "claimed_by": claimed_by,
                        "claimed_at": run.now,
                        "agent_label": agent_label,
                        "claim_token": token,
                        **({} if branch is None else {"branch": branch}),
                    }
                    for unit_id in verdict.unit
                }
            )
            await run.set_tasks(
                {
                    task_id: {
                        "status": "in_progress",
                        "claim_token": token,
                        "claimed_by": claimed_by,
                        "claimed_at": run.now,
                        "attempts": task.attempts + 1,
                        "progress": None,
                    }
                    for task_id, task in tasks.items()
                }
            )
            log_event(logger, INFO, "graph.plan.story_claimed", story_id=story_id, tasks=len(tasks))
            return await self._answer(run, story_id, verdict, repeat=False, token=token)

        return await self._transact(project_id, lambda board: board.unit_of(story_id), work)

    async def release(
        self,
        project_id: str,
        story_id: str,
        *,
        claim_token: str | None = None,
        force: bool = False,
        note: str | None = None,
        branch: str | None = None,
    ) -> StoryRelease:
        """End the claim: the claimed tasks → ``pending``; ``note`` and ``branch`` are stored when given."""

        check_note(note)
        check_branch(branch)
        if claim_token is None and not force:
            raise TaskStateError("claim_mismatch")

        async def work(run: Work) -> StoryRelease:
            if not force:
                persisting(run.board)
            story = stored_story(run.board, story_id)
            if story.claimed_by is None:
                if force or same_token(story.claim_token, claim_token):
                    return StoryRelease(released=(), repeat=True)
                raise TaskStateError("claim_mismatch")
            if not force and not same_token(story.claim_token, claim_token):
                raise TaskStateError("claim_mismatch")
            group = held_group(run.board, story_id)
            claimed = claimed_tasks(run.board, group, story.claim_token)
            extra: dict[str, Any] = {"note": note} if note is not None else {}
            await run.set_tasks({task_id: {**CLEARED_TASK, "status": "pending", "note": None} for task_id in claimed})
            await run.set_stories(
                {other: {**CLEARED_STORY, **extra, **({} if branch is None else {"branch": branch})} for other in group}
            )
            log_event(logger, INFO, "graph.plan.story_released", story_id=story_id, tasks=len(claimed), force=force)
            return StoryRelease(released=claimed)

        return await self._transact(project_id, lambda board: held_group(board, story_id), work)

    async def report_progress(
        self, project_id: str, task_id: str, *, claim_token: str, progress: TaskProgress, note: str | None = None
    ) -> StoryTask:
        """Set ``progress`` (and a note) on a claimed task of a held story; the status stays ``in_progress``."""

        check_progress(progress)
        check_note(note)
        story_id = await self._story_of(project_id, task_id)

        async def work(run: Work) -> StoryTask:
            persisting(run.board)
            task = run.loaded.tasks.get(task_id)
            story = run.board.stories.get(story_id)
            if task is None or story is None:
                raise TaskStateError("unknown_task")
            if (
                task.status != "in_progress"
                or story.claimed_by is None
                or not same_token(task.claim_token, claim_token)
                or not same_token(story.claim_token, claim_token)
            ):
                raise TaskStateError("claim_mismatch")
            if task.progress != progress or (note is not None and task.note != note):
                await run.set_tasks({task_id: {"progress": progress, **({} if note is None else {"note": note})}})
            fresh = await run.reload()
            return story_task_items(fresh, [fresh.tasks[task_id]])[0]

        return await self._transact(project_id, lambda board: held_group(board, story_id), work)

    async def complete(
        self,
        project_id: str,
        story_id: str,
        *,
        claim_token: str,
        completed_by: str,
        merge_commit: str,
        pr_url: str | None = None,
        done: Collection[str] | None = None,
    ) -> Completed:
        """Complete the tasks in ``done`` (default: every claimed task) with the merge commit; the other claimed tasks
        return to ``pending``; the claim ends."""

        check_claimant(completed_by)
        commit = commit_text(merge_commit)
        check_pr_url(pr_url)

        async def work(run: Work) -> Completed:
            persisting(run.board)
            story = stored_story(run.board, story_id)
            if story.claimed_by is None:
                return completion_repeat(run.board, story, claim_token, completed_by, commit)
            if not same_token(story.claim_token, claim_token):
                raise TaskStateError("claim_mismatch")
            group = held_group(run.board, story_id)
            claimed = claimed_tasks(run.board, group, claim_token)
            check_done(done, claimed)
            finished = tuple(task_id for task_id in claimed if done is None or task_id in set(done))
            returned = tuple(task_id for task_id in claimed if task_id not in finished)
            before = claimable(StoryBoard(run.loaded, applying=False))
            await run.set_tasks(
                {
                    **{
                        task_id: {
                            **CLEARED_TASK,
                            "status": "completed",
                            "commit": commit,
                            "completed_by": completed_by,
                            "completed_at": run.now,
                        }
                        for task_id in finished
                    },
                    **{task_id: {**CLEARED_TASK, "status": "pending", "note": None} for task_id in returned},
                }
            )
            await run.set_stories(
                {other: {**CLEARED_STORY, **({} if pr_url is None else {"pr_url": pr_url})} for other in group}
            )
            fresh = StoryBoard(await run.reload(), applying=False)
            available = claimable(fresh) - before - set(group)
            implements = tuple(
                dict.fromkeys(spec_id for task_id in finished for spec_id in run.loaded.tasks[task_id].implements)
            )
            log_event(
                logger,
                INFO,
                "graph.plan.story_completed",
                story_id=story_id,
                completed=len(finished),
                released=len(returned),
            )
            return Completed(finished, returned, tuple(sorted(available, key=fresh.rank_key)), implements)

        return await self._transact(project_id, lambda board: held_group(board, story_id), work)

    async def block(
        self, project_id: str, story_id: str, *, claim_token: str, reason: BlockReason, finding_id: str
    ) -> StoryRelease:
        """Block every claimed task with ``reason`` and ``blocked_by`` = ``finding_id``; the claim ends."""

        check_story_reason(reason)
        check_printable(finding_id, FINDING_LIMIT)

        async def work(run: Work) -> StoryRelease:
            persisting(run.board)
            story = stored_story(run.board, story_id)
            if story.claimed_by is None:
                again = blocked_by(run.board.loaded.story_tasks(story_id), finding_id)
                if same_token(story.claim_token, claim_token) and again:
                    return StoryRelease(released=again, repeat=True)
                raise TaskStateError("claim_mismatch")
            if not same_token(story.claim_token, claim_token):
                raise TaskStateError("claim_mismatch")
            group = held_group(run.board, story_id)
            claimed = claimed_tasks(run.board, group, claim_token)
            await run.set_tasks(
                {
                    task_id: {**CLEARED_TASK, "status": "blocked", "block_reason": reason, "blocked_by": finding_id}
                    for task_id in claimed
                }
            )
            await run.set_stories({other: dict(CLEARED_STORY) for other in group})
            log_event(logger, INFO, "graph.plan.story_blocked", story_id=story_id, tasks=len(claimed), reason=reason)
            return StoryRelease(released=claimed)

        return await self._transact(project_id, lambda board: held_group(board, story_id), work)

    async def unblock_finding(self, project_id: str, finding_id: str) -> tuple[str, ...]:
        """Return the tasks still blocked by ``finding_id`` to ``pending`` (a host call after a person resolved it)."""

        check_printable(finding_id, FINDING_LIMIT)

        async def work(run: Work) -> tuple[str, ...]:
            persisting(run.board)
            ids = blocked_ids(run.board, finding_id)
            await run.set_tasks(
                {task_id: {"status": "pending", "block_reason": None, "blocked_by": None} for task_id in ids}
            )
            log_event(logger, INFO, "graph.plan.finding_unblocked", tasks=len(ids))
            return ids

        return await self._transact(project_id, lambda board: blocked_stories(board, finding_id), work)

    async def _answer(
        self, run: Work, story_id: str, verdict: Verdict, *, repeat: bool, token: str | None = None
    ) -> StoryClaim:
        fresh = await run.reload()
        story = next(item for item in fresh.current.stories if item.story_id == story_id)
        held = token or next(item.claim_token for item in fresh.current.stories if item.story_id in verdict.unit)
        order = [story_id, *(other for other in verdict.unit if other != story_id)]
        if held is None or story.claimed_at is None:
            raise TaskStateError("claim_mismatch")
        return StoryClaim(
            claim_token=held,
            story_ids=tuple(order),
            tasks=story_task_items(fresh, [fresh.tasks[task_id] for task_id in verdict.tasks]),
            claimed_at=story.claimed_at,
            repeat=repeat,
        )

    async def _story_of(self, project_id: str, task_id: str) -> str:
        loaded = LoadedPlan(await read_current_plan(self._executor, project_id))
        task = loaded.tasks.get(task_id)
        if task is None:
            raise TaskStateError("unknown_task")
        return task.story_id

    async def _transact(
        self,
        project_id: str,
        candidates: Callable[[StoryBoard], Collection[str]],
        work: Callable[[Work], Awaitable[_T]],
    ) -> _T:
        """Run ``work`` in one transaction that holds the locks of the stories ``candidates`` names (and their tasks):
        guessed from an unlocked read, checked after the locks (a unit that grew gets locked in a second round)."""

        wanted = set(candidates(StoryBoard(LoadedPlan(await read_current_plan(self._executor, project_id)))))
        for _ in range(ATTEMPTS):
            async with self._executor.tx(project_id) as tx:
                ids = sorted(wanted)
                await tx.run(LOCK_STORIES, story_ids=ids)
                await tx.run(LOCK_TASKS, story_ids=ids)
                board = StoryBoard(LoadedPlan(await read_current_plan(TxReader(tx), project_id)))
                needed = set(candidates(board))
                if needed <= wanted:
                    return await work(Work(tx, project_id, board, self._clock().isoformat(timespec="seconds")))
                wanted |= needed
        raise TaskStateError("not_available")
