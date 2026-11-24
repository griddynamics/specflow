"""Tier 2: ``db.atlassian`` and the acting account of ``db.jira.save_target`` against a real Postgres (migrated
scratch database, see specflow_db_live_support).

Skipped unless ``SPECFLOW_TEST_POSTGRES=1``. Includes the refresh lock: a second ``lock_connection`` waits for the
first transaction, so two processes never refresh one token at once (docs/jira-sync/oauth.md "Tokens").
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import atlassian, jira
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
WAIT = 0.4  # how long a blocked statement is given before the test calls it blocked
LIMIT = 15  # no test waits longer than this on a lock


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def new_user(factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    user = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:id, :actor, 'n', :email)"),
            {"id": user, "actor": f"actor-{user}", "email": f"{user}@example.com"},
        )
    return user


async def connect(factory: async_sessionmaker[AsyncSession], user: uuid.UUID, *, token: bytes, now: datetime) -> None:
    async with factory() as session, session.begin():
        await atlassian.upsert_connection(
            session,
            user,
            cloud_id="cloud-1",
            account_id="acc-1",
            account_name="Ada",
            scopes="read:jira-work offline_access",
            refresh_token=token,
            now=now,
        )


async def read(factory: async_sessionmaker[AsyncSession], user: uuid.UUID):
    async with factory() as session:
        return await atlassian.get_connection(session, user)


async def test_upsert_creates_and_replaces_the_connection_of_a_user(factory) -> None:
    user = await new_user(factory)
    assert await read(factory, user) is None

    await connect(factory, user, token=b"cipher-1", now=NOW)
    async with factory() as session, session.begin():
        await atlassian.mark_expired(session, user, now=NOW)
    await connect(factory, user, token=b"cipher-2", now=LATER)

    row = await read(factory, user)
    assert (row.status, row.refresh_token, row.connected_at, row.expired_at) == ("active", b"cipher-2", LATER, None)
    assert (row.cloud_id, row.account_name, row.scopes) == ("cloud-1", "Ada", "read:jira-work offline_access")


async def test_rotate_mark_expired_and_delete_touch_only_the_given_user(factory) -> None:
    user, other = await new_user(factory), await new_user(factory)
    await connect(factory, user, token=b"cipher-1", now=NOW)
    await connect(factory, other, token=b"cipher-o", now=NOW)

    async with factory() as session, session.begin():
        await atlassian.rotate_refresh_token(session, user, refresh_token=b"cipher-2", now=LATER)
    rotated = await read(factory, user)
    async with factory() as session, session.begin():
        await atlassian.mark_expired(session, user, now=LATER)
    expired = await read(factory, user)
    async with factory() as session, session.begin():
        deleted = await atlassian.delete_connection(session, user)
        again = await atlassian.delete_connection(session, user)

    assert (rotated.refresh_token, rotated.last_used_at, rotated.status) == (b"cipher-2", LATER, "active")
    assert (expired.status, expired.expired_at) == ("expired", LATER)
    assert (deleted, again) == (True, False) and await read(factory, user) is None
    untouched = await read(factory, other)
    assert (untouched.refresh_token, untouched.status) == (b"cipher-o", "active")


async def test_a_second_refresh_lock_waits_for_the_first_transaction(factory) -> None:
    user = await new_user(factory)
    await connect(factory, user, token=b"cipher-1", now=NOW)
    held = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with factory() as session, session.begin():
            await atlassian.lock_connection(session, user)
            held.set()
            await release.wait()
            await atlassian.rotate_refresh_token(session, user, refresh_token=b"cipher-2", now=LATER)

    async def second() -> bytes:
        async with factory() as session, session.begin():
            row = await atlassian.lock_connection(session, user)
            return row.refresh_token

    holder = asyncio.create_task(first())
    await asyncio.wait_for(held.wait(), LIMIT)
    waiter = asyncio.create_task(second())
    await asyncio.sleep(WAIT)
    assert not waiter.done()  # blocked on the row lock

    release.set()
    await asyncio.wait_for(holder, LIMIT)

    assert await asyncio.wait_for(waiter, LIMIT) == b"cipher-2"  # it reads the rotated token


async def test_save_target_sets_the_acting_account_and_deleting_the_user_clears_it(factory) -> None:
    owner, other = await new_user(factory), await new_user(factory)
    project = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO projects (id, name, created_by) VALUES (:id, 'P', :user)"), {"id": project, "user": owner}
        )
        await jira.save_target(
            session, project, jira_project="SF", epic_key=None, enabled=True, now=NOW, connected_user_id=other
        )
    async with factory() as session:
        first = await jira.get_target(session, project)
    async with factory() as session, session.begin():
        await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": other})
    async with factory() as session:
        orphaned = await jira.get_target(session, project)
    async with factory() as session, session.begin():
        await jira.save_target(
            session, project, jira_project="SF", epic_key=None, enabled=True, now=LATER, connected_user_id=owner
        )
    async with factory() as session:
        taken_over = await jira.get_target(session, project)

    assert first.connected_user_id == other
    assert orphaned.connected_user_id is None  # the user went, the target stays without an acting account
    assert taken_over.connected_user_id == owner and taken_over.updated_at == NOW  # the acting account is no key
