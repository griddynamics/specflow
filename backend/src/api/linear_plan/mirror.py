"""The task status mirror (decision O4): when an agent changes the status of a Graphico task, the Linear sub-issue of
the task follows. ``in_progress`` moves the issue to the team's first ``started`` state, ``completed`` to its first
``completed`` state, and never backwards (a person who moved it further is left alone). ``failed`` and ``blocked`` move
nothing and leave one comment, under a deterministic id so a retried call never duplicates it. A pull request of the
task is attached as a link (the same URL is idempotent at Linear). Only a project with an enabled target and a task
with an issue row is mirrored. A Linear failure is logged as its code and never reaches the agent's call."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Final

from api.contained import Contained
from api.linear_plan.store import PlanStore, TargetChangedError
from db.linear_plan import LinearTargetView
from db.models import LinearIssue
from graph.plan.reader import LoadedPlan
from linear.errors import LinearAlreadyExistsError, LinearError, LinearRateLimitedError
from linear.gateway import LinearGateway
from linear.ids import KIND_COMMENT, node_uuid
from linear.models import StateType, WorkflowState
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.linear_plan.mirror")

PR_TITLE: Final = "Pull request"
# The state types in the order an issue only moves forward through; ``canceled`` is final like ``completed``.
_RANK: Final[dict[StateType, int]] = {
    "triage": 0,
    "backlog": 0,
    "unstarted": 0,
    "started": 1,
    "completed": 2,
    "canceled": 2,
}
_STATE_OF: Final[dict[str, StateType]] = {"in_progress": "started", "completed": "completed"}
_COMMENTED: Final = frozenset({"failed", "blocked"})


@dataclass(frozen=True)
class TaskChange:
    """One task whose status an agent changed: the plan node, the new status, why (``failed``/``blocked``) and the
    pull request of the work."""

    node_id: str
    status: str
    reason: str | None = None
    pr_url: str | None = None


@dataclass(frozen=True)
class MirrorDeps:
    """What the mirror reads and calls: the same pieces as the plan pass."""

    store: PlanStore
    gateway_for: Callable[[uuid.UUID], AbstractAsyncContextManager[LinearGateway]]
    issue_of: Callable[[str, str], Awaitable[LinearIssue | None]]
    load_plan: Callable[[str], Awaitable[LoadedPlan]]


def first_state(states: Sequence[WorkflowState], state_type: StateType) -> WorkflowState | None:
    """The state of ``state_type`` with the lowest position."""

    return min((state for state in states if state.type == state_type), key=lambda state: state.position, default=None)


async def mirror_changes(project_id: str, changes: Sequence[TaskChange], deps: MirrorDeps) -> None:
    """Mirror ``changes`` to Linear. Never raises (cancellation excepted): a failure is logged as its code, a rate
    limit ends the run (the next status change or a full pass catches up)."""

    with Contained(passing=(LinearError,)) as step:  # a project not connected, a database error: the call is done
        try:
            await _Mirror(project_id, deps).run(changes)
        except LinearRateLimitedError as error:
            log_event(logger, WARNING, "api.linear_plan.mirror.rate_limited", project_id=project_id, code=str(error))
        except LinearError as error:
            log_event(logger, WARNING, "api.linear_plan.mirror.failed", project_id=project_id, code=str(error))
    if step.failed:
        log_event(logger, WARNING, "api.linear_plan.mirror.failed", project_id=project_id, code=step.error_type)


class _Mirror:
    def __init__(self, project_id: str, deps: MirrorDeps) -> None:
        self.project_id = project_id
        self.pid = uuid.UUID(project_id)
        self.deps = deps
        self.states: dict[str, list[WorkflowState]] = {}

    async def run(self, changes: Sequence[TaskChange]) -> None:
        target = await self.deps.store.get_target(self.pid)
        if target is None or not target.enabled:
            return
        rows = [(change, await self.deps.issue_of(self.project_id, change.node_id)) for change in changes]
        wanted = [(change, row) for change, row in rows if row is not None and row.issue_id and not row.removed_at]
        if not wanted:
            return
        async with self.deps.gateway_for(self.pid) as gateway:
            for change, row in wanted:
                await self._one(gateway, target, change, str(row.issue_id))

    async def _one(self, gateway: LinearGateway, target: LinearTargetView, change: TaskChange, issue_id: str) -> None:
        if change.status in _STATE_OF:
            await self._move(gateway, target, change, issue_id)
        elif change.status in _COMMENTED:
            await self._comment(gateway, issue_id, change)
        if change.pr_url and change.status == "completed":
            await gateway.attach_link(issue_id, change.pr_url, PR_TITLE)

    async def _move(self, gateway: LinearGateway, target: LinearTargetView, change: TaskChange, issue_id: str) -> None:
        issue = await gateway.issue(issue_id)
        wanted_type = _STATE_OF[change.status]
        if _RANK[issue.state.type] >= _RANK[wanted_type]:
            return
        if issue.team_id not in self.states:
            self.states[issue.team_id] = await gateway.workflow_states(issue.team_id)
        state = first_state(self.states[issue.team_id], wanted_type)
        if state is None:
            log_event(logger, INFO, "api.linear_plan.mirror.no_state", project_id=self.project_id, state=wanted_type)
            return
        await gateway.update_issue(issue.id, state_id=state.id)
        try:
            await self.deps.store.update_item(target, change.node_id, mirrored_state=state.name)
        except TargetChangedError:
            return

    async def _comment(self, gateway: LinearGateway, issue_id: str, change: TaskChange) -> None:
        loaded = await self.deps.load_plan(self.project_id)
        found = loaded.tasks.get(change.node_id)
        attempt = 0 if found is None else found.attempts
        comment_id = str(node_uuid(self.pid, KIND_COMMENT, f"{change.node_id}:{change.status}:{attempt}"))
        body = f"Graphico: task {change.status}" + (f": {change.reason}" if change.reason else "")
        try:
            await gateway.create_comment(issue_id, body, comment_id=comment_id)
        except LinearAlreadyExistsError:
            return  # a retried call: the comment is there
