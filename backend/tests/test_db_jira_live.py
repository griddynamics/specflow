"""Tier 2: ``db.jira`` against a real Postgres (migrated scratch database, see specflow_db_live_support).

Skipped unless ``SPECFLOW_TEST_POSTGRES=1``. Includes the lock order of the target row in both directions
(FR-JIRA-RUN-08): a map-row write holds the share lock, ``save_target`` holds the update lock.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import jira
from db.models import JiraTarget
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


async def new_project(factory: async_sessionmaker[AsyncSession], name: str = "Project") -> uuid.UUID:
    user, project = uuid.uuid4(), uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:id, :actor, 'n', :email)"),
            {"id": user, "actor": f"actor-{user}", "email": f"{user}@example.com"},
        )
        await session.execute(
            text("INSERT INTO projects (id, name, created_by) VALUES (:id, :name, :user)"),
            {"id": project, "name": name, "user": user},
        )
    return project


async def save(
    factory: async_sessionmaker[AsyncSession],
    project: uuid.UUID,
    *,
    jira_project: str = "SF",
    epic_key: str | None = None,
    enabled: bool = True,
    now: datetime = NOW,
) -> jira.TargetSaved:
    async with factory() as session, session.begin():
        return await jira.save_target(
            session, project, jira_project=jira_project, epic_key=epic_key, enabled=enabled, now=now
        )


async def issues(factory: async_sessionmaker[AsyncSession], project: uuid.UUID) -> list:
    async with factory() as session:
        return await jira.list_issues(session, project)


async def pending(factory: async_sessionmaker[AsyncSession], project: uuid.UUID, node_id: str, kind: str = "story"):
    async with factory() as session, session.begin():
        await jira.add_pending(session, project, node_id=node_id, kind=kind, requested_at=NOW)


async def test_save_target_reports_what_changed_and_get_target_adds_the_project_name(factory) -> None:
    project = await new_project(factory, "Specflow")

    async with factory() as session:
        assert await jira.get_target(session, project) is None
    created = await save(factory, project, enabled=False)
    same = await save(factory, project, enabled=False, now=LATER)
    turned_on = await save(factory, project, now=LATER)
    epic = await save(factory, project, epic_key="SF-7", now=LATER + timedelta(hours=1))

    assert created == jira.TargetSaved(project_changed=False, turned_on=False, keys_changed=True)
    assert same == jira.TargetSaved(project_changed=False, turned_on=False, keys_changed=False)
    assert turned_on == jira.TargetSaved(project_changed=False, turned_on=True, keys_changed=False)
    assert epic == jira.TargetSaved(project_changed=False, turned_on=False, keys_changed=True)
    async with factory() as session:
        view = await jira.get_target(session, project)
    assert view is not None
    assert (view.project_name, view.jira_project, view.epic_key, view.enabled) == ("Specflow", "SF", "SF-7", True)
    assert view.updated_at == LATER + timedelta(hours=1)
    assert view.last_status is None


async def test_a_jira_project_change_deletes_the_rows_of_that_project_only(factory) -> None:
    mine, other = await new_project(factory), await new_project(factory)
    for project in (mine, other):
        await save(factory, project)
        await pending(factory, project, "STORY::a::one")

    unchanged = await save(factory, mine, epic_key="SF-1")
    assert unchanged.project_changed is False
    assert len(await issues(factory, mine)) == 1  # an epic key change keeps the map (FR-JIRA-CFG-04)

    changed = await save(factory, mine, jira_project="NEW")

    assert changed == jira.TargetSaved(project_changed=True, turned_on=False, keys_changed=True)
    assert await issues(factory, mine) == []
    assert len(await issues(factory, other)) == 1


async def test_a_pass_is_recorded_and_only_enabled_running_targets_are_listed(factory) -> None:
    running, finished, disabled = (await new_project(factory) for _ in range(3))
    await save(factory, running)
    await save(factory, finished)
    await save(factory, disabled, enabled=False)
    async with factory() as session, session.begin():
        for project in (running, finished, disabled):
            await jira.record_start(session, project, trigger="mcp", queued_at=NOW)
        await jira.record_end(
            session, finished, status="partial", error_code=None, counts={"created": 2, "failed": 1}, finished_at=LATER
        )

    async with factory() as session:
        assert await jira.running_targets(session) == [running]
        view = await jira.get_target(session, finished)
    assert view is not None
    assert (view.last_status, view.last_trigger, view.last_started_at, view.last_finished_at) == (
        "partial",
        "mcp",
        NOW,
        LATER,
    )
    assert view.last_counts == {"created": 2, "failed": 1}
    async with factory() as session, session.begin():
        await jira.record_start(session, finished, trigger="plan_run", queued_at=LATER)
    async with factory() as session:
        view = await jira.get_target(session, finished)
    assert view is not None
    assert (view.last_status, view.last_finished_at, view.last_counts) == ("running", None, None)


async def test_issue_rows_pending_upsert_partial_update_and_positions(factory) -> None:
    project, other = await new_project(factory), await new_project(factory)
    await pending(factory, project, "STORY::a::two")
    await pending(factory, project, "STORY::a::one")
    await pending(factory, other, "STORY::a::one")
    async with factory() as session, session.begin():
        await jira.update_issue(session, project, "STORY::a::two", error_code="jira_invalid_field")
        await jira.add_pending(session, project, node_id="STORY::a::two", kind="story", requested_at=LATER)
        await jira.update_issue(session, project, "STORY::a::one", jira_key="SF-2", content={"v": 1}, content_hash="h")
        await jira.add_pending(session, project, node_id="STORY::a::one", kind="story", requested_at=LATER)
        await jira.update_issue(session, project, "STORY::a::one", position=3, error_code="x", synced_at=LATER)
        await jira.update_issue(session, project, "STORY::a::one", error_code=None)
        await jira.update_issue(session, project, "STORY::a::one")
        await jira.set_positions(session, project, {"STORY::a::one": 1, "STORY::a::two": 2})

    one, two = await issues(factory, project)
    assert (one.node_id, one.jira_key, one.content, one.content_hash) == ("STORY::a::one", "SF-2", {"v": 1}, "h")
    assert (one.requested_at, one.position, one.error_code, one.synced_at) == (NOW, 1, None, LATER)
    assert (two.node_id, two.jira_key, two.requested_at, two.position) == ("STORY::a::two", None, LATER, 2)
    assert two.error_code is None  # a new create clears the error of the refused one
    [theirs] = await issues(factory, other)
    assert (theirs.jira_key, theirs.position) == (None, None)


async def run_blocked(task: asyncio.Task) -> bool:
    """True when the task has not finished after ``WAIT`` seconds."""

    done, _ = await asyncio.wait({task}, timeout=WAIT)
    return not done


async def test_a_map_row_write_holding_the_share_lock_delays_save_target_whose_delete_removes_the_row(factory) -> None:
    project = await new_project(factory)
    await save(factory, project)
    await pending(factory, project, "STORY::a::one")

    async with factory() as writer:
        await writer.begin()
        assert await jira.target_unchanged(writer, project, jira_project="SF", epic_key=None)
        await jira.update_issue(writer, project, "STORY::a::one", jira_key="SF-1")
        change = asyncio.create_task(save(factory, project, jira_project="NEW"))

        assert await run_blocked(change)
        await writer.commit()
    saved = await asyncio.wait_for(change, LIMIT)

    assert saved.project_changed is True
    assert await issues(factory, project) == []  # the row the pass wrote is deleted with the old map


async def test_save_target_holding_the_update_lock_makes_target_unchanged_answer_false_after_its_commit(
    factory,
) -> None:
    project = await new_project(factory)
    await save(factory, project)

    async with factory() as saver, factory() as reader:
        await saver.begin()
        await jira.save_target(saver, project, jira_project="SF", epic_key="SF-9", enabled=True, now=LATER)
        await reader.begin()
        check = asyncio.create_task(jira.target_unchanged(reader, project, jira_project="SF", epic_key=None))

        assert await run_blocked(check)
        await saver.commit()
        assert await asyncio.wait_for(check, LIMIT) is False
        await reader.rollback()


async def test_save_target_compares_with_the_locked_row_not_one_the_session_loaded_earlier(factory) -> None:
    # Tab 2 loads the target (SF); tab 1 commits SF to NEW and a pass maps an issue; tab 2 saves SF again.
    project = await new_project(factory)
    await save(factory, project)

    async with factory() as stale:
        await stale.begin()
        before = await jira.get_target(stale, project)
        assert before is not None and before.jira_project == "SF"
        await stale.execute(select(JiraTarget).where(JiraTarget.project_id == project))  # the entity is now cached
        await save(factory, project, jira_project="NEW")
        await pending(factory, project, "STORY::a::one")
        saved = await jira.save_target(stale, project, jira_project="SF", epic_key=None, enabled=True, now=LATER)
        await stale.commit()

    assert saved.project_changed is True
    assert await issues(factory, project) == []
