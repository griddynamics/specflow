"""Tier 2: ``db.jira_gaps`` against a real Postgres (migrated scratch database, see specflow_db_live_support): the
target and its project change, the map of posted gaps (one row per gap, answered once), the poll bookkeeping, and the
account lookup by Atlassian account id. Skipped unless ``SPECFLOW_TEST_POSTGRES=1``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import jira_gaps
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_live import requires_live
from test_db_integrations_live import new_project, new_user

pytestmark = requires_live("POSTGRES")

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def save(factory, project, *, jira_project="SHOP", grouping="one", enabled=True) -> jira_gaps.GapTargetSaved:
    async with factory() as session, session.begin():
        return await jira_gaps.save_target(
            session, project, jira_project=jira_project, grouping=grouping, enabled=enabled, now=NOW
        )


async def post(factory, project, gap_id, *, key="SHOP-2", number=1, area="a") -> bool:
    async with factory() as session, session.begin():
        return await jira_gaps.add_issue(
            session, project, gap_id=gap_id, issue_key=key, area=area, number=number, now=NOW
        )


async def test_a_target_is_created_read_back_and_listed_only_while_enabled(factory) -> None:
    ana = await new_user(factory, "Ana")
    project, other = await new_project(factory, ana), await new_project(factory, ana)

    first = await save(factory, project)
    await save(factory, other, enabled=False)

    assert first == jira_gaps.GapTargetSaved(turned_on=True, project_changed=False)
    async with factory() as session:
        found = await jira_gaps.get_target(session, project)
        enabled = await jira_gaps.enabled_targets(session)
        missing = await jira_gaps.get_target(session, uuid.uuid4())
    assert missing is None and found is not None
    assert (found.jira_project, found.grouping, found.story_key, found.enabled) == ("SHOP", "one", None, True)
    assert [target.project_id for target in enabled if target.project_id in {project, other}] == [project]


async def test_saving_again_reports_what_changed_and_a_project_change_forgets_the_map_the_story_and_the_cursor(
    factory,
) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project, enabled=False)
    async with factory() as session, session.begin():
        await jira_gaps.set_story(session, project, "SHOP-1")
        await jira_gaps.record_poll(session, project, cursor=NOW, now=NOW, error_code="jira_forbidden")
    await post(factory, project, "G-1")

    again = await save(factory, project, grouping="area")  # turned on, same project
    assert again == jira_gaps.GapTargetSaved(turned_on=True, project_changed=False)
    async with factory() as session:
        kept = await jira_gaps.get_target(session, project)
    assert kept is not None and kept.story_key == "SHOP-1" and kept.grouping == "area" and kept.poll_cursor == NOW

    moved = await save(factory, project, jira_project="OTHER", grouping="area")

    assert moved == jira_gaps.GapTargetSaved(turned_on=False, project_changed=True)
    async with factory() as session:
        target = await jira_gaps.get_target(session, project)
        rows = await jira_gaps.list_issues(session, project)
    assert target is not None and (target.story_key, target.poll_cursor, target.last_error_code) == (None, None, None)
    assert rows == []


async def test_a_gap_is_posted_once_and_answered_once(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project)

    assert await post(factory, project, "G-1", key="SHOP-2", number=1) is True
    assert await post(factory, project, "G-1", key="SHOP-9", number=5) is False  # the first row stays
    await post(factory, project, "G-2", key="SHOP-3", number=1)
    async with factory() as session, session.begin():
        first = await jira_gaps.mark_answered(session, project, "G-1", now=NOW + timedelta(hours=1))
        second = await jira_gaps.mark_answered(session, project, "G-1", now=NOW + timedelta(hours=2))
        unknown = await jira_gaps.mark_answered(session, project, "G-404", now=NOW)

    assert (first, second, unknown) == (True, False, False)
    async with factory() as session:
        rows = {row.gap_id: row for row in await jira_gaps.list_issues(session, project)}
    assert (rows["G-1"].issue_key, rows["G-1"].number, rows["G-1"].answered_at) == (
        "SHOP-2",
        1,
        NOW + timedelta(hours=1),
    )
    assert rows["G-2"].answered_at is None


async def test_the_poll_bookkeeping_keeps_the_cursor_unless_a_new_one_is_given(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project)

    async with factory() as session, session.begin():
        await jira_gaps.record_poll(session, project, cursor=NOW, now=NOW, error_code=None)
        await jira_gaps.record_poll(
            session, project, cursor=None, now=NOW + timedelta(minutes=1), error_code="jira_forbidden"
        )
        await jira_gaps.record_error(session, project, "jira_unreachable")

    async with factory() as session:
        target = await jira_gaps.get_target(session, project)
    assert target is not None
    assert (target.poll_cursor, target.last_polled_at, target.last_error_code) == (
        NOW,
        NOW + timedelta(minutes=1),
        "jira_unreachable",
    )


async def test_the_grouping_is_checked_by_the_database(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)

    with pytest.raises(IntegrityError):
        await save(factory, project, grouping="both")


async def test_accounts_are_found_only_for_members_of_the_project(factory) -> None:
    ana, bo, cy = await new_user(factory, "Ana"), await new_user(factory, "Bo"), await new_user(factory, "Cy")
    project = await new_project(factory, ana, bo)  # cy is no member
    async with factory() as session, session.begin():
        for user, account in ((ana, "acct-ana"), (bo, "acct-bo"), (cy, "acct-cy")):
            await session.execute(
                text(
                    "INSERT INTO atlassian_connections (user_id, cloud_id, account_id, account_name, scopes, "
                    "refresh_token, status, connected_at, last_used_at) "
                    "VALUES (:u, 'cloud-1', :a, 'N', 'read', :t, 'active', now(), now())"
                ),
                {"u": user, "a": account, "t": b"cipher"},
            )

    async with factory() as session:
        accounts = await jira_gaps.member_accounts(session, project)
        own = await jira_gaps.account_of(session, cy)
        none = await jira_gaps.account_of(session, uuid.uuid4())

    assert accounts == {"acct-ana": ana, "acct-bo": bo}
    assert own == "acct-cy" and none is None


async def test_a_write_is_guarded_by_the_jira_project_the_pass_started_with(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    await save(factory, project, jira_project="SHOP")

    async with factory() as session, session.begin():
        await jira_gaps.require_destination(session, project, "SHOP")
    await save(factory, project, jira_project="OTHER")
    for stale, who in (("SHOP", project), ("OTHER", uuid.uuid4())):
        with pytest.raises(jira_gaps.DestinationChangedError):
            async with factory() as session, session.begin():
                await jira_gaps.require_destination(session, who, stale)
