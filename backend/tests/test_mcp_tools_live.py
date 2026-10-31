"""Live data tier of the MCP tools (docs/mcp-server/testing.md): real Postgres and real Neo4j behind the real registry.

Gates: ``SPECFLOW_TEST_POSTGRES=1`` for the membership and user-row tests (a fresh migrated scratch database per test,
``specflow_db_live_support``), ``SPECFLOW_TEST_NEO4J=1`` for the graph tests (unique ``pytest-graph-`` project ids,
deleted afterwards, nothing outside them touched; ``graph_support.neo4j_project``). Run with
``uv run --env-file ../.env.local``. The QA seed projects are never read or written: every id is made by the test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.mcp.registry import install
from db import repositories
from db.models import ProjectMember, User
from db.session import build_engine, build_session_factory
from graph.core import GraphReader, Ontology
from graph_support import (
    RENAMED_FEATURE_LABEL,
    RENAMED_FEATURE_PREFIX,
    Neo4jProject,
    load_feature_renamed_ontology,
    shipped_ontology,
)
from mcp_support import (
    EXTREME_PROJECT_CURSOR,
    MCP_URL,
    build_app,
    call,
    code,
    fake_runtime,
    feature_ids,
    mcp_dev_token,
    mcp_env,
    owned_project,
    sdk_session,
    structured,
    tool_app,
)
from settings import DatabaseSettings
from specflow_live import requires_live
from test_graph_reader import ScriptedExecutor, paged_executor
from test_graph_reader_live import create

pytest_plugins = ["graph_support"]

ALICE = mcp_dev_token("alice@example.com")
FEATURE = "Feature"


# --- Postgres: FR-MCP-ACC-04, ACC-05 ---------------------------------------------------------------


@pytest_asyncio.fixture
async def factory(migrated_database: DatabaseSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


def postgres_app(
    valid_env: dict[str, str], factory: async_sessionmaker[AsyncSession], executor: ScriptedExecutor
) -> Any:
    """The registry app over the real repositories on the scratch database; the graph is a recording executor."""

    ontology = shipped_ontology()
    runtime = fake_runtime(reader=GraphReader(executor, ontology), session_factory=factory, ontology=ontology)
    return build_app(mcp_env(valid_env), runtime=runtime, tools=lambda server: install(server, resource_url=MCP_URL))


async def users(factory: async_sessionmaker[AsyncSession]) -> list[User]:
    async with factory() as session:
        return list((await session.scalars(select(User))).all())


@requires_live("POSTGRES")
async def test_fr_mcp_acc_04_a_membership_row_deleted_between_two_calls_denies_the_second(
    valid_env: dict[str, str], factory: async_sessionmaker[AsyncSession]
) -> None:
    """FR-MCP-ACC-04: no cache: alice reads her project, the test deletes her ``project_members`` row directly, the
    next call on the same client is ``project_not_found`` and reads nothing more from the graph."""

    executor = paged_executor(["FEAT::A"], FEATURE)
    app = postgres_app(valid_env, factory, executor)
    async with factory() as session, session.begin():
        user = await repositories.upsert_user(session, SimplePrincipal("dev:alice@example.com", "alice@example.com"))
        project = await repositories.create_project(session, user.id, "Alpha")
    async with sdk_session(app, ALICE) as client:
        first = await call(client, "list_features", project_id=str(project.id))
        graph_calls = len(executor.calls)
        async with factory() as session, session.begin():
            await session.execute(delete(ProjectMember).where(ProjectMember.project_id == project.id))
        second = await call(client, "list_features", project_id=str(project.id))
        listed = await call(client, "list_projects")
    assert feature_ids(first) == ["FEAT::A"] and graph_calls > 0
    assert code(second) == "project_not_found"
    assert len(executor.calls) == graph_calls  # the denied call reached no graph query
    assert structured(listed) == {"items": [], "next_cursor": None}, listed


@requires_live("POSTGRES")
async def test_fr_mcp_acc_03_a_foreign_project_is_denied_by_the_real_membership_predicate(
    valid_env: dict[str, str], factory: async_sessionmaker[AsyncSession]
) -> None:
    """FR-MCP-ACC-02 / ACC-03 / ACC-04: bob owns a real project (real ``project_members`` row) and alice asks for it:
    the real ``get_member_project`` finds no row for her, so the answer is byte-identical to a random UUID's and the
    recording graph executor saw no query. Alice's own project, in the same database, still reads."""

    executor = paged_executor(["FEAT::A"], FEATURE)
    app = postgres_app(valid_env, factory, executor)
    async with factory() as session, session.begin():
        alice = await repositories.upsert_user(session, SimplePrincipal("dev:alice@example.com", "alice@example.com"))
        bob = await repositories.upsert_user(session, SimplePrincipal("dev:bob@example.com", "bob@example.com"))
        mine = await repositories.create_project(session, alice.id, "Alpha")
        theirs = await repositories.create_project(session, bob.id, "Beta")
    async with sdk_session(app, ALICE) as client:
        foreign = await call(client, "list_features", project_id=str(theirs.id))
        unknown = await call(client, "list_features", project_id=str(uuid.uuid4()))
        calls_before_own = len(executor.calls)
        own = await call(client, "list_features", project_id=str(mine.id))
    assert code(foreign) == "project_not_found"
    assert foreign.model_dump(mode="json", by_alias=True) == unknown.model_dump(mode="json", by_alias=True)
    assert calls_before_own == 0  # neither denial reached the graph
    assert feature_ids(own) == ["FEAT::A"]


@requires_live("POSTGRES")
async def test_fr_mcp_acc_05_a_new_subject_has_one_user_row_even_when_every_call_is_denied(
    valid_env: dict[str, str], factory: async_sessionmaker[AsyncSession]
) -> None:
    """FR-MCP-ACC-05: two denied calls and two ``list_projects`` calls leave exactly one ``users`` row (the row is
    committed before the denial and found again by ``auth_subject``)."""

    app = postgres_app(valid_env, factory, paged_executor([], FEATURE))
    async with sdk_session(app, ALICE) as client:
        denied = [await call(client, "list_features", project_id=str(uuid.uuid4())) for _ in range(2)]
        assert len(await users(factory)) == 1  # kept although both calls answered project_not_found
        listed = [await call(client, "list_projects") for _ in range(2)]
    assert [code(r) for r in denied] == ["project_not_found"] * 2
    assert [structured(r)["items"] for r in listed] == [[], []]
    (row,) = await users(factory)
    assert row.auth_subject == "dev:alice@example.com"


@requires_live("POSTGRES")
async def test_fr_mcp_tool_01_list_projects_pages_real_rows_newest_first_and_only_the_callers(
    valid_env: dict[str, str], factory: async_sessionmaker[AsyncSession]
) -> None:
    """FR-MCP-TOOL-01 / ACC-01: real ``list_projects``: three of alice's projects in two pages, bob's never listed."""

    app = postgres_app(valid_env, factory, paged_executor([], FEATURE))
    async with factory() as session, session.begin():
        alice = await repositories.upsert_user(session, SimplePrincipal("dev:alice@example.com", "alice@example.com"))
        bob = await repositories.upsert_user(session, SimplePrincipal("dev:bob@example.com", "bob@example.com"))
        theirs = await repositories.create_project(session, bob.id, "bobs")
    for name in ("one", "two", "three"):  # one transaction each: ``created_at`` is the transaction's time
        async with factory() as session, session.begin():
            await repositories.create_project(session, alice.id, name)
    async with sdk_session(app, ALICE) as client:
        first = await call(client, "list_projects", limit=2)
        second = await call(client, "list_projects", limit=2, cursor=structured(first)["next_cursor"])
        extreme = await call(client, "list_projects", cursor=EXTREME_PROJECT_CURSOR)
        tampered = await call(client, "list_projects", cursor=repositories.encode_cursor(theirs.created_at, theirs.id))
    names = [i["name"] for i in structured(first)["items"] + structured(second)["items"]]
    assert names == ["three", "two", "one"] and structured(second)["next_cursor"] is None
    assert str(theirs.id) not in str(structured(first)) + str(structured(second))
    # a cursor built from bob's project is only a position: it lists alice's older rows, never bob's
    assert not tampered.is_error and str(theirs.id) not in str(structured(tampered))
    assert {i["name"] for i in structured(tampered)["items"]} <= {"one", "two", "three"}
    assert code(extreme) == "invalid_cursor"  # F1: an out-of-range UTC instant is a bad cursor, not a database error


class SimplePrincipal:
    """The three attributes ``upsert_user`` reads of a verified token."""

    def __init__(self, subject: str, email: str) -> None:
        self.subject, self.email, self.name = subject, email, None


# --- Neo4j: FR-MCP-TOOL-02, TOOL-03, TOOL-05, TOOL-06 ----------------------------------------------


async def seeded_app(
    monkeypatch: pytest.MonkeyPatch,
    valid_env: dict[str, str],
    neo4j_project: Neo4jProject,
    ontology: Ontology | None = None,
) -> tuple[Any, str]:
    """The registry app over the real reader; alice owns one project whose graph id the test hands to Neo4j cleanup."""

    used = ontology or neo4j_project.ontology
    app, repo = tool_app(monkeypatch, valid_env, None, ontology=used, reader=neo4j_project.reader(ontology=used))
    project = await owned_project(repo, "alice@example.com", "Live")
    neo4j_project.project_ids.append(str(project.id))
    return app, str(project.id)


@requires_live("NEO4J")
async def test_fr_mcp_tool_02_only_feature_nodes_are_listed_with_their_stored_attributes(
    monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str], neo4j_project: Neo4jProject
) -> None:
    """FR-MCP-TOOL-02 / TOOL-05: features, a module and requirements in one graph: only ``FEAT::`` ids come back, items
    hold ``spec_id``, ``name`` and the stored attributes (``stmt``, ``quote``), no embedding or other property."""

    app, pid = await seeded_app(monkeypatch, valid_env, neo4j_project)
    await create(
        neo4j_project,
        pid,
        FEATURE,
        [
            {"key": "FEAT::B", "name": "Beta", "stmt": "Does B.", "quote": "B shall.", "name_embedding": [0.5, 0.5]},
            {"key": "FEAT::A", "name": "Alpha", "mystery": "UNKNOWN-PROP"},
        ],
    )
    await create(neo4j_project, pid, "Module", [{"key": "MOD::M"}])
    await create(neo4j_project, pid, "FunctionalRequirement", [{"key": "FR-001"}, {"key": "FR-002"}])
    async with sdk_session(app, ALICE) as client:
        result = await call(client, "list_features", project_id=pid)
    assert not result.is_error
    assert feature_ids(result) == ["FEAT::A", "FEAT::B"]
    assert structured(result)["total_count"] == 2 and structured(result)["project_id"] == pid
    alpha, beta = structured(result)["items"]
    assert alpha == {"spec_id": "FEAT::A", "name": "Alpha", "attributes": {}}  # absent attributes are left out
    assert beta == {"spec_id": "FEAT::B", "name": "Beta", "attributes": {"stmt": "Does B.", "quote": "B shall."}}
    assert "UNKNOWN-PROP" not in str(result.content) and "0.5" not in str(result.content)


@requires_live("NEO4J")
async def test_fr_mcp_tool_03_real_counts_and_pages_in_spec_id_order(
    monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str], neo4j_project: Neo4jProject
) -> None:
    """FR-MCP-TOOL-03 / TOOL-04: seven features, ``limit=5``: five then two, ``total_count`` 7 on both, ``spec_id``
    order across them (``FEAT::F10`` sorts before ``FEAT::F9``), the last ``next_cursor`` null; limit 1 walks all."""

    app, pid = await seeded_app(monkeypatch, valid_env, neo4j_project)
    keys = ["FEAT::F1", "FEAT::F2", "FEAT::F3", "FEAT::F9", "FEAT::F10", "FEAT::F11", "FEAT::F4"]
    await create(neo4j_project, pid, FEATURE, [{"key": key} for key in keys])
    async with sdk_session(app, ALICE) as client:
        first = await call(client, "list_features", project_id=pid, limit=5)
        second = await call(client, "list_features", project_id=pid, limit=5, cursor=structured(first)["next_cursor"])
        walked: list[str] = []
        cursor = None
        while True:
            page = await call(client, "list_features", project_id=pid, limit=1, cursor=cursor)
            walked += feature_ids(page)
            cursor = structured(page)["next_cursor"]
            if cursor is None:
                break
    assert feature_ids(first) + feature_ids(second) == sorted(keys)
    assert (len(feature_ids(first)), len(feature_ids(second))) == (5, 2)
    assert (structured(first)["total_count"], structured(second)["total_count"]) == (7, 7)
    assert structured(first)["next_cursor"] and structured(second)["next_cursor"] is None
    assert walked == sorted(keys)


@requires_live("NEO4J")
async def test_fr_mcp_tool_03_a_project_without_a_graph_is_empty_and_another_projects_nodes_stay_out(
    monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str], neo4j_project: Neo4jProject
) -> None:
    """FR-MCP-TOOL-03 / ACC-02: nodes of a second project id are not listed for alice's project; hers has none."""

    app, pid = await seeded_app(monkeypatch, valid_env, neo4j_project)
    other = neo4j_project.new_project()
    await create(neo4j_project, other, FEATURE, [{"key": "FEAT::OTHER"}])
    async with sdk_session(app, ALICE) as client:
        result = await call(client, "list_features", project_id=pid)
    assert (structured(result)["total_count"], structured(result)["items"], structured(result)["next_cursor"]) == (
        0,
        [],
        None,
    )


@requires_live("NEO4J")
async def test_fr_mcp_tool_06_the_renamed_feature_ontology_lists_only_its_own_label(
    monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str], neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    """FR-MCP-TOOL-06: under the derived ontology (``Funktion``, ``FUNK::``, type key ``feature``) the tool returns the
    ``Funktion`` nodes only; ``Feature`` nodes of the shipped label are not features here. (Startup refusal without a
    ``feature`` type: ``test_mcp_app.py::test_an_ontology_without_a_feature_type_refuses_the_start``.)"""

    renamed = load_feature_renamed_ontology(tmp_path)
    app, pid = await seeded_app(monkeypatch, valid_env, neo4j_project, renamed)
    await create(neo4j_project, pid, RENAMED_FEATURE_LABEL, [{"key": f"{RENAMED_FEATURE_PREFIX}{c}"} for c in "ABC"])
    await create(neo4j_project, pid, FEATURE, [{"key": "FEAT::X"}, {"key": "FEAT::Y"}])
    async with sdk_session(app, ALICE) as client:
        first = await call(client, "list_features", project_id=pid, limit=2)
        second = await call(client, "list_features", project_id=pid, limit=2, cursor=structured(first)["next_cursor"])
    assert feature_ids(first) + feature_ids(second) == ["FUNK::A", "FUNK::B", "FUNK::C"]
    assert structured(first)["total_count"] == 3 and structured(second)["next_cursor"] is None
