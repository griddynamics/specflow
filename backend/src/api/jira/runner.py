"""The in-process Jira sync runner: one drain task per project, one pass at a time in the process.

Rules: docs/jira-sync/sync.md#triggers-and-runs. A start while the project's pass is queued or runs sets a flag and
the pass is repeated once when it ends (starts are merged, never dropped). A pass records ``running`` as soon as it is
queued, before it waits for the process-wide slot, so a restart recovers a waiting pass too. Logs carry ids, codes and
counts.

A start made while the pass waits for the slot also schedules one more pass: this is intended, it keeps the ``queued``
answer true.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from agent_contracts.jira_models import JiraSyncItem, JiraSyncStatus
from api.contained import Contained
from api.jira.desired import EPIC, SpecRef
from api.jira.state import Trigger, items_of, status_of
from api.jira.sync import NOT_CONFIGURED, SyncDeps, SyncResult, sync_once
from db import jira as jira_db
from db.jira import JiraTargetView, TargetSaved
from graph.core import Ontology, load_ontology
from graph.core.reader import GraphReader
from graph.core.store import as_executor
from graph.plan.reader import LoadedPlan, load_plan
from jira.gateway import JiraGateway
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.jira.runner")

MAX_CONCURRENT_SYNCS: Final = 1  # passes at once in this process: one Atlassian account, the other projects wait


class JiraRunnerClosedError(RuntimeError):
    """A sync was requested after the runner was closed."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class JiraRunnerDependencies:
    """What the runner builds a pass from; the defaults are production's, tests swap in stubs."""

    sync_once: Callable[[str, SyncDeps], Awaitable[SyncResult]] = sync_once
    load_plan: Callable[[str], Awaitable[LoadedPlan]] | None = None  # the graph store's current plan when ``None``
    spec_names: Callable[[str, Sequence[str]], Awaitable[Mapping[str, SpecRef]]] | None = None  # graph reads
    now: Callable[[], datetime] = _utc_now


@dataclass
class _Project:
    trigger: Trigger  # the trigger of the next pass (the latest start; a merge overwrites it)
    queued_at: datetime  # when the current pass was queued
    pass_trigger: Trigger  # the trigger of the current pass (what a live status shows)
    running: bool = False  # a pass of the project is queued or runs here (the status reads ``running``)
    dirty: bool = False  # a start came in meanwhile: one more pass follows
    task: asyncio.Task[None] | None = None


class JiraSyncRunner:
    """Starts, merges and tracks the Jira sync passes of this process; held on ``Runtime.jira``."""

    def __init__(
        self,
        gateway: JiraGateway,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        store: Any,
        public_url: str | None,
        site_url: str,
        dependencies: JiraRunnerDependencies | None = None,
    ) -> None:
        deps = dependencies or JiraRunnerDependencies()
        self._gateway = gateway
        self._transaction = transaction
        self._store = store
        self._site_url = site_url
        self._sync_once = deps.sync_once
        self._now = deps.now
        self._ontology_value: Ontology | None = None
        self._deps = SyncDeps(
            gateway=gateway,
            transaction=transaction,
            load_plan=deps.load_plan or self._load_plan,
            spec_names=deps.spec_names or self._spec_names,
            public_url=public_url,
            now=deps.now,
        )
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_SYNCS)
        self._projects: dict[str, _Project] = {}
        self._recovery: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def gateway(self) -> JiraGateway:
        """The gateway of the server's one Atlassian account (the access check uses it too)."""

        return self._gateway

    # Public ------------------------------------------------------------------

    async def start(self, project_id: str, trigger: Trigger, *, user_id: str | None = None) -> JiraSyncStatus:
        """Queue a pass of the project, or merge into the one that is queued or runs (``queued`` true).

        A project without an enabled target starts nothing and answers ``skipped`` / ``jira_not_configured``.
        ``user_id`` only goes to the log.
        """

        if self._closed:
            raise JiraRunnerClosedError("the jira sync runner is closed")
        target, created_epic = await self._read(project_id)
        if self._closed:  # ``aclose`` ran during the read: no pass may be spawned after it collected the tasks
            raise JiraRunnerClosedError("the jira sync runner is closed")
        if target is None or not target.enabled:
            return _not_configured(project_id, target)
        project = self._projects.get(project_id)
        if project is not None and project.running:
            project.dirty = True
            project.trigger = trigger
            log_event(logger, INFO, "api.jira.sync.merged", project_id=project_id, trigger=trigger, user_id=user_id)
            return self._live(status_of(target, live=True, queued=True, created_epic=created_epic), project)
        project = _Project(trigger=trigger, queued_at=self._now(), pass_trigger=trigger, running=True)
        self._projects[project_id] = project
        project.task = asyncio.create_task(self._drain(project_id, project), name=f"jira-sync-{project_id}")
        log_event(logger, INFO, "api.jira.sync.queued", project_id=project_id, trigger=trigger, user_id=user_id)
        return self._live(status_of(target, live=True, queued=False, created_epic=created_epic), project)

    async def after_plan(self, project_id: str) -> None:
        """The plan run returned: start a pass when the project has an enabled target. Never raises."""

        await self._start_contained(project_id, "plan_run")

    async def after_settings(self, project_id: str, saved: TargetSaved) -> None:
        """The settings were saved: a pass when the target was turned on or its keys changed (it merges into a pass
        the change stopped, FR-JIRA-RUN-08). A disabled target starts nothing. Never raises."""

        if saved.turned_on or saved.keys_changed:
            await self._start_contained(project_id, "settings")

    async def status(self, project_id: str) -> JiraSyncStatus | None:
        """The sync status of the project; ``None`` when it has no target."""

        target, created_epic = await self._read(project_id)
        if target is None:
            return None
        running, queued = self.live(project_id)
        return self.status_from(project_id, target, created_epic, running=running, queued=queued)

    def live(self, project_id: str) -> tuple[bool, bool]:
        """``(running, queued)``: a pass is queued or runs here; a start is merged into one more."""

        project = self._projects.get(project_id)
        running = project is not None and project.running
        return running, running and project is not None and project.dirty

    def status_from(
        self, project_id: str, target: JiraTargetView, created_epic: str | None, *, running: bool, queued: bool
    ) -> JiraSyncStatus:
        """The status of one snapshot of the target, with the live pass's trigger and queue time when ``running``."""

        status = status_of(target, live=running, queued=queued, created_epic=created_epic)
        project = self._projects.get(project_id)
        return self._live(status, project) if running and project is not None else status

    @staticmethod
    def _live(status: JiraSyncStatus, project: _Project) -> JiraSyncStatus:
        # the stored row only has the current pass's values once its ``record_start`` ran
        return status.model_copy(update={"trigger": project.pass_trigger, "started_at": project.queued_at})

    async def items(self, project_id: str) -> list[JiraSyncItem]:
        async with self._transaction() as session:
            rows = await jira_db.list_issues(session, uuid.UUID(project_id))
        return items_of(rows, self._site_url)

    def start_recovery(self) -> None:
        """Restart, in the background, the passes a stopped process left ``running``; a failure is logged only."""

        self._recovery = asyncio.create_task(self._recover(), name="jira-sync-recovery")

    async def aclose(self) -> None:
        """Cancel the queued and running passes (the next start repeats them) and close the gateway."""

        self._closed = True
        tasks = [project.task for project in self._projects.values() if project.task is not None]
        if self._recovery is not None:
            tasks.append(self._recovery)
        tasks = [task for task in tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._gateway.aclose()

    # Reads ---------------------------------------------------------------------

    async def _read(self, project_id: str) -> tuple[JiraTargetView | None, str | None]:
        """The target and, when it names no Epic, the key of the one Specflow created."""

        pid = uuid.UUID(project_id)
        async with self._transaction() as session:
            target = await jira_db.get_target(session, pid)
            if target is None or target.epic_key:
                return target, None
            rows = await jira_db.list_issues(session, pid)
        return target, next((row.jira_key for row in rows if row.node_id == EPIC and row.jira_key), None)

    async def _ontology(self) -> Ontology:
        if self._ontology_value is None:
            self._ontology_value = await asyncio.to_thread(load_ontology)
        return self._ontology_value

    async def _load_plan(self, project_id: str) -> LoadedPlan:
        return await load_plan(as_executor(self._store), project_id)

    async def _spec_names(self, project_id: str, ids: Sequence[str]) -> Mapping[str, SpecRef]:
        """Each asked id (an alias too) to the current id and name of its node; an unknown id is left out."""

        reader = GraphReader(as_executor(self._store), await self._ontology())
        refs: dict[str, SpecRef] = {}
        for node in (await reader.nodes_by_spec_ids(project_id, ids)).values():
            ref = SpecRef(node.spec_id, node.name)
            for name in (node.spec_id, *node.spec_id_aliases):
                refs[name] = ref
        return {name: refs[name] for name in ids if name in refs}

    # Starting ------------------------------------------------------------------

    async def _start_contained(self, project_id: str, trigger: Trigger) -> None:
        with Contained() as started:
            await self.start(project_id, trigger)
        if started.failed:
            log_event(
                logger,
                WARNING,
                "api.jira.sync.start_failed",
                project_id=project_id,
                trigger=trigger,
                error_type=started.error_type,
            )

    async def _recover(self) -> None:
        with Contained() as recovered:
            async with self._transaction() as session:
                project_ids = await jira_db.running_targets(session)
            for project_id in project_ids:
                await self._start_contained(str(project_id), "recovery")
        if recovered.failed:
            log_event(
                logger,
                WARNING,
                "api.jira.sync.start_failed",
                project_id=None,
                trigger="recovery",
                error_type=recovered.error_type,
            )

    # The pass ------------------------------------------------------------------

    async def _drain(self, project_id: str, project: _Project) -> None:
        try:
            while True:
                project.dirty = False
                project.pass_trigger = project.trigger
                await self._pass(project_id, project)
                if not project.dirty:
                    break
                project.queued_at = self._now()
        finally:
            project.running = False  # no await between the last check and here: a start cannot slip in

    async def _pass(self, project_id: str, project: _Project) -> None:
        trigger = project.pass_trigger
        pid = uuid.UUID(project_id)
        with Contained() as ran:  # a failed pass never stops the project's later ones
            async with self._transaction() as session:
                await jira_db.record_start(session, pid, trigger=trigger, queued_at=project.queued_at)
            async with self._slots:
                log_event(logger, INFO, "api.jira.sync.started", project_id=project_id, trigger=trigger)
                result = await self._sync_once(project_id, self._deps)
            async with self._transaction() as session:
                await jira_db.record_end(
                    session,
                    pid,
                    status=result.status,
                    error_code=result.error_code,
                    counts=dataclasses.asdict(result.counts),
                    finished_at=self._now(),
                )
            log_event(
                logger,
                INFO,
                "api.jira.sync.finished",
                project_id=project_id,
                status=result.status,
                error_code=result.error_code,
                counts=dataclasses.asdict(result.counts),  # flat names would collide with LogRecord's ``created``
            )
        if ran.failed:  # ``sync_once`` logs its own unexpected errors; this is the runner's (storage, a stub)
            log_event(
                logger,
                WARNING,
                "api.jira.sync.failed",
                project_id=project_id,
                error_code="unexpected_error",
                error_type=ran.error_type,
            )


def _not_configured(project_id: str, target: JiraTargetView | None) -> JiraSyncStatus:
    """The answer of a start with no enabled target: nothing was queued."""

    return JiraSyncStatus(
        project_id=project_id,
        enabled=False,
        status="skipped",
        queued=False,
        trigger=None,
        started_at=None,
        finished_at=None,
        error_code=NOT_CONFIGURED,
        counts=None,
        jira_project="" if target is None else target.jira_project,
        epic_key=None if target is None else target.epic_key,
    )
