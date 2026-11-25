"""Application-role data access for the Notion connections (docs/notion/oauth.md "Data"). Stream C1 owns this file; the
``ConnectionRows`` below are final and complete.

Same rules as ``db.github``: every function runs inside the caller's session and transaction, flushes and never commits;
every statement filters on the user it is given. The refresh token arrives and leaves as Fernet ciphertext; nothing
here logs. ``ConnectionRows`` is the ``notion.connections.ConnectionStore`` of one session; ``connection_store`` turns a
session transaction (the api's ``Runtime.transaction``) into the store transaction ``NotionConnections`` takes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import NotionConnection
from notion.connections import ACTIVE, EXPIRED, StoredConnection, StoreTransaction, TokenGrant


def _stored(row: NotionConnection | None) -> StoredConnection | None:
    if row is None:
        return None
    return StoredConnection(
        user_id=row.user_id,
        workspace_id=row.workspace_id,
        workspace_name=row.workspace_name,
        bot_id=row.bot_id,
        refresh_token=bytes(row.refresh_token),
        status=row.status,
        connected_at=row.connected_at,
        last_used_at=row.last_used_at,
    )


class ConnectionRows:
    """The ``ConnectionStore`` of one session (``notion.connections``)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = select(NotionConnection).where(NotionConnection.user_id == user_id)
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = (
            select(NotionConnection)
            .where(NotionConnection.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def save(
        self,
        user_id: uuid.UUID,
        grant: TokenGrant,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime | None,
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {
            "workspace_id": grant.workspace_id,
            "workspace_name": grant.workspace_name,
            "bot_id": grant.bot_id,
            "refresh_token": refresh_token,
            "refresh_expires_at": refresh_expires_at,
            "status": ACTIVE,
            "connected_at": now,
            "last_used_at": now,
            "expired_at": None,
        }
        statement = pg_insert(NotionConnection).values(user_id=user_id, **values)
        await self._session.execute(statement.on_conflict_do_update(index_elements=("user_id",), set_=values))

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime | None, now: datetime
    ) -> None:
        await self._session.execute(
            update(NotionConnection)
            .where(NotionConnection.user_id == user_id)
            .values(refresh_token=refresh_token, refresh_expires_at=refresh_expires_at, last_used_at=now)
        )

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(NotionConnection).where(NotionConnection.user_id == user_id).values(status=EXPIRED, expired_at=now)
        )

    async def delete(self, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(NotionConnection).where(NotionConnection.user_id == user_id).returning(NotionConnection.user_id)
        )
        return result.first() is not None


def connection_store(transaction: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> StoreTransaction:
    """The store transaction of ``NotionConnections`` over a session transaction (committed on success)."""

    @asynccontextmanager
    async def store() -> AsyncIterator[ConnectionRows]:
        async with transaction() as session:
            yield ConnectionRows(session)

    return store


async def connection_status(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """``active``, ``expired`` or ``None`` (no connection)."""

    statement = select(NotionConnection.status).where(NotionConnection.user_id == user_id)
    return (await session.execute(statement)).scalar_one_or_none()
