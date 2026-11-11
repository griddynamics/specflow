"""Live data tier of the API agent routes (docs/mcp-server/testing.md, Live): real Postgres and a real graph store
behind the real routes, mirroring the MCP live tier for the API side.

Gates (as for the MCP live tier): ``SPECFLOW_TEST_POSTGRES=1`` for the membership and user-row tests (a fresh
migrated scratch database per test, ``specflow_db_live_support``), ``SPECFLOW_TEST_GRAPH_STORE`` for the graph tests
(each test writes under the raw UUID of a project it creates in the ``FakeRepo``, unique per test and registered with
``graph_support.graph_project`` to be deleted afterwards; nothing outside it is touched). Run with
``uv run --env-file ../.env.local``. The QA seed projects are never read or written: every id is made by the test.
The one ungated test points the real driver at a closed port (no server needed).
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

from agent_contracts import AGENT_ROUTES
from db import repositories
from db.models import ProjectMember, User
from db.session import build_engine, build_session_factory
from graph_probes import requires_live_graph
from graph_probes.base import NodeSpec
from graph_support import (
    RENAMED_FEATURE_LABEL,
    RENAMED_FEATURE_PREFIX,
    GraphProject,
    live_embedding_dim,
    load_feature_renamed_ontology,
)
from settings import DatabaseSettings, load_settings
from specflow_api_agent_support import (
    EXTREME_PROJECT_CURSOR,
    USER_HEADER,
    agent_env,
    owned_project,
    serve_agent,
)
from specflow_live import requires_live

FEATURE = "Feature"
LIST_PROJECTS = AGENT_ROUTES["list_projects"]
LIST_FEATURES = AGENT_ROUTES["list_features"]


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


@pytest_asyncio.fixture
async def factory(
    migrated_database: DatabaseSettings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_database)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


class Principal:
    """The three attributes ``upsert_user`` reads of a verified token."""

    def __init__(self, email: str) -> None:
        self.subject, self.email, self.name = f"user:{email}", email, None


async def users(factory: async_sessionmaker[AsyncSession]) -> list[User]:
    async with factory() as session:
        return list((await session.scalars(select(User))).all())


def flipped(key: str) -> str:
    """``key`` mirrored character by character, so a node uuid sorts opposite to its ``spec_id``."""

    return "".join(chr(0x2FFF - ord(char)) for char in key)


async def create(project: GraphProject, pid: str, label: str, rows: list[dict[str, Any]]) -> None:
    """Spec nodes of ``label``: each row's ``key`` is its ``spec_id`` (its uuid sorts opposite, so a tie-break by uuid
    would show), ``name`` defaults to the key, other fields become properties."""

    await project.create_nodes(
        [
            NodeSpec(
                ("SpecNode", label),
                {
                    "uuid": f"{pid}-{flipped(row['key'])}",
                    "spec_id": row["key"],
                    "name": row.get("name") or row["key"],
                    **{k: v for k, v in row.items() if k not in ("key", "name")},
                },
            )
            for row in rows
        ],
        project_id=pid,
    )


def feature_ids(response: Any) -> list[str]:
    return [item["spec_id"] for item in response.json()["items"]]


# --- Postgres: FR-MCP-ACC-02 to -05 on the agent routes ------------------------------------------------------------


@requires_live("POSTGRES")
async def test_a_foreign_project_is_denied_by_the_real_membership_predicate_byte_identical_to_an_unknown_one(
    monkeypatch, env, factory
):
    """bob owns a real project (real ``project_members`` row), alice asks for it: the answer equals a random UUID's,
    byte    for byte, the recording graph saw no query, and her own project in the same database still reads."""

    async with factory() as session, session.begin():
        alice = await repositories.upsert_user(session, Principal("alice@example.com"))
        bob = await repositories.upsert_user(session, Principal("bob@example.com"))
        mine = await repositories.create_project(session, alice.id, "Alpha")
        theirs = await repositories.create_project(session, bob.id, "Beta")
    async with serve_agent(monkeypatch, env, session_factory=factory) as rig:
        foreign = await rig.get(LIST_FEATURES, project_id=str(theirs.id))
        unknown = await rig.get(LIST_FEATURES, project_id=str(uuid.uuid4()))
        malformed = await rig.get(LIST_FEATURES, project_id="x/../y")
        calls_before_own = len(rig.executor.calls)
        own = await rig.get(LIST_FEATURES, project_id=str(mine.id))
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "project_not_found"
    assert (
        (foreign.status_code, foreign.content, foreign.headers[USER_HEADER])
        == (
            unknown.status_code,
            unknown.content,
            unknown.headers[USER_HEADER],
        )
        == (malformed.status_code, malformed.content, malformed.headers[USER_HEADER])
    )
    assert calls_before_own == 0  # no denial reached the graph
    assert own.status_code == 200 and own.headers[USER_HEADER] == str(alice.id)


@requires_live("POSTGRES")
async def test_a_membership_row_deleted_between_two_calls_denies_the_second_without_a_cache(monkeypatch, env, factory):
    """FR-MCP-ACC-04: alice reads her project, the test deletes her ``project_members`` row directly, the next call is
    ``project_not_found`` and reads nothing more from the graph."""

    async with factory() as session, session.begin():
        user = await repositories.upsert_user(session, Principal("alice@example.com"))
        project = await repositories.create_project(session, user.id, "Alpha")
    async with serve_agent(monkeypatch, env, session_factory=factory) as rig:
        first = await rig.get(LIST_FEATURES, project_id=str(project.id))
        graph_calls = len(rig.executor.calls)
        async with factory() as session, session.begin():
            await session.execute(delete(ProjectMember).where(ProjectMember.project_id == project.id))
        second = await rig.get(LIST_FEATURES, project_id=str(project.id))
        listed = await rig.get(LIST_PROJECTS)
    assert first.status_code == 200 and graph_calls > 0
    assert second.status_code == 404 and second.json()["error"]["code"] == "project_not_found"
    assert len(rig.executor.calls) == graph_calls
    assert listed.json() == {"items": [], "next_cursor": None}


@requires_live("POSTGRES")
async def test_a_new_subject_has_one_user_row_even_when_every_call_is_denied_and_the_header_names_it(
    monkeypatch, env, factory
):
    """FR-MCP-ACC-05: two denials and two ``list_projects`` calls leave exactly one ``users`` row, committed before the
    denial; every answer names that row in ``X-SpecFlow-User-Id``."""

    async with serve_agent(monkeypatch, env, session_factory=factory) as rig:
        denied = [await rig.get(LIST_FEATURES, project_id=str(uuid.uuid4())) for _ in range(2)]
        assert len(await users(factory)) == 1  # kept although both calls answered project_not_found
        listed = [await rig.get(LIST_PROJECTS) for _ in range(2)]
    (row,) = await users(factory)
    assert row.auth_subject == "user:alice@example.com"
    assert [r.status_code for r in denied] == [404, 404] and [r.json()["items"] for r in listed] == [[], []]
    assert {r.headers[USER_HEADER] for r in (*denied, *listed)} == {str(row.id)}


@requires_live("POSTGRES")
async def test_list_projects_pages_real_rows_newest_first_and_only_the_callers(monkeypatch, env, factory):
    """FR-MCP-TOOL-01 / ACC-01: three of alice's projects in two pages, bob's never listed; a cursor built from bob's
    project is only a position; an out-of-range instant is ``invalid_cursor`` (400), not a database error (500)."""

    async with factory() as session, session.begin():
        alice = await repositories.upsert_user(session, Principal("alice@example.com"))
        bob = await repositories.upsert_user(session, Principal("bob@example.com"))
        theirs = await repositories.create_project(session, bob.id, "bobs")
    for name in (
        "one",
        "two",
        "three",
    ):  # one transaction each: ``created_at`` is the transaction's time
        async with factory() as session, session.begin():
            await repositories.create_project(session, alice.id, name)
    async with serve_agent(monkeypatch, env, session_factory=factory) as rig:
        first = await rig.get(LIST_PROJECTS, limit=2)
        second = await rig.get(LIST_PROJECTS, limit=2, cursor=first.json()["next_cursor"])
        extreme = await rig.get(LIST_PROJECTS, cursor=EXTREME_PROJECT_CURSOR)
        tampered = await rig.get(
            LIST_PROJECTS,
            cursor=repositories.encode_cursor(theirs.created_at, theirs.id),
        )
    names = [i["name"] for i in first.json()["items"] + second.json()["items"]]
    assert names == ["three", "two", "one"] and second.json()["next_cursor"] is None
    assert str(theirs.id) not in first.text + second.text
    assert tampered.status_code == 200 and str(theirs.id) not in tampered.text
    assert {i["name"] for i in tampered.json()["items"]} <= {"one", "two", "three"}
    assert (extreme.status_code, extreme.json()["error"]["code"]) == (
        400,
        "invalid_cursor",
    )
    assert extreme.headers[USER_HEADER] == str(alice.id)


# --- a Postgres that cannot be reached during provisioning (the real driver, no server) -----------------------------


@pytest.mark.parametrize("tool", ["list_projects", "list_features", "get_task_brief"])
async def test_a_closed_postgres_port_during_provisioning_is_503_database_unavailable_without_a_header(
    monkeypatch, env, tool
):
    """Plan 04 Done: the real asyncpg connection error (nothing listens on the port) maps to 503
    ``database_unavailable`` on an agent route, never the generic 500; the user is not provisioned, so no header.

    Deterministic in ``make check``: it connects to the loopback interface only, where a closed port 1 (a privileged
    port nothing in a test or CI environment listens on) is refused at once with ECONNREFUSED; no DNS, no timeout, no
    traffic leaves the host."""

    closed = load_settings("api", env).database.model_copy(update={"port": 1})
    engine = build_engine(closed)
    try:
        async with serve_agent(monkeypatch, env, session_factory=build_session_factory(engine)) as rig:
            path = AGENT_ROUTES[tool]
            response = await rig.get(path, project_id=str(uuid.uuid4()), task_id="TASK::X")
    finally:
        await engine.dispose()
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "database_unavailable"
    assert USER_HEADER not in response.headers


# --- Graph store: FR-MCP-TOOL-02, -03, -05, -06 through the route ---------------------------------------------------


@requires_live_graph()
async def test_only_feature_nodes_are_listed_with_their_stored_attributes(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], graph_project: GraphProject
) -> None:
    """FR-MCP-TOOL-02 / TOOL-05: features, a module and requirements in one graph: only ``FEAT::`` ids come back, items
    hold ``spec_id``, ``name`` and the stored attributes, no embedding or other property."""

    async with serve_agent(monkeypatch, env, reader=graph_project.reader(), ontology=graph_project.ontology) as rig:
        project = await owned_project(rig.repo, "alice@example.com", "Live")
        pid = str(project.id)
        graph_project.project_ids.append(pid)
        await create(
            graph_project,
            pid,
            FEATURE,
            [
                {
                    "key": "FEAT::B",
                    "name": "Beta",
                    "stmt": "Does B.",
                    "quote": "B shall.",
                    "name_embedding": [0.5] * live_embedding_dim(),
                },
                {"key": "FEAT::A", "name": "Alpha", "mystery": "UNKNOWN-PROP"},
            ],
        )
        await create(graph_project, pid, "Module", [{"key": "MOD::M"}])
        await create(
            graph_project,
            pid,
            "FunctionalRequirement",
            [{"key": "FR-001"}, {"key": "FR-002"}],
        )
        response = await rig.get(LIST_FEATURES, project_id=pid)
    assert response.status_code == 200, response.text
    assert feature_ids(response) == ["FEAT::A", "FEAT::B"]
    assert response.json()["total_count"] == 2 and response.json()["project_id"] == pid
    alpha, beta = response.json()["items"]
    assert alpha == {"spec_id": "FEAT::A", "name": "Alpha", "attributes": {}}
    assert beta == {
        "spec_id": "FEAT::B",
        "name": "Beta",
        "attributes": {"stmt": "Does B.", "quote": "B shall."},
    }
    assert "UNKNOWN-PROP" not in response.text and "0.5" not in response.text


@requires_live_graph()
async def test_real_counts_and_pages_in_spec_id_order_with_the_canonical_cursor(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], graph_project: GraphProject
) -> None:
    """FR-MCP-TOOL-03 / TOOL-04: seven features, ``limit=5``: five then two, ``total_count`` 7 on both, ``spec_id``
    order (``FEAT::F10`` before ``FEAT::F9``), the last ``next_cursor`` null; limit 1 walks all; padded cursor: 400."""

    keys = [
        "FEAT::F1",
        "FEAT::F2",
        "FEAT::F3",
        "FEAT::F9",
        "FEAT::F10",
        "FEAT::F11",
        "FEAT::F4",
    ]
    async with serve_agent(monkeypatch, env, reader=graph_project.reader(), ontology=graph_project.ontology) as rig:
        pid = str((await owned_project(rig.repo, "alice@example.com", "Live")).id)
        graph_project.project_ids.append(pid)
        await create(graph_project, pid, FEATURE, [{"key": key} for key in keys])
        first = await rig.get(LIST_FEATURES, project_id=pid, limit=5)
        second = await rig.get(LIST_FEATURES, project_id=pid, limit=5, cursor=first.json()["next_cursor"])
        walked: list[str] = []
        cursor = None
        while True:
            page = await rig.get(
                LIST_FEATURES,
                project_id=pid,
                limit=1,
                **({"cursor": cursor} if cursor else {}),
            )
            walked += feature_ids(page)
            cursor = page.json()["next_cursor"]
            if cursor is None:
                break
        padded = await rig.get(LIST_FEATURES, project_id=pid, cursor=first.json()["next_cursor"] + "=")
    assert feature_ids(first) + feature_ids(second) == sorted(keys)
    assert (len(feature_ids(first)), len(feature_ids(second))) == (5, 2)
    assert (first.json()["total_count"], second.json()["total_count"]) == (7, 7)
    assert first.json()["next_cursor"] and second.json()["next_cursor"] is None
    assert walked == sorted(keys)
    assert padded.status_code == 400 and padded.json()["error"]["code"] == "invalid_cursor"


@requires_live_graph()
async def test_a_project_without_a_graph_is_empty_and_another_projects_nodes_stay_out(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], graph_project: GraphProject
) -> None:
    """FR-MCP-TOOL-03 / ACC-02: nodes of a second project id are not listed for alice's project; hers has none."""

    async with serve_agent(monkeypatch, env, reader=graph_project.reader(), ontology=graph_project.ontology) as rig:
        pid = str((await owned_project(rig.repo, "alice@example.com", "Live")).id)
        graph_project.project_ids.append(pid)
        other = graph_project.new_project()
        await create(graph_project, other, FEATURE, [{"key": "FEAT::OTHER"}])
        response = await rig.get(LIST_FEATURES, project_id=pid)
    body = response.json()
    assert (body["total_count"], body["items"], body["next_cursor"]) == (0, [], None)


@requires_live_graph()
async def test_the_renamed_feature_ontology_lists_only_its_own_label(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    graph_project: GraphProject,
    tmp_path: Path,
) -> None:
    """FR-MCP-TOOL-06: under the derived ontology (``Funktion``, ``FUNK::``, type key ``feature``) the route returns the
    ``Funktion`` nodes only; ``Feature`` nodes of the shipped label are not features here."""

    renamed = load_feature_renamed_ontology(tmp_path)
    async with serve_agent(
        monkeypatch,
        env,
        reader=graph_project.reader(ontology=renamed),
        ontology=renamed,
    ) as rig:
        pid = str((await owned_project(rig.repo, "alice@example.com", "Live")).id)
        graph_project.project_ids.append(pid)
        await create(
            graph_project,
            pid,
            RENAMED_FEATURE_LABEL,
            [{"key": f"{RENAMED_FEATURE_PREFIX}{c}"} for c in "ABC"],
        )
        await create(graph_project, pid, FEATURE, [{"key": "FEAT::X"}, {"key": "FEAT::Y"}])
        first = await rig.get(LIST_FEATURES, project_id=pid, limit=2)
        second = await rig.get(LIST_FEATURES, project_id=pid, limit=2, cursor=first.json()["next_cursor"])
    assert feature_ids(first) + feature_ids(second) == ["FUNK::A", "FUNK::B", "FUNK::C"]
    assert first.json()["total_count"] == 3 and second.json()["next_cursor"] is None
