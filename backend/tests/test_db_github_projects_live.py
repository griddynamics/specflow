"""Tier 2: ``db.github_projects`` against a real Postgres (migrated scratch database, see specflow_db_live_support): the
target and its destination change, the map of plan nodes (pending rows, applied rows, unique issue numbers), the run
bookkeeping, the cascade from a removed repository and the one cross-project read. Skipped unless
``SPECFLOW_TEST_POSTGRES=1``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import github as github_db
from db import github_projects as projects_db
from db.github_projects import ProjectChoice, TargetSaved
from db.session import build_engine, build_session_factory
from github.client import Repository
from settings import DatabaseSettings
from specflow_live import requires_live
from test_db_integrations_live import new_project, new_user

pytestmark = requires_live("POSTGRES")

NOW = datetime(2026, 1, 1, tzinfo=UTC)
PROJECT_ONE = ProjectChoice("PVT_1", "Plan", "https://github.com/orgs/acme/projects/1")
PROJECT_TWO = ProjectChoice("PVT_2", "Other", "https://github.com/orgs/acme/projects/2")


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def repository(factory, project, owner, number: int = 7) -> uuid.UUID:
    found = Repository(
        id=number,
        full_name=f"acme/r{number}",
        html_url=f"https://github.com/acme/r{number}",
        private=True,
        default_branch="main",
        size_kb=10,
    )
    async with factory() as session, session.begin():
        return await github_db.add_repository(session, project, found, added_by=owner)


async def save(factory, project, repository_id, owner, *, choice=PROJECT_ONE, enabled=True) -> TargetSaved:
    async with factory() as session, session.begin():
        return await projects_db.save_target(
            session,
            project,
            repository_id=repository_id,
            project=choice,
            enabled=enabled,
            now=NOW,
            connected_user_id=owner,
        )


async def test_a_target_is_created_and_read_back_with_the_repository_name(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    repo_id = await repository(factory, project, ana)

    first = await save(factory, project, repo_id, ana)

    assert first == TargetSaved(destination_changed=False, turned_on=True, keys_changed=True)
    async with factory() as session:
        found = await projects_db.get_target(session, project)
        missing = await projects_db.get_target(session, uuid.uuid4())
    assert missing is None and found is not None
    assert (found.repository_name, found.project_node_id, found.project_title, found.enabled) == (
        "acme/r7",
        "PVT_1",
        "Plan",
        True,
    )
    assert (found.connected_user_id, found.last_status) == (ana, None)


async def test_saving_again_reports_what_changed_and_a_new_destination_forgets_the_map(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    repo_id = await repository(factory, project, ana)
    await save(factory, project, repo_id, ana, enabled=False)
    async with factory() as session, session.begin():
        await projects_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=NOW)
        await projects_db.update_item(session, project, "STORY::A", issue_number=3, item_id="PVTI_3")

    same = await save(factory, project, repo_id, ana, enabled=True)
    async with factory() as session:
        kept = await projects_db.list_items(session, project)
    moved = await save(factory, project, repo_id, ana, choice=PROJECT_TWO)
    async with factory() as session:
        gone = await projects_db.list_items(session, project)

    assert same == TargetSaved(destination_changed=False, turned_on=True, keys_changed=False)
    assert [(item.node_id, item.issue_number) for item in kept] == [("STORY::A", 3)]
    assert moved == TargetSaved(destination_changed=True, turned_on=False, keys_changed=True)
    assert gone == []


async def test_a_pending_row_takes_the_new_request_time_until_it_has_an_issue(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    repo_id = await repository(factory, project, ana)
    await save(factory, project, repo_id, ana)
    later = NOW + timedelta(hours=1)

    async with factory() as session, session.begin():
        await projects_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=NOW)
        await projects_db.update_item(session, project, "STORY::A", error_code="github_item_invalid")
        await projects_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=later)
        await projects_db.update_item(session, project, "STORY::A", issue_number=5, issue_id=50, issue_node_id="I_5")
        await projects_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=NOW)
    async with factory() as session:
        [row] = await projects_db.list_items(session, project)

    assert (row.requested_at, row.issue_number, row.issue_id, row.issue_node_id, row.error_code) == (
        later,
        5,
        50,
        "I_5",
        None,
    )


async def test_an_unknown_column_is_refused_and_an_issue_number_is_one_node_only(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    repo_id = await repository(factory, project, ana)
    await save(factory, project, repo_id, ana)
    async with factory() as session, session.begin():
        for node in ("STORY::A", "STORY::B"):
            await projects_db.add_pending(session, project, node_id=node, kind="story", requested_at=NOW)
        await projects_db.update_item(session, project, "STORY::A", issue_number=1)
        with pytest.raises(TypeError):
            await projects_db.update_item(session, project, "STORY::A", kind="subtask")
    with pytest.raises(IntegrityError):
        async with factory() as session, session.begin():
            await projects_db.update_item(session, project, "STORY::B", issue_number=1)


async def test_the_guard_locks_the_target_and_is_true_only_for_its_destination(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    repo_id = await repository(factory, project, ana)
    other = await repository(factory, project, ana, number=8)
    await save(factory, project, repo_id, ana)

    async with factory() as session, session.begin():
        same = await projects_db.target_unchanged(session, project, repository_id=repo_id, project_node_id="PVT_1")
        wrong_project = await projects_db.target_unchanged(session, project, repository_id=repo_id, project_node_id="X")
        wrong_repo = await projects_db.target_unchanged(session, project, repository_id=other, project_node_id="PVT_1")
        none = await projects_db.target_unchanged(session, uuid.uuid4(), repository_id=repo_id, project_node_id="PVT_1")

    assert (same, wrong_project, wrong_repo, none) == (True, False, False, False)


async def test_the_run_columns_record_the_start_and_the_end_and_recovery_finds_a_running_target(factory) -> None:
    ana = await new_user(factory, "Ana")
    project, idle = await new_project(factory, ana), await new_project(factory, ana)
    for target in (project, idle):
        await save(factory, target, await repository(factory, target, ana), ana)

    async with factory() as session, session.begin():
        await projects_db.record_start(session, project, trigger="plan_run", queued_at=NOW)
    async with factory() as session:
        running = await projects_db.running_targets(session)
        started = await projects_db.get_target(session, project)
    async with factory() as session, session.begin():
        await projects_db.record_end(
            session, project, status="partial", error_code=None, counts={"created": 2}, finished_at=NOW
        )
    async with factory() as session:
        ended = await projects_db.get_target(session, project)
        after = await projects_db.running_targets(session)

    assert project in running and idle not in running and project not in after
    assert started is not None and (started.last_status, started.last_trigger) == ("running", "plan_run")
    assert ended is not None and (ended.last_status, ended.last_counts, ended.last_finished_at) == (
        "partial",
        {"created": 2},
        NOW,
    )
    with pytest.raises(IntegrityError):  # a closed set of triggers
        async with factory() as session, session.begin():
            await projects_db.record_start(session, project, trigger="mcp", queued_at=NOW)


async def test_removing_the_repository_deletes_the_target_and_a_new_target_clears_the_map_left_behind(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    repo_id = await repository(factory, project, ana)
    await save(factory, project, repo_id, ana)
    async with factory() as session, session.begin():
        await projects_db.add_pending(session, project, node_id="STORY::A", kind="story", requested_at=NOW)

    async with factory() as session, session.begin():
        assert await github_db.delete_repository(session, project, repo_id) is True
    async with factory() as session:
        target = await projects_db.get_target(session, project)
        left = await projects_db.list_items(session, project)
    again = await save(factory, project, await repository(factory, project, ana, number=9), ana)
    async with factory() as session:
        cleared = await projects_db.list_items(session, project)

    assert target is None and len(left) == 1
    assert again == TargetSaved(destination_changed=False, turned_on=True, keys_changed=True) and cleared == []
