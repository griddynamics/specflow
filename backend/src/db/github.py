"""Application-role data access for the GitHub connections and the project repositories (docs/github/data-model.md).

Same rules as ``db.atlassian``: every function runs inside the caller's session and transaction, flushes and never
commits; every statement filters on the user or the project it is given. The refresh token arrives and leaves as
Fernet ciphertext; nothing here logs. ``ConnectionRows`` is the ``github.connections.ConnectionStore`` of one session;
``connection_store`` turns a session transaction (the api's ``Runtime.transaction``, a worker's session factory)
into the store transaction ``GitHubConnections`` takes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import GitHubConnection, ProjectRepository, User
from github.client import GitHubUser, Repository
from github.connections import ACTIVE, EXPIRED, StoredConnection, StoreTransaction

PENDING: Final = "pending"
CLONING: Final = "cloning"
CLONED: Final = "cloned"
FAILED: Final = "failed"


# Connections ---------------------------------------------------------------------------------------------------------


def _stored(row: GitHubConnection | None) -> StoredConnection | None:
    if row is None:
        return None
    return StoredConnection(
        user_id=row.user_id,
        github_user_id=row.github_user_id,
        login=row.login,
        refresh_token=bytes(row.refresh_token),
        status=row.status,
        connected_at=row.connected_at,
        last_used_at=row.last_used_at,
    )


class ConnectionRows:
    """The ``ConnectionStore`` of one session (``github.connections``)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = select(GitHubConnection).where(GitHubConnection.user_id == user_id)
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = (
            select(GitHubConnection)
            .where(GitHubConnection.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def save(
        self,
        user_id: uuid.UUID,
        user: GitHubUser,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime | None,
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {
            "github_user_id": user.id,
            "login": user.login,
            "refresh_token": refresh_token,
            "refresh_expires_at": refresh_expires_at,
            "status": ACTIVE,
            "connected_at": now,
            "last_used_at": now,
            "expired_at": None,
        }
        statement = pg_insert(GitHubConnection).values(user_id=user_id, **values)
        await self._session.execute(statement.on_conflict_do_update(index_elements=("user_id",), set_=values))

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime | None, now: datetime
    ) -> None:
        await self._session.execute(
            update(GitHubConnection)
            .where(GitHubConnection.user_id == user_id)
            .values(refresh_token=refresh_token, refresh_expires_at=refresh_expires_at, last_used_at=now)
        )

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(GitHubConnection).where(GitHubConnection.user_id == user_id).values(status=EXPIRED, expired_at=now)
        )

    async def delete(self, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(GitHubConnection).where(GitHubConnection.user_id == user_id).returning(GitHubConnection.user_id)
        )
        return result.first() is not None


def connection_store(transaction: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> StoreTransaction:
    """The store transaction of ``GitHubConnections`` over a session transaction (committed on success)."""

    @asynccontextmanager
    async def store() -> AsyncIterator[ConnectionRows]:
        async with transaction() as session:
            yield ConnectionRows(session)

    return store


def session_transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """One session and one transaction of ``session_factory`` (committed on success): what a worker hands to
    ``connection_store``."""

    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    return transaction


async def connection_status(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """``active``, ``expired`` or ``None`` (no connection)."""

    statement = select(GitHubConnection.status).where(GitHubConnection.user_id == user_id)
    return (await session.execute(statement)).scalar_one_or_none()


# Project repositories ------------------------------------------------------------------------------------------------


class RepositoryExistsError(Exception):
    """The project already has this GitHub repository."""


@dataclass(frozen=True)
class RepositoryRow:
    """A project repository with the display name of the user who added it (``None`` once they are gone)."""

    repository: ProjectRepository
    added_by_name: str | None


def _with_adder() -> Any:
    return select(ProjectRepository, User.display_name).outerjoin(User, User.id == ProjectRepository.added_by)


async def list_repositories(session: AsyncSession, project_id: uuid.UUID) -> list[RepositoryRow]:
    statement = (
        _with_adder()
        .where(ProjectRepository.project_id == project_id)
        .order_by(ProjectRepository.full_name, ProjectRepository.id)
    )
    return [RepositoryRow(row, name) for row, name in (await session.execute(statement)).all()]


async def get_repository(
    session: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID
) -> RepositoryRow | None:
    statement = _with_adder().where(ProjectRepository.project_id == project_id, ProjectRepository.id == repository_id)
    found = (await session.execute(statement)).first()
    return None if found is None else RepositoryRow(found[0], found[1])


async def add_repository(
    session: AsyncSession, project_id: uuid.UUID, repository: Repository, *, added_by: uuid.UUID
) -> uuid.UUID:
    """Insert the repository ``pending``; ``RepositoryExistsError`` when the project has it already."""

    statement = (
        pg_insert(ProjectRepository)
        .values(
            id=uuid.uuid4(),
            project_id=project_id,
            github_repo_id=repository.id,
            full_name=repository.full_name,
            html_url=repository.html_url,
            private=repository.private,
            default_branch=repository.default_branch,
            added_by=added_by,
            clone_state=PENDING,
        )
        .on_conflict_do_nothing(constraint="uq_project_repositories_project_repo")
        .returning(ProjectRepository.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is None:
        raise RepositoryExistsError
    return inserted


async def delete_repository(session: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID) -> bool:
    result = await session.execute(
        delete(ProjectRepository)
        .where(ProjectRepository.project_id == project_id, ProjectRepository.id == repository_id)
        .returning(ProjectRepository.id)
    )
    return result.first() is not None


async def request_clone(session: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID) -> bool:
    """Mark the repository ``pending`` (clearing a failure) unless a check is ``cloning`` now; true when it exists."""

    statement = (
        update(ProjectRepository)
        .where(
            ProjectRepository.project_id == project_id,
            ProjectRepository.id == repository_id,
            ProjectRepository.clone_state != CLONING,
        )
        .values(clone_state=PENDING, failure_code=None)
        .returning(ProjectRepository.id)
    )
    if (await session.execute(statement)).first() is not None:
        return True
    return await get_repository(session, project_id, repository_id) is not None


async def lock_repository(
    session: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID
) -> ProjectRepository | None:
    """The row locked ``FOR UPDATE`` (a clone check holds it while it changes the state)."""

    statement = (
        select(ProjectRepository)
        .where(ProjectRepository.project_id == project_id, ProjectRepository.id == repository_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def start_clone(session: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID) -> bool:
    """``cloning``; false when the repository is gone (removed meanwhile)."""

    statement = (
        update(ProjectRepository)
        .where(ProjectRepository.project_id == project_id, ProjectRepository.id == repository_id)
        .values(clone_state=CLONING, failure_code=None)
        .returning(ProjectRepository.id)
    )
    return (await session.execute(statement)).first() is not None


@dataclass(frozen=True)
class Snapshot:
    """What a successful clone check found and stored."""

    default_branch: str | None
    head_sha: str
    file_count: int
    size_bytes: int
    snapshot_key: str
    snapshot_bytes: int


async def finish_clone(
    session: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID, snapshot: Snapshot, *, now: datetime
) -> str | None:
    """``cloned`` with the new snapshot; the key of the snapshot it replaced (to delete), ``None`` when there was none
    or the repository is gone."""

    row = await lock_repository(session, project_id, repository_id)
    if row is None:
        return None
    previous = row.snapshot_key
    row.clone_state, row.failure_code = CLONED, None
    row.default_branch, row.head_sha = snapshot.default_branch, snapshot.head_sha
    row.file_count, row.size_bytes = snapshot.file_count, snapshot.size_bytes
    row.snapshot_key, row.snapshot_bytes = snapshot.snapshot_key, snapshot.snapshot_bytes
    row.checked_at = row.cloned_at = now
    await session.flush()
    return previous if previous != snapshot.snapshot_key else None


async def fail_clone(
    session: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID, code: str, *, now: datetime
) -> bool:
    """``failed`` with ``code``; the last good snapshot stays. False when the repository is gone."""

    statement = (
        update(ProjectRepository)
        .where(ProjectRepository.project_id == project_id, ProjectRepository.id == repository_id)
        .values(clone_state=FAILED, failure_code=code, checked_at=now)
        .returning(ProjectRepository.id)
    )
    return (await session.execute(statement)).first() is not None
