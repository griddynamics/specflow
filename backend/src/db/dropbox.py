"""Application-role data access for the Dropbox connections (docs/dropbox/oauth.md "Data").

Same rules as ``db.box``: every function runs inside the caller's session and transaction, flushes and never commits;
every statement filters on the user it is given. The refresh token arrives and leaves as Fernet ciphertext; nothing
here logs. ``ConnectionRows`` is the ``dropbox.connections.ConnectionStore`` of one session; ``connection_store`` turns
a session transaction (the api's ``Runtime.transaction``) into the store transaction ``DropboxConnections`` takes.

``ConnectionRows`` is ``db.box.ConnectionRows`` without the ``lock``/``rotate`` pair (Dropbox refresh tokens never
rotate) and with ``touch``.
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

from db.models import DropboxConnection
from dropbox.client import DropboxAccount
from dropbox.connections import ACTIVE, EXPIRED, StoredConnection, StoreTransaction


def _stored(row: DropboxConnection | None) -> StoredConnection | None:
    if row is None:
        return None
    return StoredConnection(
        user_id=row.user_id,
        account_id=row.account_id,
        name=row.name,
        team_id=row.team_id,
        team_name=row.team_name,
        refresh_token=bytes(row.refresh_token),
        status=row.status,
        connected_at=row.connected_at,
        last_used_at=row.last_used_at,
    )


class ConnectionRows:
    """The ``ConnectionStore`` of one session (``dropbox.connections``) over ``db.models.DropboxConnection``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None:
        """The user's row, or ``None``."""

        statement = select(DropboxConnection).where(DropboxConnection.user_id == user_id)
        return _stored((await self._session.execute(statement)).scalar_one_or_none())

    async def save(self, user_id: uuid.UUID, account: DropboxAccount, *, refresh_token: bytes, now: datetime) -> None:
        """Insert or replace (``ON CONFLICT (user_id) DO UPDATE``) the row, ``active``: account id, display name, team
        id and name, the ciphertext, ``connected_at = last_used_at = now``, ``expired_at`` null. Never the e-mail."""

        values: dict[str, Any] = {
            "account_id": account.account_id,
            "name": account.name,
            "team_id": account.team_id,
            "team_name": account.team_name,
            "refresh_token": refresh_token,
            "status": ACTIVE,
            "connected_at": now,
            "last_used_at": now,
            "expired_at": None,
        }
        statement = pg_insert(DropboxConnection).values(user_id=user_id, **values)
        await self._session.execute(statement.on_conflict_do_update(index_elements=("user_id",), set_=values))

    async def touch(self, user_id: uuid.UUID, *, now: datetime) -> None:
        """``last_used_at = now``."""

        await self._session.execute(
            update(DropboxConnection).where(DropboxConnection.user_id == user_id).values(last_used_at=now)
        )

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        """``status = 'expired'``, ``expired_at = now``."""

        await self._session.execute(
            update(DropboxConnection).where(DropboxConnection.user_id == user_id).values(status=EXPIRED, expired_at=now)
        )

    async def delete(self, user_id: uuid.UUID) -> bool:
        """Delete the row; whether there was one."""

        result = await self._session.execute(
            delete(DropboxConnection).where(DropboxConnection.user_id == user_id).returning(DropboxConnection.user_id)
        )
        return result.first() is not None


def connection_store(transaction: Callable[[], AbstractAsyncContextManager[AsyncSession]]) -> StoreTransaction:
    """The store transaction of ``DropboxConnections`` over a session transaction (committed on success)."""

    @asynccontextmanager
    async def store() -> AsyncIterator[ConnectionRows]:
        async with transaction() as session:
            yield ConnectionRows(session)

    return store
