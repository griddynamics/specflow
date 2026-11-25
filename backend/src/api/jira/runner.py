"""The in-process Jira sync runner: the Jira side of ``api.exports.runner.ExportRunner``.

The runner (per-project drain, merged starts, one pass at a time, ``running`` before the slot, startup recovery, live
progress) is ``api.exports.runner``; this class says how a Jira target is read, how a pass is recorded and run (as the
target's acting account), and what a Jira status and item look like. Rules: docs/jira-sync/sync.md#triggers-and-runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast, override

from agent_contracts.jira_models import JiraSyncItem, JiraSyncProgress, JiraSyncStatus
from api.exports.runner import ExportRunner, ProjectRun, utc_now
from api.exports.status import FAILED
from api.jira.connections import AtlassianConnections
from api.jira.desired import EPIC, SpecRef
from api.jira.state import items_of, status_of
from api.jira.sync import NOT_CONFIGURED, SyncCounts, SyncDeps, SyncProgress, SyncResult, sync_once
from db import jira as jira_db
from db.jira import JiraTargetView, TargetSaved
from graph.core import Ontology, load_ontology
from graph.core.reader import GraphReader
from graph.core.store import as_executor
from graph.plan.reader import LoadedPlan, load_plan
from jira.gateway import JiraError, JiraGateway
from logger.logger import get_logger

logger = get_logger("api.jira.runner")

_Snapshot = tuple[JiraTargetView | None, str | None]  # the target and the key of the Epic Specflow created, if any


class JiraRunnerClosedError(RuntimeError):
    """A sync was requested after the runner was closed."""


@dataclass(frozen=True)
class JiraRunnerDependencies:
    """What the runner builds a pass from; the defaults are production's, tests swap in stubs."""

    sync_once: Callable[[str, SyncDeps], Awaitable[SyncResult]] = sync_once
    load_plan: Callable[[str], Awaitable[LoadedPlan]] | None = None  # the graph store's current plan when ``None``
    spec_names: Callable[[str, Sequence[str]], Awaitable[Mapping[str, SpecRef]]] | None = None  # graph reads
    now: Callable[[], datetime] = utc_now


class JiraSyncRunner(ExportRunner[_Snapshot, JiraSyncStatus, SyncResult]):
    """Starts, merges and tracks the Jira sync passes of this process; held on ``Runtime.jira``."""

    logger = logger
    label = "jira sync"
    event_prefix = "api.jira.sync"

    def __init__(
        self,
        gateway: JiraGateway | None,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        store: Any,
        public_url: str | None,
        site_url: str,
        dependencies: JiraRunnerDependencies | None = None,
        connections: AtlassianConnections | None = None,
    ) -> None:
        """``token`` mode passes the one ``gateway``; ``oauth`` mode passes ``connections`` (and no gateway): each pass
        then acts as the target's ``connected_user_id`` (docs/jira-sync/oauth.md "Acting account of a target")."""

        deps = dependencies or JiraRunnerDependencies()
        super().__init__(closed_error=JiraRunnerClosedError, now=deps.now)
        self._gateway = gateway
        self._connections = connections
        self._transaction = transaction
        self._store = store
        self._site_url = site_url
        self._sync_once = deps.sync_once
        self._ontology_value: Ontology | None = None
        self._deps = SyncDeps(
            gateway=cast(JiraGateway, gateway),  # None in oauth mode: each pass puts its acting account's gateway
            transaction=transaction,
            load_plan=deps.load_plan or self._load_plan,
            spec_names=deps.spec_names or self._spec_names,
            public_url=public_url,
            now=deps.now,
            report=self._progress,
        )

    @property
    def gateway(self) -> JiraGateway | None:
        """The gateway of the server's one Atlassian account (``token`` mode); ``None`` in ``oauth`` mode."""

        return self._gateway

    @property
    def connections(self) -> AtlassianConnections | None:
        """The users' Atlassian connections (``oauth`` mode); ``None`` in ``token`` mode."""

        return self._connections

    async def gateway_for(self, user_id: uuid.UUID | None) -> JiraGateway:
        """The gateway acting as ``user_id`` (``oauth``: their connection, ``JiraError`` with ``jira_not_connected`` or
        ``jira_reconnect_required`` when it cannot act), or the one account (``token``: ``user_id`` is ignored)."""

        if self._connections is None:
            return cast(JiraGateway, self._gateway)
        return await self._connections.gateway(user_id)

    # Public ------------------------------------------------------------------

    async def after_settings(self, project_id: str, saved: TargetSaved) -> None:
        """The settings were saved: a pass when the target was turned on or its keys changed (it merges into a pass
        the change stopped, FR-JIRA-RUN-08). A disabled target starts nothing. Never raises."""

        if saved.turned_on or saved.keys_changed:
            await self.start_contained(project_id, "settings")

    async def status(self, project_id: str) -> JiraSyncStatus | None:
        """The sync status of the project; ``None`` when it has no target."""

        target, created_epic = await self._read(project_id)
        if target is None:
            return None
        running, queued = self.live(project_id)
        return self.status_from(project_id, target, created_epic, running=running, queued=queued)

    def status_from(
        self, project_id: str, target: JiraTargetView, created_epic: str | None, *, running: bool, queued: bool
    ) -> JiraSyncStatus:
        """The status of one snapshot of the target, with the live pass's trigger and queue time when ``running``."""

        status = status_of(target, live=running, queued=queued, created_epic=created_epic)
        project = self._projects.get(project_id)
        return self._live(status, project) if running and project is not None else status

    def _progress(self, project_id: str, progress: SyncProgress) -> None:
        self.report(project_id, JiraSyncProgress(step=progress.step, done=progress.done, total=progress.total))

    @staticmethod
    def _live(status: JiraSyncStatus, project: ProjectRun) -> JiraSyncStatus:
        # the stored row only has the current pass's values once its ``record_start`` ran
        return status.model_copy(
            update={"trigger": project.pass_trigger, "started_at": project.queued_at, "progress": project.progress}
        )

    async def items(self, project_id: str) -> list[JiraSyncItem]:
        async with self._transaction() as session:
            rows = await jira_db.list_issues(session, uuid.UUID(project_id))
        return items_of(rows, self._site_url)

    @override
    async def aclose(self) -> None:
        """Cancel the queued and running passes (the next start repeats them) and close the gateway."""

        await super().aclose()
        if self._gateway is not None:
            await self._gateway.aclose()
        if self._connections is not None:
            await self._connections.aclose()

    # Reads ---------------------------------------------------------------------

    @override
    async def _read(self, project_id: str) -> _Snapshot:
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

    # The hooks of the runner ---------------------------------------------------

    @override
    def _enabled(self, snapshot: _Snapshot) -> bool:
        return snapshot[0] is not None and snapshot[0].enabled

    @override
    def _skipped(self, project_id: str, snapshot: _Snapshot) -> JiraSyncStatus:
        return _not_configured(project_id, snapshot[0])

    @override
    def _answer(self, snapshot: _Snapshot, project: ProjectRun, *, queued: bool) -> JiraSyncStatus:
        target, created_epic = snapshot
        return self._live(
            status_of(cast(JiraTargetView, target), live=True, queued=queued, created_epic=created_epic), project
        )

    @override
    async def _record_start(self, pid: uuid.UUID, trigger: str, queued_at: datetime) -> None:
        async with self._transaction() as session:
            await jira_db.record_start(session, pid, trigger=trigger, queued_at=queued_at)

    @override
    async def _record_end(self, pid: uuid.UUID, result: SyncResult, finished_at: datetime) -> None:
        async with self._transaction() as session:
            await jira_db.record_end(
                session,
                pid,
                status=result.status,
                error_code=result.error_code,
                counts=dataclasses.asdict(result.counts),
                finished_at=finished_at,
            )

    @override
    def _outcome(self, result: SyncResult) -> tuple[str, str | None, Mapping[str, Any]]:
        return result.status, result.error_code, dataclasses.asdict(result.counts)

    @override
    async def _running_projects(self) -> list[uuid.UUID]:
        async with self._transaction() as session:
            return await jira_db.running_targets(session)

    @override
    async def _run(self, project_id: str, pid: uuid.UUID) -> SyncResult:
        """One pass with the gateway of its acting account (``oauth``), which may refuse: the pass then ends
        ``failed`` with ``jira_not_connected`` or ``jira_reconnect_required`` and no Jira call. The acting account is
        read here, with the slot held, so a takeover saved while the pass waited for the slot is the one it acts as."""

        deps = self._deps
        if self._connections is not None:
            async with self._transaction() as session:
                target = await jira_db.get_target(session, pid)
            try:
                gateway = await self._connections.gateway(target.connected_user_id if target is not None else None)
            except JiraError as error:
                return SyncResult(FAILED, error.reason.value, SyncCounts())
            deps = dataclasses.replace(deps, gateway=gateway)
        return await self._sync_once(project_id, deps)


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
