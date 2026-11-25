"""Tier 2: ``db.notion_plan`` against a real Postgres (migrated scratch database, see specflow_db_live_support): the
target and the change of its parent page, the map of plan nodes (pending rows, applied rows, one page per node), the
guard, the run bookkeeping and the one cross-project read. Skipped unless ``SPECFLOW_TEST_POSTGRES=1``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import notion_plan as plan_db
from db.notion_plan import ParentChoice, TargetSaved
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_live import requires_live
from test_db_integrations_live import new_project, new_user

pytestmark = requires_live("POSTGRES")

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PAGE_ONE = ParentChoice("page-1", "Lab", "https://www.notion.so/page1")
PAGE_TWO = ParentChoice("page-2", "Other", "https://www.notion.so/page2")


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def save(factory, project, *, parent=PAGE_ONE, enabled=True) -> TargetSaved:
    async with factory() as session, session.begin():
        return await plan_db.save_target(session, project, parent=parent, enabled=enabled, now=NOW)


async def test_a_target_is_created_and_read_back_with_the_project_name(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)

    first = await save(factory, project)

    assert first == TargetSaved(destination_changed=False, turned_on=True, keys_changed=True)
    async with factory() as session:
        found = await plan_db.get_target(session, project)
        missing = await plan_db.get_target(session, uuid.uuid4())
    assert missing is None and found is not None
    assert (found.project_name, found.parent_page_id, found.parent_page_title, found.enabled) == (
        "P",
        "page-1",
        "Lab",
        True,
    )
    assert (found.database_id, found.last_status) == (None, None)


async def test_a_new_parent_page_clears_the_database_and_forgets_the_map(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project, enabled=False)
    async with factory() as session, session.begin():
        assert await plan_db.update_target(
            session, project, parent_page_id="page-1", database_id="db", data_source_id="ds", database_url="u"
        )
        await plan_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=NOW)
        await plan_db.update_item(session, project, "STORY::A", page_id="row-1")

    same = await save(factory, project, enabled=True)
    async with factory() as session:
        kept = await plan_db.list_items(session, project)
        target = await plan_db.get_target(session, project)
    moved = await save(factory, project, parent=PAGE_TWO)
    async with factory() as session:
        gone = await plan_db.list_items(session, project)
        cleared = await plan_db.get_target(session, project)

    assert same == TargetSaved(destination_changed=False, turned_on=True, keys_changed=False)
    assert [(item.node_id, item.page_id) for item in kept] == [("STORY::A", "row-1")]
    assert target is not None and (target.database_id, target.data_source_id) == ("db", "ds")
    assert moved == TargetSaved(destination_changed=True, turned_on=False, keys_changed=True) and gone == []
    assert cleared is not None and (cleared.database_id, cleared.data_source_id, cleared.database_url) == (
        None,
        None,
        None,
    )


async def test_the_database_ids_are_stored_only_while_the_parent_page_is_the_one_the_pass_started_with(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project)

    async with factory() as session, session.begin():
        stale = await plan_db.update_target(session, project, parent_page_id="page-9", database_id="db")
        fresh = await plan_db.update_target(session, project, parent_page_id="page-1", database_id="db")
        with pytest.raises(TypeError):
            await plan_db.update_target(session, project, parent_page_id="page-1", enabled=False)

    assert (stale, fresh) == (False, True)


async def test_a_pending_row_takes_the_new_request_time_until_it_has_a_page(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project)
    later = NOW + timedelta(hours=1)

    async with factory() as session, session.begin():
        await plan_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=NOW)
        await plan_db.update_item(session, project, "STORY::A", error_code="notion_item_invalid")
        await plan_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=later)
        await plan_db.update_item(session, project, "STORY::A", page_id="row-5", content={"a": 1}, content_hash="h")
        await plan_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=NOW)
    async with factory() as session:
        [row] = await plan_db.list_items(session, project)

    assert (row.requested_at, row.page_id, row.content, row.content_hash, row.error_code) == (
        later,
        "row-5",
        {"a": 1},
        "h",
        None,
    )


async def test_an_unknown_column_is_refused_and_a_page_belongs_to_one_node_only(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project)
    async with factory() as session, session.begin():
        for node in ("STORY::A", "STORY::B"):
            await plan_db.add_pending(session, project, node_id=node, kind="story", requested_at=NOW)
        await plan_db.update_item(session, project, "STORY::A", page_id="row-1")
        with pytest.raises(TypeError):
            await plan_db.update_item(session, project, "STORY::A", kind="task")
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await plan_db.update_item(session, project, "STORY::B", page_id="row-1")
    with pytest.raises(IntegrityError):  # a closed set of kinds
        async with factory() as session, session.begin():
            await plan_db.add_pending(session, project, node_id="X", kind="subtask", requested_at=NOW)


async def test_the_guard_locks_the_target_and_is_true_only_for_its_parent_page(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project)

    async with factory() as session, session.begin():
        same = await plan_db.target_unchanged(session, project, parent_page_id="page-1")
        other = await plan_db.target_unchanged(session, project, parent_page_id="page-2")
        none = await plan_db.target_unchanged(session, uuid.uuid4(), parent_page_id="page-1")

    assert (same, other, none) == (True, False, False)


async def test_the_run_columns_record_the_start_and_the_end_and_recovery_finds_a_running_target(factory) -> None:
    ana = await new_user(factory, "Ana")
    project, idle = await new_project(factory, ana), await new_project(factory, ana)
    for target in (project, idle):
        await save(factory, target)

    async with factory() as session, session.begin():
        await plan_db.record_start(session, project, trigger="plan_run", queued_at=NOW)
    async with factory() as session:
        running = await plan_db.running_targets(session)
        started = await plan_db.get_target(session, project)
    async with factory() as session, session.begin():
        await plan_db.record_end(
            session, project, status="partial", error_code=None, counts={"created": 2, "deferred": 4}, finished_at=NOW
        )
    async with factory() as session:
        ended = await plan_db.get_target(session, project)
        after = await plan_db.running_targets(session)

    assert project in running and idle not in running and project not in after
    assert started is not None and (started.last_status, started.last_trigger) == ("running", "plan_run")
    assert ended is not None and (ended.last_status, ended.last_counts, ended.last_finished_at) == (
        "partial",
        {"created": 2, "deferred": 4},
        NOW,
    )
    with pytest.raises(IntegrityError):  # a closed set of triggers
        async with factory() as session, session.begin():
            await plan_db.record_start(session, project, trigger="mcp", queued_at=NOW)
