"""Application-role data access for the Box connections (docs/box/oauth.md "Data").

Same rules as ``db.github``: every function runs inside the caller's session and transaction, flushes and never
commits; every statement filters on the user it is given. The refresh token arrives and leaves as Fernet ciphertext;
nothing here logs. ``ConnectionRows`` is the ``box.connections.ConnectionStore`` of one session; ``connection_store``
turns a session transaction (the api's ``Runtime.transaction``) into the store transaction ``BoxConnections`` takes.

``ConnectionRows`` is a copy of ``db.github.ConnectionRows`` with the Box columns of ``db.models.BoxConnection``.
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

from box.client import BoxUser
from box.connections import ACTIVE, EXPIRED, StoredConnection, StoreTransaction
from db.models import BoxConnection


def _stored(row: BoxConnection | None) -> StoredConnection | None:
    if row is None:
        return None
    return StoredConnection(
        user_id=row.user_id,
        box_user_id=row.box_user_id,
        name=row.name,
        enterprise_id=row.enterprise_id,
        enterprise_name=row.enterprise_name,
        refresh_token=bytes(row.refresh_token),
        status=row.status,
        connected_at=row.connected_at,
        last_used_at=row.last_used_at,
    )


class ConnectionRows:
    """The ``ConnectionStore`` of one session (``box.connections``)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = select(BoxConnection).where(BoxConnection.user_id == user_id)
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        statement = (
            select(BoxConnection)
            .where(BoxConnection.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def save(
        self,
        user_id: uuid.UUID,
        user: BoxUser,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime,
        now: datetime,
    ) -> None:
        values: dict[str, Any] = {
            "box_user_id": user.id,
            "name": user.name,
            "enterprise_id": user.enterprise_id,
            "enterprise_name": user.enterprise_name,
            "refresh_token": refresh_token,
            "refresh_expires_at": refresh_expires_at,
            "status": ACTIVE,
            "connected_at": now,
            "last_used_at": now,
            "expired_at": None,
        }
        statement = pg_insert(BoxConnection).values(user_id=user_id, **values)
        await self._session.execute(statement.on_conflict_do_update(index_elements=("user_id",), set_=values))

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime, now: datetime
    ) -> None:
        await self._session.execute(
            update(BoxConnection)
            .where(BoxConnection.user_id == user_id)
            .values(refresh_token=refresh_token, refresh_expires_at=refresh_expires_at, last_used_at=now)
        )

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        await self._session.execute(
            update(BoxConnection).where(BoxConnection.user_id == user_id).values(status=EXPIRED, expired_at=now)
        )

    async def delete(self, user_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            delete(BoxConnection).where(BoxConnection.user_id == user_id).returning(BoxConnection.user_id)
        )
        return result.first() is not None


def connection_store(transaction: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> StoreTransaction:
    """The store transaction of ``BoxConnections`` over a session transaction (committed on success)."""

    @asynccontextmanager
    async def store() -> AsyncIterator[ConnectionRows]:
        async with transaction() as session:
            yield ConnectionRows(session)

    return store
