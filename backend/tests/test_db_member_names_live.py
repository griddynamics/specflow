"""Live tier of ``db.repositories.member_names`` (a fresh migrated scratch database per test,
``specflow_db_live_support``).

Gate: ``SPECFLOW_TEST_POSTGRES=1`` (see ``specflow_live``). Run with ``uv run --env-file ../.env.local``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db import repositories as repo
from db.models import ProjectMember
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")


class Principal:
    """The three attributes ``upsert_user`` reads of a verified token."""

    def __init__(self, email: str, name: str | None) -> None:
        self.subject, self.email, self.name = f"user:{email}", email, name


@pytest_asyncio.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


async def test_names_come_from_the_projects_members_only_and_the_email_fallback_is_none(factory):
    """ana (named) and bob (display name = email) are members of the project, cy is a member of another one only, and a
    random id is nobody: one query answers ``Ana``, ``None``, ``None``, ``None``."""

    async with factory() as session, session.begin():
        ana = await repo.upsert_user(session, Principal("ana@example.com", "Ana"))
        bob = await repo.upsert_user(session, Principal("bob@example.com", None))
        cy = await repo.upsert_user(session, Principal("cy@example.com", "Cy"))
        project = await repo.create_project(session, ana.id, "Alpha")
        other = await repo.create_project(session, cy.id, "Beta")
        session.add(ProjectMember(project_id=project.id, user_id=bob.id, role="owner"))
        await session.flush()
        names = await repo.member_names(session, project.id, {ana.id, bob.id, cy.id, uuid.uuid4()})
        elsewhere = await repo.member_names(session, other.id, [cy.id, ana.id])
    assert [names[ana.id], names[bob.id], names[cy.id]] == ["Ana", None, None]
    assert sorted(value for value in names.values() if value is not None) == ["Ana"] and len(names) == 4
    assert elsewhere == {cy.id: "Cy", ana.id: None}
