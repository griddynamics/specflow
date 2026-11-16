"""Tier 2: the PostgreSQL adapters of ``GapRecords`` and ``Amendments`` (``api.gap.records``) on a real database.

The relational port contract (``gap_records_contract``, also run on the fakes) on a scratch database migrated to
head, with two projects (FR-GOWN-02, FR-GOWN-04), plus what only the database enforces: the gap tables' row-level
security policy and no grant to the agent role, one recommended option per gap, the option rank range and the
project foreign key. Skipped unless ``SPECFLOW_TEST_POSTGRES=1``:

    SPECFLOW_TEST_POSTGRES=1 uv run --env-file ../.env.local pytest tests/test_api_gap_records_live.py

The scratch database (``specflow_pytest_<hex>``) is dropped afterwards; the dev database is never touched.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
from pydantic import BaseModel
from sqlalchemy import literal, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.gap.records import PostgresAmendments, PostgresGapRecords
from db import gap as gap_repo
from db import repositories as repo
from db.agent_scope import agent_scope, build_agent_session_factory, dispose_agent_session_factory
from db.base import Base
from db.models import GapEvent, GapNoiseRule, GapNote, GapRun, GapSeed, GapSite
from db.session import build_engine, build_session_factory, transaction
from gap_records_contract import CASES, Backend, Case, option, run
from graph.core.errors import ConformanceError
from graph.gap.models import EventRow, NoiseRuleRow, NoteRow, RunRow, SeedRow, SiteRow
from settings import DatabaseSettings
from specflow_db_live_support import AgentRole, query_column
from specflow_live import requires_live

pytestmark = requires_live("POSTGRES")

GAP_TABLES = (
    "corpus_amendment",
    "gap_event",
    "gap_noise_rule",
    "gap_note",
    "gap_option",
    "gap_run",
    "gap_section_relevance",
    "gap_seed",
    "gap_site",
    "gap_suppression",
    "gap_verdict",
)


@dataclass(frozen=True)
class Principal:
    subject: str
    email: str
    name: str | None


@dataclass(frozen=True)
class World:
    factory: async_sessionmaker[AsyncSession]
    project_a: str
    project_b: str

    def transaction(self) -> Any:
        return transaction(self.factory)


@pytest_asyncio.fixture
async def world(migrated_database: DatabaseSettings) -> AsyncIterator[World]:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            user = await repo.upsert_user(session, Principal("gap", "gap@example.com", "Gap"))
            a = await repo.create_project(session, user.id, "A")
            b = await repo.create_project(session, user.id, "B")
        yield World(factory, str(a.id), str(b.id))
    finally:
        await engine.dispose()


def _row[M: BaseModel](model: type[M], values: Any) -> M:
    return model.model_validate({name: values[name] for name in model.model_fields})


class PostgresProbe:
    """Reads back the write-only rows of a project; stores noise rules (no port call writes them yet)."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def _rows[M: BaseModel](self, table: Any, model: type[M], project_id: str) -> Sequence[M]:
        async with self.world.transaction() as session:
            rows = (await session.execute(select(table).where(table.c.project_id == uuid.UUID(project_id)))).mappings()
            return tuple(_row(model, row) for row in rows)

    async def runs(self, project_id: str) -> Sequence[RunRow]:
        return await self._rows(GapRun.__table__, RunRow, project_id)

    async def seeds(self, project_id: str) -> Sequence[SeedRow]:
        return await self._rows(GapSeed.__table__, SeedRow, project_id)

    async def sites(self, project_id: str) -> Sequence[SiteRow]:
        return await self._rows(GapSite.__table__, SiteRow, project_id)

    async def notes(self, project_id: str) -> Sequence[NoteRow]:
        return await self._rows(GapNote.__table__, NoteRow, project_id)

    async def events(self, project_id: str) -> Sequence[EventRow]:
        return await self._rows(GapEvent.__table__, EventRow, project_id)

    async def add_noise_rule(self, project_id: str, row: NoiseRuleRow) -> None:
        async with self.world.transaction() as session:
            session.add(GapNoiseRule(project_id=uuid.UUID(project_id), **row.model_dump()))


@pytest.mark.parametrize("case", CASES, ids=[case.__name__ for case in CASES])
async def test_fr_gown_02_the_postgres_adapters_keep_the_relational_port_contract(case: Case, world: World) -> None:
    backend = Backend(
        PostgresGapRecords(world.transaction),
        PostgresAmendments(world.transaction),
        PostgresProbe(world),
        world.project_a,
        world.project_b,
    )
    await case(backend)


def test_fr_gown_04_every_gap_table_has_the_agent_policy_and_row_level_security(
    migrated_database: DatabaseSettings,
) -> None:
    secured = query_column(
        migrated_database,
        "SELECT relname FROM pg_class WHERE relkind = 'r' AND relrowsecurity "
        "AND (relname LIKE 'gap%' OR relname = 'corpus_amendment') ORDER BY relname",
    )
    policies = query_column(
        migrated_database,
        "SELECT tablename FROM pg_policies WHERE policyname = 'agent_project_scope' "
        "AND (tablename LIKE 'gap%' OR tablename = 'corpus_amendment') ORDER BY tablename",
    )

    assert tuple(secured) == GAP_TABLES
    assert tuple(policies) == GAP_TABLES


async def test_the_agent_role_has_no_grant_on_the_gap_tables(
    migrated_database: DatabaseSettings, agent_role: AgentRole, world: World
) -> None:
    factory = build_agent_session_factory(migrated_database, agent_role.settings())
    try:
        for table in GAP_TABLES:
            with pytest.raises(DBAPIError) as caught:
                async with agent_scope(factory, uuid.UUID(world.project_a)) as session:
                    await session.execute(select(literal(1)).select_from(Base.metadata.tables[table]))
            assert "permission denied" in str(caught.value.orig)
    finally:
        await dispose_agent_session_factory(factory)


async def test_the_database_keeps_one_recommended_option_and_the_rank_range(world: World) -> None:
    """The port refuses such sets first (contract); the table refuses them too, for any other writer."""

    project = uuid.UUID(world.project_a)
    option_values = option(1).model_dump(mode="json", exclude={"gap_id"})
    refusals = (
        (
            "ix_gap_option_one_recommended",
            [option_values | {"recommended": True}, option_values | {"rank": 2, "recommended": True}],
        ),
        ("ck_gap_option_rank", [option_values | {"rank": 5}]),
    )
    for name, rows in refusals:
        with pytest.raises(IntegrityError, match=name):
            async with world.transaction() as session:
                await gap_repo.replace_options(session, project, "GAP::1", rows)
    # A refused write changed nothing.
    assert await PostgresGapRecords(world.transaction).options(world.project_a, "GAP::1") == ()


async def test_rows_need_a_known_project(world: World) -> None:
    records = PostgresGapRecords(world.transaction)

    with pytest.raises(IntegrityError, match="fk_gap_run_project_id_projects"):
        await records.start_run(str(uuid.uuid4()), run())
    with pytest.raises(ConformanceError, match="gap_project_id_invalid"):
        await records.options("not-a-uuid", "GAP::1")
