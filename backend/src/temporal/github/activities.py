"""The activities of the clone check (docs/github/clone.md): one does the whole check, one marks a row failed.

``github_snapshot`` records every closed failure on the row itself and returns its code, so a closed failure is never
retried. An unexpected error is retried once; on the last attempt it ends the row ``failed`` with
``github_clone_failed``, so no row stays ``cloning``. History, logs and errors carry ids and codes only: never the
token, the repository name, a GitHub text or git's stderr.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from db.github import Snapshot, fail_clone, finish_clone, lock_repository, start_clone
from github.codes import CLONE_FAILURE_CODES, GitHubCode, GitHubError
from github.connections import GitHubConnections
from logger.logger import ERROR, INFO, WARNING, get_logger, log_event
from storage import ObjectStore
from storage.keys import repository_snapshot
from temporal.containment import Contain
from temporal.github import clone as git
from temporal.github.models import GitHubSnapshotFailInput, GitHubSnapshotInput, GitHubSnapshotResult
from temporal.github.workflows import FAIL_ACTIVITY, SNAPSHOT_ACTIVITY, SNAPSHOT_MAX_ATTEMPTS

logger = get_logger("temporal.github.activities")

HEARTBEAT_INTERVAL_SECONDS: Final = 20.0
UPLOAD_CHUNK_BYTES: Final = 8 * 1024 * 1024
_SNAPSHOT_FILE: Final = "snapshot.tar.gz"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Target:
    """What the check needs from the row: whose token to use and which GitHub repository."""

    added_by: uuid.UUID | None
    github_repo_id: int


class SnapshotRows(Protocol):
    """The row changes of a check, each in its own short transaction."""

    async def start(self, project_id: uuid.UUID, repository_id: uuid.UUID) -> Target | None:
        """``cloning``; ``None`` when the repository is gone."""

    async def finish(
        self, project_id: uuid.UUID, repository_id: uuid.UUID, snapshot: Snapshot, *, now: datetime
    ) -> tuple[bool, str | None]:
        """``cloned``: (the row still exists, the snapshot key it replaced)."""

    async def fail(self, project_id: uuid.UUID, repository_id: uuid.UUID, code: str, *, now: datetime) -> bool:
        """``failed`` with ``code`` (the last good snapshot stays); false when the repository is gone."""


class SessionSnapshotRows:
    """``SnapshotRows`` over the application role's session factory (``db.github``)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session, session.begin():
            yield session

    async def start(self, project_id: uuid.UUID, repository_id: uuid.UUID) -> Target | None:
        async with self._transaction() as session:
            row = await lock_repository(session, project_id, repository_id)
            if row is None or not await start_clone(session, project_id, repository_id):
                return None
            return Target(added_by=row.added_by, github_repo_id=row.github_repo_id)

    async def finish(
        self, project_id: uuid.UUID, repository_id: uuid.UUID, snapshot: Snapshot, *, now: datetime
    ) -> tuple[bool, str | None]:
        async with self._transaction() as session:
            if await lock_repository(session, project_id, repository_id) is None:
                return False, None
            return True, await finish_clone(session, project_id, repository_id, snapshot, now=now)

    async def fail(self, project_id: uuid.UUID, repository_id: uuid.UUID, code: str, *, now: datetime) -> bool:
        async with self._transaction() as session:
            return await fail_clone(session, project_id, repository_id, code, now=now)


class _CheckFailedError(Exception):
    """A closed failure of the check; ``code`` goes to the row."""

    def __init__(self, code: GitHubCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class SnapshotOptions:
    """The seams of the check; production uses the defaults (tests inject a local remote, a fake git or a clock)."""

    remote: Callable[[str], git.Remote] = git.github_remote
    runner: git.GitRunner = git.run_git
    clone_seconds: float = git.CLONE_TIMEOUT_SECONDS
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS
    temp_root: str | None = None  # None = TMPDIR (a disk volume on worker-agents)
    now: Callable[[], datetime] = _utc_now


class GitHubSnapshotActivities:
    """The clone check over one row store, object store and ``GitHubConnections`` (``None``: GitHub is not
    configured on this worker, every check ends ``github_auth_failed``)."""

    def __init__(
        self,
        rows: SnapshotRows,
        store: ObjectStore,
        connections: GitHubConnections | None,
        options: SnapshotOptions | None = None,
    ) -> None:
        self._rows = rows
        self._store = store
        self._connections = connections
        self._options = options or SnapshotOptions()

    @property
    def activities(self) -> list[Callable[..., Any]]:
        return [self.github_snapshot, self.github_snapshot_fail]

    @asynccontextmanager
    async def _heartbeating(self) -> AsyncIterator[None]:
        async def beat() -> None:
            while True:
                activity.heartbeat()
                await asyncio.sleep(self._options.heartbeat_interval)

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @activity.defn(name=SNAPSHOT_ACTIVITY)
    async def github_snapshot(self, data: GitHubSnapshotInput) -> GitHubSnapshotResult:
        """The whole check; a closed failure is recorded and returned, never raised."""

        started = time.monotonic()
        ids: dict[str, Any] = {"project_id": str(data.project_id), "repository_id": str(data.repository_id)}
        last_attempt = activity.info().attempt >= SNAPSHOT_MAX_ATTEMPTS
        with Contain() as contained:  # never BaseException: a cancellation propagates, the workflow ends the row
            try:
                async with self._heartbeating():
                    return await self._check(data, ids, started)
            except _CheckFailedError as failure:
                return await self._record_failure(data, ids, failure.code, started)
        log_event(
            logger, ERROR, "worker.github.snapshot.error", **ids, error_type=contained.error_type, last=last_attempt
        )
        if last_attempt:
            return await self._record_failure(data, ids, GitHubCode.CLONE_FAILED, started)
        raise ApplicationError("internal_error", type="GitHubSnapshotError") from None

    @activity.defn(name=FAIL_ACTIVITY)
    async def github_snapshot_fail(self, data: GitHubSnapshotFailInput) -> bool:
        """The workflow's last resort (a timed-out, crashed or cancelled check): the row ``failed``."""

        code = data.code if data.code in CLONE_FAILURE_CODES else GitHubCode.CLONE_FAILED.value
        return await self._rows.fail(data.project_id, data.repository_id, code, now=self._options.now())

    # Inside --------------------------------------------------------------------------------------------------------

    async def _record_failure(
        self, data: GitHubSnapshotInput, ids: dict[str, Any], code: GitHubCode, started: float
    ) -> GitHubSnapshotResult:
        found = await self._rows.fail(data.project_id, data.repository_id, code.value, now=self._options.now())
        log_event(
            logger,
            WARNING,
            "worker.github.snapshot.failed",
            **ids,
            code=code.value,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return GitHubSnapshotResult(code=code.value, ended=found)

    async def _check(self, data: GitHubSnapshotInput, ids: dict[str, Any], started: float) -> GitHubSnapshotResult:
        target = await self._rows.start(data.project_id, data.repository_id)
        if target is None:
            log_event(logger, INFO, "worker.github.snapshot.gone", **ids)
            return GitHubSnapshotResult(code=None, ended=False)
        log_event(logger, INFO, "worker.github.snapshot.started", **ids)
        if self._connections is None:
            log_event(logger, WARNING, "worker.github.snapshot.unconfigured", **ids)
            raise _CheckFailedError(GitHubCode.AUTH_FAILED)
        if target.added_by is None:
            raise _CheckFailedError(GitHubCode.NOT_CONNECTED)
        token, full_name = await _access(self._connections, target.added_by, target.github_repo_id)
        with tempfile.TemporaryDirectory(prefix="gh-snapshot-", dir=self._options.temp_root) as workdir:
            work = Path(workdir)
            cloned = await self._clone(full_name, token, work)
            stats = await asyncio.to_thread(git.tree_stats, cloned.path)
            try:
                git.check_limits(stats)
            except GitHubError as error:
                raise _CheckFailedError(error.code) from None
            archive = work / _SNAPSHOT_FILE
            await asyncio.to_thread(git.write_snapshot, cloned.path, archive)
            key = repository_snapshot(str(data.project_id), str(data.repository_id), cloned.head_sha)
            stored = await self._upload(archive, key)
        snapshot = Snapshot(
            default_branch=cloned.branch,
            head_sha=cloned.head_sha,
            file_count=stats.file_count,
            size_bytes=stats.size_bytes,
            snapshot_key=key,
            snapshot_bytes=stored,
        )
        found, replaced = await self._rows.finish(
            data.project_id, data.repository_id, snapshot, now=self._options.now()
        )
        if not found:  # removed while it was cloned: the new snapshot has no owner
            await self._delete(key, ids)
            return GitHubSnapshotResult(code=None, ended=False)
        if replaced is not None:
            await self._delete(replaced, ids)
        log_event(
            logger,
            INFO,
            "worker.github.snapshot.finished",
            **ids,
            file_count=stats.file_count,
            size_bytes=stats.size_bytes,
            links_skipped=stats.links_skipped,
            snapshot_bytes=stored,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return GitHubSnapshotResult(code=None, ended=True)

    async def _clone(self, full_name: str, token: str, work: Path) -> git.Clone:
        options = self._options
        try:
            remote = options.remote(full_name)
            return await git.clone(remote, token, work, runner=options.runner, seconds=options.clone_seconds)
        except GitHubError as error:
            raise _CheckFailedError(_clone_code(error.code)) from None

    async def _upload(self, archive: Path, key: str) -> int:
        """Stream the archive to the object store; any store error is ``storage_failed`` (the upload is aborted)."""

        with Contain() as opening:
            writer = await self._store.open_writer(key, content_type=git.SNAPSHOT_CONTENT_TYPE)
        if opening.error is not None:
            raise _CheckFailedError(GitHubCode.STORAGE_FAILED) from None
        stored = None
        try:
            with Contain() as writing:
                with archive.open("rb") as content:
                    while chunk := await asyncio.to_thread(content.read, UPLOAD_CHUNK_BYTES):
                        await writer.write(chunk)
                stored = await writer.close()
        finally:
            if stored is None:  # a store error or a cancellation: no half object stays
                with suppress(Exception):
                    await writer.abort()
        if writing.error is not None or stored is None:
            raise _CheckFailedError(GitHubCode.STORAGE_FAILED) from None
        return stored.size

    async def _delete(self, key: str, ids: dict[str, Any]) -> None:
        """Best effort: a left-over snapshot is removed with the repository's prefix later."""

        with Contain() as contained:
            await self._store.delete(key)
        if contained.error is not None:
            log_event(logger, WARNING, "worker.github.snapshot.delete_failed", **ids, error_type=contained.error_type)


async def _access(connections: GitHubConnections, user_id: uuid.UUID, github_repo_id: int) -> tuple[str, str]:
    """The user's token and the repository's current ``owner/name``, read through that token."""

    try:
        token = await connections.access_token(user_id)
        repository = await connections.repository(user_id, github_repo_id)
    except GitHubError as error:
        raise _CheckFailedError(_clone_code(error.code)) from None
    if repository.too_large:
        raise _CheckFailedError(GitHubCode.REPO_TOO_LARGE)
    # ``repository`` may have refreshed the token after a 401: take the current one.
    with suppress(GitHubError):
        token = await connections.access_token(user_id)
    return token, repository.full_name


def _clone_code(code: GitHubCode) -> GitHubCode:
    """A code of the connection or the clone as a clone failure code (an internal code becomes a public one)."""

    if code is GitHubCode.GRANT_INVALID:
        return GitHubCode.RECONNECT_REQUIRED
    if code in (GitHubCode.REFUSED, GitHubCode.CODE_INVALID, GitHubCode.STATE_INVALID):
        return GitHubCode.AUTH_FAILED
    return code if code.value in CLONE_FAILURE_CODES else GitHubCode.CLONE_FAILED
