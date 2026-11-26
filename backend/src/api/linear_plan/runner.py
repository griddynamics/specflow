"""The in-process Linear plan runner: the Linear side of ``api.exports.runner.ExportRunner``.

The runner (per-project drain, merged starts, one pass at a time, ``running`` before the slot, startup recovery) is
``api.exports.runner``; this class says how the target is read, how a pass is recorded and run (as the member the
project's Linear is connected by, read for every pass: rule R1) and what a status looks like. A pass that Linear
rate-limited ends ``failed`` with ``linear_rate_limited`` and the runner starts the project again after the
``Retry-After`` Linear sent (a ``recovery`` pass; the pass is idempotent, so it only finishes the work).
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Any, Final, cast, override

from agent_contracts.linear_models import LinearSyncItem, LinearSyncProgress, LinearSyncStatus
from api.exports.runner import ExportRunner, ProjectRun, utc_now
from api.jira.desired import SpecRef
from api.linear_plan.gateway import GatewayFactory, NotConnectedError
from api.linear_plan.mirror import MirrorDeps, TaskChange, mirror_changes
from api.linear_plan.state import items_of, not_configured, status_of
from api.linear_plan.store import DbStore, PlanStore
from api.linear_plan.sync import RATE_LIMITED, PushDeps, PushProgress, PushResult, push_once
from db import integrations as integrations_db
from db import linear_plan as plan_db
from db.integrations import LINEAR
from db.linear_plan import LinearTargetView, TargetSaved
from db.models import LinearIssue
from graph.core import load_ontology
from graph.core.reader import GraphReader
from graph.core.store import as_executor
from graph.plan.reader import LoadedPlan, load_plan
from linear.gateway import LinearGateway
from logger.logger import INFO, get_logger, log_event

logger = get_logger("api.linear_plan.runner")

RETRY_FLOOR_SECONDS: Final = 1.0
RETRY_CEILING_SECONDS: Final = 3600.0


class LinearPlanClosedError(RuntimeError):
    """An export was requested after the runner was closed."""


class LinearPlanRunner(ExportRunner[LinearTargetView | None, LinearSyncStatus, PushResult]):
    """Starts, merges and tracks the Linear plan passes of this process; held on ``Runtime.linear_plan``."""

    logger = logger
    label = "linear plan"
    event_prefix = "api.linear_plan.sync"

    def __init__(
        self,
        gateway_factory: GatewayFactory,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        graph_store: Any,
        public_url: str | None = None,
        now: Callable[[], datetime] = utc_now,
        push: Callable[[str, PushDeps], Awaitable[PushResult]] = push_once,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(closed_error=LinearPlanClosedError, now=now)
        self._factory = gateway_factory
        self._transaction = transaction
        self._graph = graph_store
        self._push = push
        self._sleep = sleep
        self._store: PlanStore = DbStore(transaction)
        self._retries: dict[str, asyncio.Task[None]] = {}
        self._mirrors: set[asyncio.Task[None]] = set()
        self._mirror_lock = asyncio.Lock()
        self._mirror_deps = MirrorDeps(
            store=self._store, gateway_for=self._gateway_for, issue_of=self.issue_of, load_plan=self._load_plan
        )
        self._deps = PushDeps(
            store=self._store,
            gateway_for=self._gateway_for,
            load_plan=self._load_plan,
            spec_names=self._spec_names,
            now=now,
            public_url=public_url,
            report=self._progress,
        )

    def gateway_of(self, user_id: uuid.UUID) -> AbstractAsyncContextManager[LinearGateway]:
        """The gateway acting as ``user_id`` (the member the project's Linear is connected by), as an async context
        manager: it closes the client and renews the token once when Linear refuses it."""

        return self._factory(user_id)

    @asynccontextmanager
    async def _gateway_for(self, project_id: uuid.UUID) -> AsyncIterator[LinearGateway]:
        """The gateway of the member the project's Linear is bound to now (rule R1), not the one who saved the target:
        a removed member, a removed integration and a take-over all take effect on the next pass. No binding is
        ``NotConnectedError``; an expired connection is ``LinearUnauthorizedError`` at the first call."""

        async with self._transaction() as session:
            bound = await integrations_db.get_integration(session, project_id, LINEAR)
        if bound is None or bound.user_id is None:
            raise NotConnectedError
        async with self.gateway_of(bound.user_id) as gateway:
            yield gateway

    # Public --------------------------------------------------------------------

    async def after_settings(self, project_id: str, saved: TargetSaved) -> None:
        """The destination was saved: a pass when it was turned on or changed. Never raises."""

        if saved.turned_on or saved.keys_changed:
            await self.start_contained(project_id, "settings")

    async def status(self, project_id: str) -> LinearSyncStatus | None:
        """The sync status of the project; ``None`` when it has no target."""

        target = await self._read(project_id)
        return None if target is None else self.status_from(target)

    def status_from(self, target: LinearTargetView) -> LinearSyncStatus:
        running, queued = self.live(str(target.project_id))
        status = status_of(target, live=running, queued=queued)
        project = self._projects.get(str(target.project_id))
        return self._live(status, project) if running and project is not None else status

    async def items(self, project_id: str) -> list[LinearSyncItem]:
        async with self._transaction() as session:
            target = await plan_db.get_target(session, uuid.UUID(project_id))
            rows = await plan_db.list_items(session, uuid.UUID(project_id))
        return items_of(rows, None if target is None else target.linear_project_url)

    @override
    async def aclose(self) -> None:
        """Cancel the queued and running passes, the waiting retries and the status mirrors (the next start repeats
        the passes)."""

        tasks = [task for task in (*self._retries.values(), *self._mirrors) if not task.done()]
        for task in tasks:
            task.cancel()
        await super().aclose()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _progress(self, project_id: str, progress: PushProgress) -> None:
        step = cast(Any, progress.step)
        self.report(project_id, LinearSyncProgress(step=step, done=progress.done, total=progress.total))

    @staticmethod
    def _live(status: LinearSyncStatus, project: ProjectRun) -> LinearSyncStatus:
        return status.model_copy(
            update={"trigger": project.pass_trigger, "started_at": project.queued_at, "progress": project.progress}
        )

    async def _write(self, call: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """One call of ``db.linear_plan`` in its own transaction."""

        async with self._transaction() as session:
            return await call(session, *args, **kwargs)

    # Reads ---------------------------------------------------------------------

    async def _load_plan(self, project_id: str) -> LoadedPlan:
        return await load_plan(as_executor(self._graph), project_id)

    async def _spec_names(self, project_id: str, ids: Sequence[str]) -> Mapping[str, SpecRef]:
        """Each asked spec id (an alias too) to the current id and name of its node; an unknown id is left out."""

        ontology = await asyncio.to_thread(load_ontology)
        found = await GraphReader(as_executor(self._graph), ontology).nodes_by_spec_ids(project_id, ids)
        refs: dict[str, SpecRef] = {}
        for node in found.values():
            refs.update(dict.fromkeys((node.spec_id, *node.spec_id_aliases), SpecRef(node.spec_id, node.name)))
        return {name: refs[name] for name in ids if name in refs}

    # The hooks of the runner ---------------------------------------------------

    @override
    async def _read(self, project_id: str) -> LinearTargetView | None:
        return cast(LinearTargetView | None, await self._write(plan_db.get_target, uuid.UUID(project_id)))

    @override
    def _enabled(self, snapshot: LinearTargetView | None) -> bool:
        return snapshot is not None and snapshot.enabled

    @override
    def _skipped(self, project_id: str, snapshot: LinearTargetView | None) -> LinearSyncStatus:
        return not_configured(project_id, snapshot)

    @override
    def _answer(self, snapshot: LinearTargetView | None, project: ProjectRun, *, queued: bool) -> LinearSyncStatus:
        return self._live(status_of(cast(LinearTargetView, snapshot), live=True, queued=queued), project)

    @override
    async def _record_start(self, pid: uuid.UUID, trigger: str, queued_at: datetime) -> None:
        await self._write(plan_db.record_start, pid, trigger=trigger, queued_at=queued_at)

    @override
    async def _run(self, project_id: str, pid: uuid.UUID) -> PushResult:
        result = await self._push(project_id, self._deps)
        if result.error_code == RATE_LIMITED:
            self._retry_later(project_id, result.retry_after)
        return result

    @override
    async def _record_end(self, pid: uuid.UUID, result: PushResult, finished_at: datetime) -> None:
        counts = dataclasses.asdict(result.counts)
        await self._write(
            plan_db.record_end,
            pid,
            status=result.status,
            error_code=result.error_code,
            counts=counts,
            finished_at=finished_at,
        )

    @override
    def _outcome(self, result: PushResult) -> tuple[str, str | None, Mapping[str, Any]]:
        return result.status, result.error_code, dataclasses.asdict(result.counts)

    @override
    async def _running_projects(self) -> list[uuid.UUID]:
        return cast(list[uuid.UUID], await self._write(plan_db.running_targets))

    # Rate limit ----------------------------------------------------------------

    def _retry_later(self, project_id: str, retry_after: float | None) -> None:
        """Start the project again once Linear's ``Retry-After`` has passed (one waiting retry per project)."""

        delay = min(max(retry_after or RETRY_FLOOR_SECONDS, RETRY_FLOOR_SECONDS), RETRY_CEILING_SECONDS)
        waiting = self._retries.get(project_id)
        if waiting is not None and not waiting.done():
            return
        self._retries[project_id] = asyncio.create_task(
            self._retry(project_id, delay), name=f"linear-retry-{project_id}"
        )
        log_event(logger, INFO, f"{self.event_prefix}.retry_scheduled", project_id=project_id, seconds=delay)

    async def _retry(self, project_id: str, delay: float) -> None:
        await self._sleep(delay)
        self._retries.pop(project_id, None)  # before the start: the new pass may be rate-limited again
        await self.start_contained(project_id, "recovery")

    # Seam of the task status mirror (stream S3b) -------------------------------

    async def issue_of(self, project_id: str, node_id: str) -> LinearIssue | None:
        """The map row (Linear issue id and identifier) of a plan node, for the status mirror."""

        async with self._transaction() as session:
            return await plan_db.get_item(session, uuid.UUID(project_id), node_id)

    def mirror_task_status(self, project_id: str, changes: Sequence[TaskChange]) -> None:
        """Mirror the status ``changes`` of tasks to their Linear issues in the background, one run at a time (a
        started task never overtakes its completion). Never raises and never waits for Linear; a closed runner does
        nothing."""

        if self._closed or not changes:
            return
        task = asyncio.create_task(self._mirror(project_id, tuple(changes)), name=f"linear-mirror-{project_id}")
        self._mirrors.add(task)
        task.add_done_callback(self._mirrors.discard)

    async def _mirror(self, project_id: str, changes: tuple[TaskChange, ...]) -> None:
        async with self._mirror_lock:
            await mirror_changes(project_id, changes, self._mirror_deps)
