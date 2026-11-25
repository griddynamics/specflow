"""Tier 2: ``db.integrations`` and the member-removal nulling against a real Postgres (migrated scratch database, see
specflow_db_live_support). Skipped unless ``SPECFLOW_TEST_POSTGRES=1``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import integrations, members
from db.integrations import ATLASSIAN, GITHUB, SLACK, BindingHeldError, WorkspaceMismatchError
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

NOW = datetime(2026, 1, 1, tzinfo=UTC)
LATER = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def new_user(factory: async_sessionmaker[AsyncSession], name: str) -> uuid.UUID:
    user = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO users (id, actor_id, display_name, email) VALUES (:id, :actor, :name, :email)"),
            {"id": user, "actor": f"actor-{user}", "name": name, "email": f"{user}@example.com"},
        )
    return user


async def new_project(factory: async_sessionmaker[AsyncSession], owner: uuid.UUID, *others: uuid.UUID) -> uuid.UUID:
    project = uuid.uuid4()
    async with factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO projects (id, name, created_by) VALUES (:id, 'P', :user)"), {"id": project, "user": owner}
        )
        for user, role in ((owner, "owner"), *((other, "collaborator") for other in others)):
            await session.execute(
                text("INSERT INTO project_members (project_id, user_id, role) VALUES (:p, :u, :role)"),
                {"p": project, "u": user, "role": role},
            )
    return project


async def bind(factory, project, app, user, *, workspace="w1", at=NOW) -> None:
    async with factory() as session, session.begin():
        await integrations.bind_integration(
            session, project, app, user_id=user, workspace_id=workspace, workspace_name=workspace.upper(), now=at
        )


async def test_a_binding_is_read_back_with_the_display_name_of_its_member(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)

    await bind(factory, project, ATLASSIAN, ana, workspace="cloud-1")
    await bind(factory, project, SLACK, ana, workspace="T1")

    async with factory() as session:
        one = await integrations.get_integration(session, project, ATLASSIAN)
        listed = await integrations.list_integrations(session, project)
        none = await integrations.get_integration(session, project, GITHUB)
    assert none is None
    assert one is not None
    assert (one.user_id, one.user_name, one.workspace_id, one.workspace_name, one.connected_at) == (
        ana,
        "Ana",
        "cloud-1",
        "CLOUD-1",
        NOW,
    )
    assert [row.app for row in listed] == ["atlassian", "slack"]


async def test_a_member_of_the_workspace_takes_over_but_another_workspace_is_refused_while_a_member_acts(
    factory,
) -> None:
    ana, bo = await new_user(factory, "Ana"), await new_user(factory, "Bo")
    project = await new_project(factory, ana, bo)
    await bind(factory, project, GITHUB, ana, workspace="octo")

    await bind(factory, project, GITHUB, bo, workspace="octo", at=LATER)  # the same workspace: bo acts from now on
    with pytest.raises(WorkspaceMismatchError):
        await bind(factory, project, GITHUB, ana, workspace="other")

    async with factory() as session:
        row = await integrations.get_integration(session, project, GITHUB)
    assert row is not None and (row.user_id, row.workspace_id, row.connected_at) == (bo, "octo", LATER)


async def test_a_member_cannot_take_over_while_the_binders_connection_is_active_but_can_when_it_is_not(factory) -> None:
    ana, bo = await new_user(factory, "Ana"), await new_user(factory, "Bo")
    project = await new_project(factory, ana, bo)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO github_connections (user_id, github_user_id, login, refresh_token, status, connected_at, "
                "last_used_at) VALUES (:u, 1, 'ana', :t, 'active', now(), now())"
            ),
            {"u": ana, "t": b"cipher"},
        )
    await bind(factory, project, GITHUB, ana, workspace="octo")

    await bind(factory, project, GITHUB, ana, workspace="octo", at=LATER)  # the binder again: fine
    with pytest.raises(BindingHeldError):
        await bind(factory, project, GITHUB, bo, workspace="octo")
    async with factory() as session, session.begin():
        await session.execute(text("UPDATE github_connections SET status = 'expired' WHERE user_id = :u"), {"u": ana})
    await bind(factory, project, GITHUB, bo, workspace="octo")

    async with factory() as session:
        row = await integrations.get_integration(session, project, GITHUB)
    assert row is not None and row.user_id == bo


async def test_a_workspace_nobody_acts_on_is_replaced_by_the_next_member(factory) -> None:
    ana, bo = await new_user(factory, "Ana"), await new_user(factory, "Bo")
    project = await new_project(factory, ana, bo)
    await bind(factory, project, GITHUB, bo, workspace="octo")
    async with factory() as session, session.begin():
        await members.remove_member(session, project, bo)  # the member goes: the row stays, with no member

    async with factory() as session:
        row = await integrations.get_integration(session, project, GITHUB)
    assert row is not None and (row.user_id, row.user_name, row.workspace_id) == (None, None, "octo")
    await bind(factory, project, GITHUB, ana, workspace="other")

    async with factory() as session:
        row = await integrations.get_integration(session, project, GITHUB)
    assert row is not None and (row.user_id, row.workspace_id) == (ana, "other")


async def test_removing_or_leaving_nulls_the_member_of_every_integration_and_the_acting_accounts(factory) -> None:
    ana, bo, cy = await new_user(factory, "Ana"), await new_user(factory, "Bo"), await new_user(factory, "Cy")
    project = await new_project(factory, ana, bo, cy)
    other = await new_project(factory, bo)  # bo is the owner of another project: nothing of it changes
    for app in (ATLASSIAN, GITHUB, SLACK):
        await bind(factory, project, app, bo)
    await bind(factory, other, ATLASSIAN, bo)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO jira_targets (project_id, jira_project, enabled, updated_at, connected_user_id) "
                "VALUES (:p, 'SF', true, now(), :u)"
            ),
            {"p": project, "u": bo},
        )

    async with factory() as session, session.begin():
        await members.leave_project(session, project, bo)

    async with factory() as session:
        held = (await session.execute(text("SELECT project_id, user_id FROM project_integrations"))).all()
        acting = (await session.execute(text("SELECT connected_user_id FROM jira_targets"))).all()
    assert sorted(held, key=str) == sorted([(project, None)] * 3 + [(other, bo)], key=str)
    assert acting == [(None,)]


async def test_pointing_moves_the_jira_target_and_the_repositories_and_a_project_without_them_is_untouched(
    factory,
) -> None:
    ana, bo = await new_user(factory, "Ana"), await new_user(factory, "Bo")
    project = await new_project(factory, ana, bo)
    empty = await new_project(factory, ana)
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO jira_targets (project_id, jira_project, enabled, updated_at, connected_user_id) "
                "VALUES (:p, 'SF', true, now(), :u)"
            ),
            {"p": project, "u": ana},
        )
        for repo_id in (1, 2):
            await session.execute(
                text(
                    "INSERT INTO project_repositories (id, project_id, github_repo_id, full_name, html_url, private, "
                    "added_by) VALUES (gen_random_uuid(), :p, :r, 'o/r', 'https://github.com/o/r', true, :u)"
                ),
                {"p": project, "r": repo_id, "u": ana},
            )

    async with factory() as session, session.begin():
        await integrations.point_jira_target(session, project, bo)
        await integrations.point_repositories(session, project, bo)
        await integrations.point_jira_target(session, empty, bo)  # no target: nothing to move
        await integrations.point_repositories(session, empty, bo)

    async with factory() as session:
        target = (await session.execute(text("SELECT connected_user_id FROM jira_targets"))).all()
        added = (await session.execute(text("SELECT DISTINCT added_by FROM project_repositories"))).all()
    assert target == [(bo,)] and added == [(bo,)]


async def test_a_backfill_adds_a_missing_row_and_leaves_an_existing_one(factory) -> None:
    ana, bo = await new_user(factory, "Ana"), await new_user(factory, "Bo")
    project = await new_project(factory, ana, bo)
    values = {"workspace_id": "T1", "workspace_name": "Shop", "now": NOW}

    async with factory() as session, session.begin():
        first = await integrations.add_missing_integration(session, project, SLACK, user_id=ana, **values)
        again = await integrations.add_missing_integration(session, project, SLACK, user_id=bo, **values)

    async with factory() as session:
        row = await integrations.get_integration(session, project, SLACK)
    assert (first, again) == (True, False) and row is not None and row.user_id == ana


async def test_a_conditional_delete_removes_only_the_binding_of_that_member(factory) -> None:
    ana, bo = await new_user(factory, "Ana"), await new_user(factory, "Bo")
    project = await new_project(factory, ana, bo)
    await bind(factory, project, GITHUB, bo)  # bo took it over after ana's check

    async with factory() as session, session.begin():
        by_ana = await integrations.delete_integration(session, project, GITHUB, only_user=ana)
        by_bo = await integrations.delete_integration(session, project, GITHUB, only_user=bo)

    assert (by_ana, by_bo) == (False, True)


async def test_a_binding_is_removed_once_and_only_in_its_project(factory) -> None:
    ana = await new_user(factory, "Ana")
    project, other = await new_project(factory, ana), await new_project(factory, ana)
    await bind(factory, project, GITHUB, ana)
    await bind(factory, other, GITHUB, ana)

    async with factory() as session, session.begin():
        first = await integrations.delete_integration(session, project, GITHUB)
        again = await integrations.delete_integration(session, project, GITHUB)

    async with factory() as session:
        left = await integrations.list_integrations(session, other)
    assert (first, again) == (True, False) and [row.app for row in left] == ["github"]


async def test_the_database_refuses_an_unknown_app_and_a_second_row_of_the_same_app(factory) -> None:
    ana = await new_user(factory, "Ana")
    project = await new_project(factory, ana)
    insert = text(
        "INSERT INTO project_integrations (project_id, app, workspace_id, workspace_name, connected_at) "
        "VALUES (:p, :app, 'w', 'W', now())"
    )
    async with factory() as session, session.begin():
        await session.execute(insert, {"p": project, "app": GITHUB})

    for app in ("notion", GITHUB):
        with pytest.raises(IntegrityError):
            async with factory() as session, session.begin():
                await session.execute(insert, {"p": project, "app": app})
