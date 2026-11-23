"""Tier 2: ``db.github`` against a real Postgres (migrated scratch database, see specflow_db_live_support).

Skipped unless ``SPECFLOW_TEST_POSTGRES=1``. Includes the refresh lock: a second ``ConnectionRows.lock`` waits for the
first transaction, so two processes (the api and a worker) never refresh one token at once (docs/github/oauth.md).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import github
from db.github import ConnectionRows, RepositoryExistsError, Snapshot, connection_store, session_transaction
from db.session import build_engine, build_session_factory
from github.client import GitHubUser, Repository
from settings import DatabaseSettings
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
WAIT = 0.4  # how long a blocked statement is given before the test calls it blocked
LIMIT = 15  # no test waits longer than this on a lock
REPO = Repository(
    id=7, full_name="octo/app", html_url="https://github.com/octo/app", private=True, default_branch="main", size_kb=10
)
SHA = "c" * 40


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def new_user(factory: async_sessionmaker[AsyncSession], name: str = "n") -> uuid.UUID:
    user = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:id, :actor, :name, :email)"),
            {"id": user, "actor": f"actor-{user}", "name": name, "email": f"{user}@example.com"},
        )
    return user


async def new_project(factory: async_sessionmaker[AsyncSession], owner: uuid.UUID) -> uuid.UUID:
    project = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO projects (id, name, created_by) VALUES (:id, 'P', :user)"), {"id": project, "user": owner}
        )
    return project


async def test_the_store_saves_replaces_rotates_expires_and_deletes_a_connection(factory) -> None:
    user = await new_user(factory)
    store = connection_store(session_transaction(factory))

    async with store() as rows:
        await rows.save(user, GitHubUser(1, "octo"), refresh_token=b"cipher-1", refresh_expires_at=LATER, now=NOW)
    async with store() as rows:
        await rows.save(user, GitHubUser(1, "octo2"), refresh_token=b"cipher-2", refresh_expires_at=None, now=LATER)
        await rows.rotate(user, refresh_token=b"cipher-3", refresh_expires_at=LATER, now=LATER)
    async with store() as rows:
        row = await rows.get(user)
    assert row is not None
    assert (row.login, row.refresh_token, row.status, row.connected_at) == ("octo2", b"cipher-3", "active", LATER)

    async with store() as rows:
        await rows.expire(user, now=LATER)
    async with factory() as session:
        assert await github.connection_status(session, user) == "expired"
    async with store() as rows:
        assert await rows.delete(user) is True
        assert await rows.delete(user) is False
        assert await rows.get(user) is None


async def test_a_second_lock_waits_for_the_first_transaction_and_sees_its_rotated_token(factory) -> None:
    user = await new_user(factory)
    async with factory() as session, session.begin():
        await ConnectionRows(session).save(
            user, GitHubUser(1, "octo"), refresh_token=b"cipher-1", refresh_expires_at=None, now=NOW
        )
    held, release = asyncio.Event(), asyncio.Event()

    async def first() -> None:
        async with factory() as session, session.begin():
            await ConnectionRows(session).lock(user)
            held.set()
            await release.wait()
            await ConnectionRows(session).rotate(user, refresh_token=b"cipher-2", refresh_expires_at=None, now=LATER)

    async def second() -> bytes:
        async with factory() as session, session.begin():
            row = await ConnectionRows(session).lock(user)
            assert row is not None
            return row.refresh_token

    holder = asyncio.create_task(first())
    await asyncio.wait_for(held.wait(), LIMIT)
    waiter = asyncio.create_task(second())
    await asyncio.sleep(WAIT)
    assert not waiter.done()  # blocked on the row lock

    release.set()
    await asyncio.wait_for(holder, LIMIT)
    assert await asyncio.wait_for(waiter, LIMIT) == b"cipher-2"


async def test_the_repository_rows_follow_a_clone_check_and_keep_the_last_snapshot(factory) -> None:
    owner = await new_user(factory, "Ada")
    project = await new_project(factory, owner)
    async with factory() as session, session.begin():
        rid = await github.add_repository(session, project, REPO, added_by=owner)
    with pytest.raises(RepositoryExistsError):
        async with factory() as session, session.begin():
            await github.add_repository(session, project, REPO, added_by=owner)

    async with factory() as session, session.begin():
        assert await github.start_clone(session, project, rid) is True
        assert await github.request_clone(session, project, rid) is True  # cloning stays cloning
    async with factory() as session, session.begin():
        snapshot = Snapshot("main", SHA, 12, 3400, f"projects/{project}/repositories/{rid}/{SHA}.tar.gz", 900)
        assert await github.finish_clone(session, project, rid, snapshot, now=NOW) is None
    async with factory() as session, session.begin():
        assert await github.fail_clone(session, project, rid, "github_clone_timeout", now=LATER) is True
    async with factory() as session:
        found = await github.get_repository(session, project, rid)
        listed = await github.list_repositories(session, project)
    assert found is not None and found.added_by_name == "Ada" and [r.repository.id for r in listed] == [rid]
    row = found.repository
    assert (row.clone_state, row.failure_code, row.head_sha, row.cloned_at, row.checked_at) == (
        "failed",
        "github_clone_timeout",
        SHA,
        NOW,
        LATER,
    )

    async with factory() as session, session.begin():
        assert await github.request_clone(session, project, rid) is True
        newer = Snapshot("main", "d" * 40, 13, 3500, f"projects/{project}/repositories/{rid}/{'d' * 40}.tar.gz", 901)
        assert await github.finish_clone(session, project, rid, newer, now=LATER) == snapshot.snapshot_key
    async with factory() as session, session.begin():
        assert await github.delete_repository(session, project, rid) is True
        assert await github.delete_repository(session, project, rid) is False
        assert await github.start_clone(session, project, rid) is False
        assert await github.request_clone(session, project, rid) is False
