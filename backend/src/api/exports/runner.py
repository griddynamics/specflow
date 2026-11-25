"""The in-process export runner: one drain task per project, one pass at a time in the process.

Destination-neutral. A destination (``api.jira.runner.JiraSyncRunner``) subclasses ``ExportRunner`` and fills in the
hooks: how its target is read, how a pass starts, runs and ends in storage, and what its status looks like.

Rules (docs/jira-sync/sync.md#triggers-and-runs): a start while the project's pass is queued or runs sets a flag and the
pass is repeated once when it ends (starts are merged, never dropped). A pass records ``running`` as soon as it is
queued, before it waits for the process-wide slot, so a restart recovers a waiting pass too. Logs carry ids, codes and
counts, under the destination's event prefix.

A start made while the pass waits for the slot also schedules one more pass: this is intended, it keeps the ``queued``
answer true.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Final

from api.contained import Contained
from logger.logger import INFO, WARNING, log_event

MAX_CONCURRENT_PASSES: Final = 1  # passes at once in this process; the other projects wait


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ProjectRun:
    """What the runner knows of one project's pass in this process."""

    trigger: str  # the trigger of the next pass (the latest start; a merge overwrites it)
    queued_at: datetime  # when the current pass was queued
    pass_trigger: str  # the trigger of the current pass (what a live status shows)
    progress: Any = None  # the running pass's latest progress; in memory only
    running: bool = False  # a pass of the project is queued or runs here (the status reads ``running``)
    dirty: bool = False  # a start came in meanwhile: one more pass follows
    task: asyncio.Task[None] | None = None


class ExportRunner[Snapshot, Status, Result](ABC):
    """Starts, merges and tracks the passes of one destination in this process.

    ``Snapshot`` is what a start reads of the target, ``Status`` what a start answers, ``Result`` what a pass returns.
    """

    logger: ClassVar[Any]  # the destination's ``logger.logger.get_logger`` logger
    label: ClassVar[
        str
    ]  # ``jira sync``: the closed error reads ``the jira sync runner is closed``, tasks ``jira-sync-<id>``
    event_prefix: ClassVar[str]  # for example ``api.jira.sync``: the events are ``<prefix>.queued``, ``.merged`` ...

    def __init__(self, *, closed_error: type[RuntimeError], now: Any = utc_now) -> None:
        self._closed_error = closed_error
        self._now = now
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_PASSES)
        self._projects: dict[str, ProjectRun] = {}
        self._recovery: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def _task_prefix(self) -> str:
        return self.label.replace(" ", "-")

    # Hooks ---------------------------------------------------------------------

    @abstractmethod
    async def _read(self, project_id: str) -> Snapshot:
        """The target of the project (and what its status needs), read before a start."""

    @abstractmethod
    def _enabled(self, snapshot: Snapshot) -> bool: ...

    @abstractmethod
    def _skipped(self, project_id: str, snapshot: Snapshot) -> Status:
        """The answer of a start with no enabled target: nothing was queued."""

    @abstractmethod
    def _answer(self, snapshot: Snapshot, project: ProjectRun, *, queued: bool) -> Status:
        """The live status a start answers (``queued``: it merged into a pass that was already there)."""

    @abstractmethod
    async def _record_start(self, pid: uuid.UUID, trigger: str, queued_at: datetime) -> None: ...

    @abstractmethod
    async def _run(self, project_id: str, pid: uuid.UUID) -> Result:
        """One pass, with the process-wide slot held."""

    @abstractmethod
    async def _record_end(self, pid: uuid.UUID, result: Result, finished_at: datetime) -> None: ...

    @abstractmethod
    def _outcome(self, result: Result) -> tuple[str, str | None, Mapping[str, Any]]:
        """``(status, error_code, counts)`` of a result, for the log."""

    @abstractmethod
    async def _running_projects(self) -> list[uuid.UUID]:
        """The projects whose stored run a stopped process left ``running`` and that are still enabled."""

    # Public ------------------------------------------------------------------

    async def start(self, project_id: str, trigger: str, *, user_id: str | None = None) -> Status:
        """Queue a pass of the project, or merge into the one that is queued or runs (``queued`` true).

        A project without an enabled target starts nothing. ``user_id`` only goes to the log.
        """

        self._open()
        snapshot = await self._read(project_id)
        self._open()  # ``aclose`` ran during the read: no pass may be spawned after it collected the tasks
        if not self._enabled(snapshot):
            return self._skipped(project_id, snapshot)
        prefix = self.event_prefix
        project = self._projects.get(project_id)
        if project is not None and project.running:
            project.dirty = True
            project.trigger = trigger
            log_event(self.logger, INFO, f"{prefix}.merged", project_id=project_id, trigger=trigger, user_id=user_id)
            return self._answer(snapshot, project, queued=True)
        project = ProjectRun(trigger=trigger, queued_at=self._now(), pass_trigger=trigger, running=True)
        self._projects[project_id] = project
        project.task = asyncio.create_task(self._drain(project_id, project), name=f"{self._task_prefix}-{project_id}")
        log_event(self.logger, INFO, f"{prefix}.queued", project_id=project_id, trigger=trigger, user_id=user_id)
        return self._answer(snapshot, project, queued=False)

    async def after_plan(self, project_id: str) -> None:
        """The plan run returned: start a pass when the project has an enabled target. Never raises."""

        await self.start_contained(project_id, "plan_run")

    def live(self, project_id: str) -> tuple[bool, bool]:
        """``(running, queued)``: a pass is queued or runs here; a start is merged into one more."""

        project = self._projects.get(project_id)
        running = project is not None and project.running
        return running, running and project is not None and project.dirty

    def report(self, project_id: str, progress: Any) -> None:
        """Keep the running pass's latest progress for its live status."""

        project = self._projects.get(project_id)
        if project is not None and project.running:
            project.progress = progress

    def start_recovery(self) -> None:
        """Restart, in the background, the passes a stopped process left ``running``; a failure is logged only."""

        self._recovery = asyncio.create_task(self._recover(), name=f"{self._task_prefix}-recovery")

    async def aclose(self) -> None:
        """Cancel the queued and running passes (the next start repeats them)."""

        self._closed = True
        tasks = [project.task for project in self._projects.values() if project.task is not None]
        if self._recovery is not None:
            tasks.append(self._recovery)
        tasks = [task for task in tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Starting ------------------------------------------------------------------

    def _open(self) -> None:
        if self._closed:
            raise self._closed_error(f"the {self.label} runner is closed")

    async def start_contained(self, project_id: str, trigger: str) -> None:
        with Contained() as started:
            await self.start(project_id, trigger)
        if started.failed:
            log_event(
                self.logger,
                WARNING,
                f"{self.event_prefix}.start_failed",
                project_id=project_id,
                trigger=trigger,
                error_type=started.error_type,
            )

    async def _recover(self) -> None:
        with Contained() as recovered:
            for project_id in await self._running_projects():
                await self.start_contained(str(project_id), "recovery")
        if recovered.failed:
            log_event(
                self.logger,
                WARNING,
                f"{self.event_prefix}.start_failed",
                project_id=None,
                trigger="recovery",
                error_type=recovered.error_type,
            )

    # The pass ------------------------------------------------------------------

    async def _drain(self, project_id: str, project: ProjectRun) -> None:
        try:
            while True:
                project.dirty = False
                project.pass_trigger = project.trigger
                await self._pass(project_id, project)
                if not project.dirty:
                    break
                project.queued_at = self._now()
        finally:
            project.progress = None
            project.running = False  # no await between the last check and here: a start cannot slip in

    async def _pass(self, project_id: str, project: ProjectRun) -> None:
        trigger = project.pass_trigger
        pid = uuid.UUID(project_id)
        prefix = self.event_prefix
        project.progress = None
        with Contained() as ran:  # a failed pass never stops the project's later ones
            await self._record_start(pid, trigger, project.queued_at)
            async with self._slots:
                log_event(self.logger, INFO, f"{prefix}.started", project_id=project_id, trigger=trigger)
                result = await self._run(project_id, pid)
            await self._record_end(pid, result, self._now())
            status, error_code, counts = self._outcome(result)
            log_event(
                self.logger,
                INFO,
                f"{prefix}.finished",
                project_id=project_id,
                status=status,
                error_code=error_code,
                counts=counts,  # flat names would collide with LogRecord's ``created``
            )
        project.progress = None
        if ran.failed:  # the pass logs its own unexpected errors; this is the runner's (storage, a stub)
            log_event(
                self.logger,
                WARNING,
                f"{prefix}.failed",
                project_id=project_id,
                error_code="unexpected_error",
                error_type=ran.error_type,
            )
