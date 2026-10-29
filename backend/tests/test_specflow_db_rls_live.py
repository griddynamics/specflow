"""Tier 2: row-level security, grants and constraints (AC-3.3, AC-3.4, AC-3.6).

Run as the session test agent role ``specflow_agent_test_<hex8>`` on a migrated scratch
database (see specflow_db_live_support). ``SPECFLOW_TEST_POSTGRES=1``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.agent_scope import (
    agent_scope,
    build_agent_session_factory,
    dispose_agent_session_factory,
)
from db.models import Document, Project, ProjectMember, User
from db.session import build_engine, build_session_factory
from settings import DatabaseSettings
from specflow_db_live_support import (
    AgentRole,
)
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")


@dataclass(frozen=True)
class Seed:
    project_a: uuid.UUID
    project_b: uuid.UUID
    doc_a: uuid.UUID
    doc_b: uuid.UUID


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def document(project_id: uuid.UUID, label: str, **extra: object) -> Document:
    return Document(
        project_id=project_id,
        content_hash=sha(label),
        original_filename=f"{label}.pdf",
        media_type="application/pdf",
        size_bytes=10,
        origin="upload",
        original_key=f"projects/{project_id}/documents/x/original.pdf",
        markdown=f"markdown of {label}",
        summarization=f"summary of {label}",
        **extra,
    )


@pytest_asyncio.fixture
async def app_factory(
    migrated_database: DatabaseSettings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def seed(app_factory: async_sessionmaker[AsyncSession]) -> Seed:
    async with app_factory() as session, session.begin():
        user = User(actor_id="human:seed", display_name="Seed", email="seed@example.com")
        session.add(user)
        await session.flush()
        projects = [Project(name=name, created_by=user.id) for name in ("A", "B")]
        session.add_all(projects)
        await session.flush()
        session.add_all(ProjectMember(project_id=p.id, user_id=user.id) for p in projects)
        docs = [document(projects[0].id, "alpha"), document(projects[1].id, "bravo")]
        session.add_all(docs)
    return Seed(projects[0].id, projects[1].id, docs[0].id, docs[1].id)


@pytest_asyncio.fixture
async def agent_factory(
    migrated_database: DatabaseSettings, agent_role: AgentRole
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = build_agent_session_factory(migrated_database, agent_role.settings())
    try:
        yield factory
    finally:
        await dispose_agent_session_factory(factory)


async def scoped_ids(factory: async_sessionmaker[AsyncSession], pid: uuid.UUID) -> set:
    async with agent_scope(factory, pid) as session:
        return set((await session.scalars(text("SELECT id FROM documents"))).all())


async def assert_denied(factory: async_sessionmaker[AsyncSession], pid: uuid.UUID, statement: str) -> None:
    with pytest.raises(DBAPIError) as caught:
        async with agent_scope(factory, pid) as session:
            await session.execute(text(statement))
    assert "permission denied" in str(caught.value.orig)


# AC-3.3 ------------------------------------------------------------------------


async def test_unscoped_agent_sessions_see_no_documents(
    agent_factory: async_sessionmaker[AsyncSession], seed: Seed
) -> None:
    async with agent_factory() as session:
        rows = (await session.execute(text("SELECT id FROM documents"))).all()
    assert rows == []


async def test_a_scope_sees_only_its_own_project(agent_factory: async_sessionmaker[AsyncSession], seed: Seed) -> None:
    assert await scoped_ids(agent_factory, seed.project_a) == {seed.doc_a}
    assert await scoped_ids(agent_factory, seed.project_b) == {seed.doc_b}
    assert await scoped_ids(agent_factory, uuid.uuid4()) == set()

    async with agent_scope(agent_factory, seed.project_a) as session:
        # Even an explicit filter on B's ids returns nothing in scope A.
        rows = await session.execute(text("SELECT markdown FROM documents WHERE id = :d"), {"d": seed.doc_b})
        assert rows.all() == []
        markdown = await session.scalar(text("SELECT markdown FROM documents WHERE id = :d"), {"d": seed.doc_a})
    assert markdown == "markdown of alpha"


async def test_the_scope_does_not_leak_to_the_next_transaction(
    agent_factory: async_sessionmaker[AsyncSession], seed: Seed
) -> None:
    async with agent_scope(agent_factory, seed.project_a) as session:
        scoped_pid = await session.scalar(text("SELECT pg_backend_pid()"))
        assert len((await session.execute(text("SELECT id FROM documents"))).all()) == 1

    async with agent_factory() as session:
        # The pool hands back the same server connection.
        assert await session.scalar(text("SELECT pg_backend_pid()")) == scoped_pid
        setting = await session.scalar(text("SELECT current_setting('app.project_id', true)"))
        rows = (await session.execute(text("SELECT id FROM documents"))).all()
    assert setting in (None, "")
    assert rows == []


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT id FROM projects",
        "SELECT project_id FROM project_members",
        "SELECT id FROM users",
        "SELECT summarization FROM documents",
        "SELECT summarization_model FROM documents",
        "SELECT * FROM documents",
        "INSERT INTO documents (id) VALUES (gen_random_uuid())",
        "UPDATE documents SET status = 'failed'",
        "UPDATE documents SET markdown = 'x'",
        "DELETE FROM documents",
    ],
)
async def test_the_agent_role_is_denied_everything_else(
    agent_factory: async_sessionmaker[AsyncSession], seed: Seed, statement: str
) -> None:
    await assert_denied(agent_factory, seed.project_a, statement)


async def test_the_test_role_has_the_required_flags(
    app_factory: async_sessionmaker[AsyncSession], agent_role: AgentRole
) -> None:
    async with app_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT rolcanlogin, rolbypassrls, rolsuper, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = :n"
                ),
                {"n": agent_role.name},
            )
        ).one()
    assert tuple(row) == (True, False, False, False, False)


# AC-3.4 ------------------------------------------------------------------------


async def test_the_app_role_is_not_affected_by_rls(app_factory: async_sessionmaker[AsyncSession], seed: Seed) -> None:
    async with app_factory() as session, session.begin():
        ids = set((await session.scalars(text("SELECT id FROM documents"))).all())
        summaries = (await session.scalars(text("SELECT summarization FROM documents"))).all()
        updated = await session.execute(text("UPDATE documents SET error = NULL"))
        projects = (await session.scalars(text("SELECT id FROM projects"))).all()

    assert ids == {seed.doc_a, seed.doc_b}
    assert sorted(summaries) == ["summary of alpha", "summary of bravo"]
    assert updated.rowcount == 2
    assert len(projects) == 2


# AC-3.6 ------------------------------------------------------------------------


async def insert_fails(factory: async_sessionmaker[AsyncSession], item: Document) -> IntegrityError:
    with pytest.raises(IntegrityError) as caught:
        async with factory() as session, session.begin():
            session.add(item)
    return caught.value


async def test_the_same_content_twice_in_one_project_is_rejected(
    app_factory: async_sessionmaker[AsyncSession], seed: Seed
) -> None:
    error = await insert_fails(app_factory, document(seed.project_a, "alpha"))
    assert "uq_documents_project_id_content_hash" in str(error.orig)

    # The same content in another project is fine.
    async with app_factory() as session, session.begin():
        session.add(document(seed.project_b, "alpha"))


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("status", "done", "ck_documents_status"),
        ("summarization_status", "queued", "ck_documents_summarization_status"),
        ("content_hash", "A" * 64, "ck_documents_content_hash_sha256"),
    ],
)
async def test_check_constraints_reject_unknown_values(
    app_factory: async_sessionmaker[AsyncSession],
    seed: Seed,
    field: str,
    value: str,
    constraint: str,
) -> None:
    item = document(seed.project_a, f"check-{field}")
    setattr(item, field, value)

    error = await insert_fails(app_factory, item)

    assert constraint in str(error.orig)


async def test_new_documents_default_to_uploaded_and_no_summary(
    app_factory: async_sessionmaker[AsyncSession], seed: Seed
) -> None:
    async with app_factory() as session:
        row = await session.get(Document, seed.doc_a)
        assert row is not None
        assert (row.status, row.summarization_status, row.version) == ("uploaded", "none", 1)
