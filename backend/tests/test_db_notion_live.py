"""Tier 2: ``db.notion`` against a real Postgres (migrated scratch database, see specflow_db_live_support).

Skipped unless ``SPECFLOW_TEST_POSTGRES=1``. Includes the refresh lock: a second ``ConnectionRows.lock`` waits for the
first transaction, so two replicas never refresh one token at once, and ``NotionConnections`` over the real rows
(docs/notion/oauth.md "Tokens", "Data").
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import notion
from db.notion import ConnectionRows, connection_store
from db.session import build_engine, build_session_factory
from notion.connections import NotionConnections, TokenGrant
from notion.reasons import NotionCode, NotionError
from settings import DatabaseSettings
from specflow_live import requires_live
from test_notion_connections import CODE_CANARY, RETURN, FakeNotion, notion_settings, state_of

pytestmark = requires_live("POSTGRES")

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
WAIT = 0.4  # how long a blocked statement is given before the test calls it blocked
LIMIT = 15  # no test waits longer than this on a lock


def grant(workspace: str = "ws-1", name: str = "Sample Workspace", bot: str = "bot-1") -> TokenGrant:
    return TokenGrant(SecretStr("access"), SecretStr("refresh"), 3600, bot, workspace, name)


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


def transaction_of(
    factory: async_sessionmaker[AsyncSession],
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    @asynccontextmanager
    async def transaction() -> AsyncIterator[AsyncSession]:
        async with factory() as session, session.begin():
            yield session

    return transaction


async def new_user(factory: async_sessionmaker[AsyncSession], name: str = "n") -> uuid.UUID:
    user = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:id, :actor, :name, :email)"),
            {"id": user, "actor": f"actor-{user}", "name": name, "email": f"{user}@example.com"},
        )
    return user


async def test_the_store_saves_replaces_rotates_expires_and_deletes_a_connection(factory) -> None:
    user = await new_user(factory)
    store = connection_store(transaction_of(factory))

    async with store() as rows:
        await rows.save(user, grant(), refresh_token=b"cipher-1", refresh_expires_at=LATER, now=NOW)
    async with store() as rows:
        await rows.save(
            user,
            grant("ws-2", "Second", "bot-2"),
            refresh_token=b"cipher-2",
            refresh_expires_at=None,
            now=LATER,
        )
        await rows.rotate(user, refresh_token=b"cipher-3", refresh_expires_at=LATER, now=LATER)
    async with store() as rows:
        row = await rows.get(user)
    assert row is not None
    assert (row.workspace_id, row.workspace_name, row.bot_id) == ("ws-2", "Second", "bot-2")
    assert (row.refresh_token, row.status, row.connected_at, row.last_used_at) == (b"cipher-3", "active", LATER, LATER)

    async with store() as rows:
        await rows.expire(user, now=LATER)
    async with factory() as session:
        assert await notion.connection_status(session, user) == "expired"
    async with store() as rows:
        await rows.save(user, grant(), refresh_token=b"cipher-4", refresh_expires_at=None, now=LATER)  # reconnect
    async with factory() as session:
        assert await notion.connection_status(session, user) == "active"
    async with store() as rows:
        assert await rows.delete(user) is True
        assert await rows.delete(user) is False
        assert await rows.get(user) is None
    async with factory() as session:
        assert await notion.connection_status(session, user) is None


async def test_a_connection_of_one_user_never_changes_another(factory) -> None:
    first, second = await new_user(factory), await new_user(factory)
    store = connection_store(transaction_of(factory))
    async with store() as rows:
        await rows.save(first, grant(), refresh_token=b"one", refresh_expires_at=None, now=NOW)
        await rows.save(second, grant(), refresh_token=b"two", refresh_expires_at=None, now=NOW)
    async with store() as rows:
        await rows.rotate(first, refresh_token=b"one-b", refresh_expires_at=None, now=LATER)
        await rows.expire(first, now=LATER)
        assert await rows.delete(first) is True
    async with store() as rows:
        other = await rows.get(second)
    assert other is not None and (other.refresh_token, other.status) == (b"two", "active")


async def test_deleting_the_user_deletes_the_connection_and_a_bad_status_is_refused(factory) -> None:
    user = await new_user(factory)
    store = connection_store(transaction_of(factory))
    async with store() as rows:
        await rows.save(user, grant(), refresh_token=b"cipher", refresh_expires_at=None, now=NOW)

    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await session.execute(
                text("UPDATE notion_connections SET status = 'weird' WHERE user_id = :id"), {"id": user}
            )
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user})
    async with factory() as session:
        assert await notion.connection_status(session, user) is None


async def hold_the_row(
    factory: async_sessionmaker[AsyncSession], user: uuid.UUID, locked: asyncio.Event, proceed: asyncio.Event
) -> None:
    """A refresh in flight: lock the row, wait, then commit a rotated refresh token."""

    async with factory() as session, session.begin():
        rows = ConnectionRows(session)
        await rows.lock(user)
        locked.set()
        await proceed.wait()
        await rows.rotate(user, refresh_token=b"cipher-2", refresh_expires_at=None, now=LATER)


async def read_the_locked_row(factory: async_sessionmaker[AsyncSession], user: uuid.UUID) -> bytes:
    async with factory() as session, session.begin():
        found = await ConnectionRows(session).lock(user)
    assert found is not None
    return found.refresh_token


async def test_a_second_lock_waits_for_the_first_transaction_and_sees_its_rotated_token(factory) -> None:
    user = await new_user(factory)
    async with factory() as session, session.begin():
        await ConnectionRows(session).save(user, grant(), refresh_token=b"cipher-1", refresh_expires_at=None, now=NOW)
    locked, proceed = asyncio.Event(), asyncio.Event()

    first = asyncio.create_task(hold_the_row(factory, user, locked, proceed))
    await asyncio.wait_for(locked.wait(), LIMIT)
    second = asyncio.create_task(read_the_locked_row(factory, user))
    await asyncio.sleep(WAIT)
    assert not second.done()  # blocked on the row lock

    proceed.set()
    await asyncio.wait_for(first, LIMIT)
    assert await asyncio.wait_for(second, LIMIT) == b"cipher-2"


async def test_the_connections_connect_refresh_expire_and_disconnect_over_the_real_rows(factory) -> None:
    user = await new_user(factory)
    scripted = FakeNotion()
    clock = [1_000_000.0]
    connections = NotionConnections(
        notion_settings(),
        store=connection_store(transaction_of(factory)),
        oauth=scripted.oauth(),
        monotonic=lambda: clock[0],
        clock=lambda: clock[0],
    )
    try:
        view, return_to = await connections.connect(
            user, code=CODE_CANARY, state=state_of(connections.authorize_url(user, RETURN))
        )
        assert (view.status, return_to) == ("active", RETURN)
        clock[0] += 3600
        assert await connections.access_token(user) == "access-2"  # rotated under the row lock, then committed
        async with factory() as session:
            stored = await ConnectionRows(session).get(user)
        assert (
            stored is not None
            and b"refresh-" not in stored.refresh_token
            and stored.last_used_at >= stored.connected_at
        )

        clock[0] += 3600
        scripted.error = (400, "invalid_grant")
        with pytest.raises(NotionError) as raised:
            await connections.access_token(user)
        assert raised.value.code is NotionCode.RECONNECT_REQUIRED
        shown = await connections.get(user)
        assert shown is not None and shown.status == "expired"

        await connections.disconnect(user)
        assert await connections.get(user) is None
    finally:
        await connections.aclose()
