"""Application-role data access for the Linear connections (``linear_connections``, migration ``0011_linear``).

Same rules as ``db.github``: every function runs inside the caller's session and transaction, flushes and never commits;
every statement filters on the user it is given. Both tokens arrive and leave as Fernet ciphertext; nothing here logs.
``ConnectionRows`` is the ``linear.connections.ConnectionStore`` of one session; ``connection_store`` turns a
session transaction (the api's ``Runtime.transaction``) into the store transaction ``LinearConnections`` takes.
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

from db.models import LinearConnection
from linear.connections import ACTIVE, EXPIRED, StoredConnection, StoreTransaction
from linear.models import Viewer


def _stored(row: LinearConnection | None) -> StoredConnection | None:
    if row is None:
        return None
    return StoredConnection(
        user_id=row.user_id,
        linear_user_id=row.linear_user_id,
        linear_user_name=row.linear_user_name,
        organization_id=row.organization_id,
        organization_name=row.organization_name,
        organization_url_key=row.organization_url_key,
        scopes=row.scopes,
        access_token=bytes(row.access_token),
        refresh_token=bytes(row.refresh_token),
        expires_at=row.expires_at,
        status=row.status,
        connected_at=row.connected_at,
        last_used_at=row.last_used_at,
    )


class ConnectionRows:
    """The ``ConnectionStore`` of one session (``linear.connections``)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = select(LinearConnection).where(LinearConnection.user_id == user_id)
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = (
            select(LinearConnection)
            .where(LinearConnection.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def save(
        self,
        user_id: uuid.UUID,
        viewer: Viewer,
        *,
        scopes: str,
        tokens: tuple[bytes, bytes],
        expires_at: datetime,
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {
            "linear_user_id": viewer.id,
            "linear_user_name": viewer.name,
            "organization_id": viewer.organization.id,
            "organization_name": viewer.organization.name,
            "organization_url_key": viewer.organization.url_key,
            "scopes": scopes,
            "access_token": tokens[0],
            "refresh_token": tokens[1],
            "expires_at": expires_at,
            "status": ACTIVE,
            "connected_at": now,
            "last_used_at": now,
            "expired_at": None,
        }
        statement = pg_insert(LinearConnection).values(user_id=user_id, **values)
        await self._session.execute(statement.on_conflict_do_update(index_elements=("user_id",), set_=values))

    async def rotate(
        self, user_id: uuid.UUID, *, tokens: tuple[bytes, bytes], expires_at: datetime, now: datetime
    ) -> None:
        await self._session.execute(
            update(LinearConnection)
            .where(LinearConnection.user_id == user_id)
            .values(access_token=tokens[0], refresh_token=tokens[1], expires_at=expires_at, last_used_at=now)
        )

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(LinearConnection).where(LinearConnection.user_id == user_id).values(status=EXPIRED, expired_at=now)
        )

    async def delete(self, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(LinearConnection).where(LinearConnection.user_id == user_id).returning(LinearConnection.user_id)
        )
        return result.first() is not None


def connection_store(transaction: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> StoreTransaction:
    """The store transaction of ``LinearConnections`` over a session transaction (committed on success)."""

    @asynccontextmanager
    async def store() -> AsyncIterator[ConnectionRows]:
        async with transaction() as session:
            yield ConnectionRows(session)

    return store
