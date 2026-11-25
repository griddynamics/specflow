"""The in-process Notion plan runner: the Notion side of ``api.exports.runner.ExportRunner``. Stream E1 owns this file;
the hooks are final and complete, ``push_once`` (``api.notion_plan.sync``) is what E1 fills.

The runner (per-project drain, merged starts, one pass at a time, ``running`` before the slot, startup recovery) is
``api.exports.runner``; this class says how the target is read, how a pass is recorded and run (as the member the
project's Notion is connected by), and what a status looks like. Rules: docs/notion/plan.md "Runs".
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, cast, override

from api.exports.runner import ExportRunner, ProjectRun, utc_now
from api.exports.status import run_state
from api.notion_plan.store import DbStore, PlanStore
from api.notion_plan.sync import NOT_CONFIGURED, PushDeps, PushResult, push_once
from api.plan_graph import PlanGraph
from api.schemas import NotionPlanCounts, NotionPlanState, NotionPlanStatus
from db import integrations as integrations_db
from db import notion_plan as plan_db
from db.integrations import NOTION
from db.notion_plan import PlanTargetView, TargetSaved
from logger.logger import get_logger
from notion.connections import NotionConnections
from notion.gateway import NotionGateway
from notion.reasons import NotionCode, NotionError

logger = get_logger("api.notion_plan.runner")


class NotionPlanClosedError(RuntimeError):
    """An export was requested after the runner was closed."""


def status_of(target: PlanTargetView, *, live: bool, queued: bool) -> NotionPlanStatus:
    """The export status of a target; ``live``: a pass is queued or runs here, ``queued``: another follows."""

    run = run_state(target, live=live, not_configured=NOT_CONFIGURED)
    counts = NotionPlanCounts(**run.counts) if run.counts else None
    return NotionPlanStatus(
        project_id=str(target.project_id),
        enabled=target.enabled,
        status=cast(NotionPlanState, run.status),
        queued=live and queued,
        trigger=target.last_trigger,
        started_at=target.last_started_at,
        finished_at=run.finished_at,
        error_code=run.error_code,
        counts=counts,
        parent_page_title=target.parent_page_title,
    )


class NotionPlanRunner(ExportRunner[PlanTargetView | None, NotionPlanStatus, PushResult]):
    """Starts, merges and tracks the Notion plan passes of this process; held on ``Runtime.notion_plan``."""

    logger = logger
    label = "notion plan"
    event_prefix = "api.notion_plan.sync"

    def __init__(
        self,
        connections: NotionConnections,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        graph_store: Any,
        now: Callable[[], datetime] = utc_now,
        push: Callable[[str, PushDeps], Awaitable[PushResult]] = push_once,
    ) -> None:
        super().__init__(closed_error=NotionPlanClosedError, now=now)
        self._transaction = transaction
        graph = PlanGraph(graph_store)
        self._push = push
        self._connections = connections
        self._store: PlanStore = DbStore(transaction)
        self._deps = PushDeps(
            store=self._store,
            gateway_for=self._gateway_for,
            load_plan=graph.load_plan,
            spec_names=graph.spec_names,
            restart=self._restart,
            now=now,
        )

    async def gateway_of(self, user_id: uuid.UUID) -> NotionGateway:
        """The gateway acting as ``user_id`` (the member the project's Notion is connected by)."""

        return await self._connections.gateway(user_id)

    async def _gateway_for(self, project_id: uuid.UUID) -> NotionGateway:
        """The gateway of the member the project's Notion is bound to now (rule R1), not the one who saved the target: a
        removed member, a removed integration and a take-over all take effect on the next pass. No binding is
        ``notion_not_connected``; an expired connection is ``notion_reconnect_required`` at the first call."""

        async with self._transaction() as session:
            bound = await integrations_db.get_integration(session, project_id, NOTION)
        if bound is None or bound.user_id is None:
            raise NotionError(NotionCode.NOT_CONNECTED)
        return await self.gateway_of(bound.user_id)

    async def _restart(self, project_id: str) -> None:
        """Rows are left: the start merges into the running pass (``dirty``) and one more pass follows (N9)."""

        await self.start(project_id, "plan_run")

    async def after_settings(self, project_id: str, saved: TargetSaved) -> None:
        """The destination was saved: a pass when it was turned on or changed. Never raises."""

        if saved.turned_on or saved.keys_changed:
            await self.start_contained(project_id, "settings")

    def status_from(self, target: PlanTargetView) -> NotionPlanStatus:
        running, queued = self.live(str(target.project_id))
        status = status_of(target, live=running, queued=queued)
        project = self._projects.get(str(target.project_id))
        return self._live(status, project) if running and project is not None else status

    @staticmethod
    def _live(status: NotionPlanStatus, project: ProjectRun) -> NotionPlanStatus:
        return status.model_copy(update={"trigger": project.pass_trigger, "started_at": project.queued_at})

    # The hooks of the runner ---------------------------------------------------

    @override
    async def _read(self, project_id: str) -> PlanTargetView | None:
        async with self._transaction() as session:
            return await plan_db.get_target(session, uuid.UUID(project_id))

    @override
    def _enabled(self, snapshot: PlanTargetView | None) -> bool:
        return snapshot is not None and snapshot.enabled

    @override
    def _skipped(self, project_id: str, snapshot: PlanTargetView | None) -> NotionPlanStatus:
        return NotionPlanStatus(
            project_id=project_id,
            enabled=False,
            status="skipped",
            queued=False,
            trigger=None,
            started_at=None,
            finished_at=None,
            error_code=NOT_CONFIGURED,
            counts=None,
            parent_page_title="" if snapshot is None else snapshot.parent_page_title,
        )

    @override
    def _answer(self, snapshot: PlanTargetView | None, project: ProjectRun, *, queued: bool) -> NotionPlanStatus:
        return self._live(status_of(cast(PlanTargetView, snapshot), live=True, queued=queued), project)

    @override
    async def _record_start(self, pid: uuid.UUID, trigger: str, queued_at: datetime) -> None:
        async with self._transaction() as session:
            await plan_db.record_start(session, pid, trigger=trigger, queued_at=queued_at)

    @override
    async def _run(self, project_id: str, pid: uuid.UUID) -> PushResult:
        return await self._push(project_id, self._deps)

    @override
    async def _running_projects(self) -> list[uuid.UUID]:
        async with self._transaction() as session:
            return await plan_db.running_targets(session)

    @override
    async def _record_end(self, pid: uuid.UUID, result: PushResult, finished_at: datetime) -> None:
        async with self._transaction() as session:
            await plan_db.record_end(
                session,
                pid,
                finished_at=finished_at,
                counts=dataclasses.asdict(result.counts),
                error_code=result.error_code,
                status=result.status,
            )

    @override
    def _outcome(self, result: PushResult) -> tuple[str, str | None, Mapping[str, Any]]:
        return result.status, result.error_code, dataclasses.asdict(result.counts)
