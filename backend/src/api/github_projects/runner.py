"""The in-process GitHub Projects runner: the GitHub side of ``api.exports.runner.ExportRunner``.

The runner (per-project drain, merged starts, one pass at a time, ``running`` before the slot, startup recovery) is
``api.exports.runner``; this class says how the target is read, how a pass is recorded and run (as the member the
project's GitHub is connected by), and what a status looks like. Rules: docs/github/projects.md "Runs".
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, cast, override

from api.exports.runner import ExportRunner, ProjectRun, utc_now
from api.exports.status import run_state
from api.github_projects.gateway import MemberGateway, ProjectsGateway
from api.github_projects.store import DbStore, ProjectsStore
from api.github_projects.sync import NOT_CONFIGURED, PushDeps, PushResult, push_once
from api.jira.desired import SpecRef
from api.schemas import GitHubProjectsCounts, GitHubProjectsState, GitHubProjectsStatus
from db import github_projects as projects_db
from db import integrations as integrations_db
from db.github_projects import ProjectTargetView, TargetSaved
from db.integrations import GITHUB
from github.client import WaitBudget
from github.codes import GitHubCode, GitHubError
from github.connections import GitHubConnections
from github.projects import ProjectsApi
from graph.core import load_ontology
from graph.core.reader import GraphReader
from graph.core.store import as_executor
from graph.plan.reader import LoadedPlan, load_plan
from logger.logger import get_logger

logger = get_logger("api.github_projects.runner")


class GitHubProjectsClosedError(RuntimeError):
    """An export was requested after the runner was closed."""


def status_of(target: ProjectTargetView, *, live: bool, queued: bool) -> GitHubProjectsStatus:
    """The export status of a target; ``live``: a pass is queued or runs here, ``queued``: another follows."""

    run = run_state(target, live=live, not_configured=NOT_CONFIGURED)
    counts = GitHubProjectsCounts(**run.counts) if run.counts else None
    return GitHubProjectsStatus(
        project_id=str(target.project_id),
        enabled=target.enabled,
        status=cast(GitHubProjectsState, run.status),
        queued=live and queued,
        trigger=target.last_trigger,
        started_at=target.last_started_at,
        finished_at=run.finished_at,
        error_code=run.error_code,
        counts=counts,
        repository=target.repository_name,
        project_title=target.project_title,
    )


class GitHubProjectsRunner(ExportRunner[ProjectTargetView | None, GitHubProjectsStatus, PushResult]):
    """Starts, merges and tracks the GitHub Projects passes of this process; held on ``Runtime.github_projects``."""

    logger = logger
    label = "github projects"
    event_prefix = "api.github_projects.sync"

    def __init__(
        self,
        connections: GitHubConnections,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        graph_store: Any,
        now: Callable[[], datetime] = utc_now,
        push: Callable[[str, PushDeps], Awaitable[PushResult]] = push_once,
    ) -> None:
        super().__init__(closed_error=GitHubProjectsClosedError, now=now)
        self._transaction = transaction
        self._graph = graph_store
        self._push = push
        self._connections = connections
        self._store: ProjectsStore = DbStore(transaction)
        self._deps = PushDeps(
            store=self._store,
            gateway_for=self._gateway_for,
            load_plan=self._load_plan,
            spec_names=self._spec_names,
            now=now,
        )

    async def gateway_of(self, user_id: uuid.UUID) -> ProjectsGateway:
        """The gateway acting as ``user_id`` (the member the project's GitHub is connected by)."""

        api = ProjectsApi(self._connections.client, WaitBudget())  # a budget of waiting per gateway, so per pass
        return MemberGateway(self._connections, api, user_id)

    async def _gateway_for(self, project_id: uuid.UUID) -> ProjectsGateway:
        """The gateway of the member the project's GitHub is bound to now (rule R1), not the one who saved the target:
        a removed member, a removed integration and a take-over all take effect on the next pass. No binding is
        ``github_not_connected``; an expired connection is ``github_reconnect_required`` at the first call."""

        async with self._transaction() as session:
            bound = await integrations_db.get_integration(session, project_id, GITHUB)
        if bound is None or bound.user_id is None:
            raise GitHubError(GitHubCode.NOT_CONNECTED)
        return await self.gateway_of(bound.user_id)

    async def after_settings(self, project_id: str, saved: TargetSaved) -> None:
        """The destination was saved: a pass when it was turned on or changed. Never raises."""

        if saved.turned_on or saved.keys_changed:
            await self.start_contained(project_id, "settings")

    def status_from(self, target: ProjectTargetView) -> GitHubProjectsStatus:
        running, queued = self.live(str(target.project_id))
        status = status_of(target, live=running, queued=queued)
        project = self._projects.get(str(target.project_id))
        return self._live(status, project) if running and project is not None else status

    @staticmethod
    def _live(status: GitHubProjectsStatus, project: ProjectRun) -> GitHubProjectsStatus:
        return status.model_copy(update={"trigger": project.pass_trigger, "started_at": project.queued_at})

    async def _load_plan(self, project_id: str) -> LoadedPlan:
        return await load_plan(as_executor(self._graph), project_id)

    async def _spec_names(self, project_id: str, ids: Sequence[str]) -> Mapping[str, SpecRef]:
        """Each asked spec id (an alias too) to the current id and name of its node; an unknown id is left out."""

        ontology = await asyncio.to_thread(load_ontology)
        found = await GraphReader(as_executor(self._graph), ontology).nodes_by_spec_ids(project_id, ids)
        refs = {
            name: SpecRef(node.spec_id, node.name)
            for node in found.values()
            for name in (node.spec_id, *node.spec_id_aliases)
        }
        return {name: refs[name] for name in ids if name in refs}

    # The hooks of the runner ---------------------------------------------------

    @override
    async def _read(self, project_id: str) -> ProjectTargetView | None:
        async with self._transaction() as session:
            return await projects_db.get_target(session, uuid.UUID(project_id))

    @override
    def _enabled(self, snapshot: ProjectTargetView | None) -> bool:
        return snapshot is not None and snapshot.enabled

    @override
    def _skipped(self, project_id: str, snapshot: ProjectTargetView | None) -> GitHubProjectsStatus:
        return GitHubProjectsStatus(
            project_id=project_id,
            enabled=False,
            status="skipped",
            queued=False,
            trigger=None,
            started_at=None,
            finished_at=None,
            error_code=NOT_CONFIGURED,
            counts=None,
            repository="" if snapshot is None else snapshot.repository_name,
            project_title="" if snapshot is None else snapshot.project_title,
        )

    @override
    def _answer(self, snapshot: ProjectTargetView | None, project: ProjectRun, *, queued: bool) -> GitHubProjectsStatus:
        return self._live(status_of(cast(ProjectTargetView, snapshot), live=True, queued=queued), project)

    @override
    async def _record_start(self, pid: uuid.UUID, trigger: str, queued_at: datetime) -> None:
        async with self._transaction() as session:
            await projects_db.record_start(session, pid, trigger=trigger, queued_at=queued_at)

    @override
    async def _run(self, project_id: str, pid: uuid.UUID) -> PushResult:
        return await self._push(project_id, self._deps)

    @override
    async def _record_end(self, pid: uuid.UUID, result: PushResult, finished_at: datetime) -> None:
        async with self._transaction() as session:
            await projects_db.record_end(
                session,
                pid,
                status=result.status,
                error_code=result.error_code,
                counts=dataclasses.asdict(result.counts),
                finished_at=finished_at,
            )

    @override
    def _outcome(self, result: PushResult) -> tuple[str, str | None, Mapping[str, Any]]:
        return result.status, result.error_code, dataclasses.asdict(result.counts)

    @override
    async def _running_projects(self) -> list[uuid.UUID]:
        async with self._transaction() as session:
            return await projects_db.running_targets(session)
