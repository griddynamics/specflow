"""Application-role data access for the Atlassian connections (docs/jira-sync/oauth.md "Data", "Tokens").

Same rules as ``db.jira``: every function runs inside the caller's session and transaction, flushes and never commits;
every statement filters on the user id it is given. The refresh token arrives and leaves as Fernet ciphertext;
nothing here logs.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AtlassianConnection

ACTIVE, EXPIRED = "active", "expired"


async def get_connection(session: AsyncSession, user_id: uuid.UUID) -> AtlassianConnection | None:
    statement = select(AtlassianConnection).where(AtlassianConnection.user_id == user_id)
    return (await session.execute(statement)).scalar_one_or_none()


async def lock_connection(session: AsyncSession, user_id: uuid.UUID) -> AtlassianConnection | None:
    """The row locked ``FOR UPDATE`` (a refresh holds it until the rotated token is committed)."""

    statement = (
        select(AtlassianConnection)
        .where(AtlassianConnection.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def upsert_connection(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    cloud_id: str,
    account_id: str,
    account_name: str,
    scopes: str,
    refresh_token: bytes,
    now: datetime,
) -> None:
    """Create or replace the user's connection: a new grant, ``active``, connected and used ``now``."""

    values = {
        "cloud_id": cloud_id,
        "account_id": account_id,
        "account_name": account_name,
        "scopes": scopes,
        "refresh_token": refresh_token,
        "status": ACTIVE,
        "connected_at": now,
        "last_used_at": now,
        "expired_at": None,
    }
    statement = pg_insert(AtlassianConnection).values(user_id=user_id, **values)
    await session.execute(statement.on_conflict_do_update(index_elements=("user_id",), set_=values))


async def rotate_refresh_token(
    session: AsyncSession, user_id: uuid.UUID, *, refresh_token: bytes, now: datetime
) -> None:
    """Store the rotated refresh token of a refresh and mark the connection used ``now``."""

    await session.execute(
        update(AtlassianConnection)
        .where(AtlassianConnection.user_id == user_id)
        .values(refresh_token=refresh_token, last_used_at=now)
    )


async def mark_expired(session: AsyncSession, user_id: uuid.UUID, *, now: datetime) -> None:
    """Atlassian refused the refresh token: ``expired`` until the user reconnects."""

    await session.execute(
        update(AtlassianConnection).where(AtlassianConnection.user_id == user_id).values(status=EXPIRED, expired_at=now)
    )


async def delete_connection(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Delete the user's connection; true when there was one."""

    result = await session.execute(
        delete(AtlassianConnection).where(AtlassianConnection.user_id == user_id).returning(AtlassianConnection.user_id)
    )
    return result.first() is not None
