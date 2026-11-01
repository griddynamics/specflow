"""Live tests of the PostgreSQL graph store against a server with pgvector.

Gate: ``SPECFLOW_TEST_GRAPH_STORE=postgres`` with ``GRAPH_STORE_URI`` (``postgresql://host:port/database``),
``GRAPH_STORE_USER``, ``GRAPH_STORE_PASSWORD`` and ``GRAPH_STORE_NAME`` (default ``graph``). The login owns the
database and the ``vector`` extension is installed in it. The suite uses vector dimension ``DIM`` and so never shares
the configured schema with the other live suites: it works in a schema of its own, ``<GRAPH_STORE_NAME>_int_<hex>``,
created by the first test that needs it and dropped when the module ends. Every test works in its own
``pytest-pg-<hex>`` project; the fixture deletes it afterwards. Schema tests use a schema of their own.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from graph.build import queries as bq
from graph.core import ConfigurationError, ConformanceError, NodeRef, Ontology, OwnedWriter
from graph.core import queries as q
from graph.core.errors import GraphWriteError, InfrastructureUnavailableError
from graph.core.queries import NodeShape, Query
from graph.stores import StoreConfig, build_store
from graph.stores.postgres import PostgresStore
from graph.stores.postgres.dialect import render_schema, schema_identifier
from graph_support import shipped_ontology

pytestmark = pytest.mark.skipif(
    os.environ.get("SPECFLOW_TEST_GRAPH_STORE", "").strip().lower() != "postgres",
    reason="set SPECFLOW_TEST_GRAPH_STORE=postgres (and GRAPH_STORE_URI, _USER, _PASSWORD, _NAME) to run these tests",
)

DIM = 8
LOCK_WAIT_POLLS = 200
LOCK_WAIT_POLL_SECONDS = 0.05
MODULE, FR, FEATURE = "Module", "FunctionalRequirement", "Feature"
PART_OF, INTEGRATES, RELATES = "PART_OF", "INTEGRATES_WITH", "RELATES_TO"
RETIRED, INFERRED = "retired", "inferred"


MAX_SCHEMA_LENGTH = 63  # PostgreSQL truncates longer identifiers
_BASE_SCHEMA = os.environ.get("GRAPH_STORE_NAME") or "graph"
INT_SCHEMA = f"{_BASE_SCHEMA[: MAX_SCHEMA_LENGTH - 13]}_int_{uuid.uuid4().hex[:8]}"


def live_config(name: str | None = None) -> StoreConfig:
    """Config of the suite's own schema; ``name`` picks another one."""

    return StoreConfig(
        backend="postgres",
        uri=os.environ["GRAPH_STORE_URI"],
        user=os.environ["GRAPH_STORE_USER"],
        password=SecretStr(os.environ["GRAPH_STORE_PASSWORD"]),
        name=name or INT_SCHEMA,
    )


def postgres_store(config: StoreConfig) -> PostgresStore:
    built = build_store(config)
    assert isinstance(built, PostgresStore)
    return built


@pytest.fixture(scope="module")
def ontology() -> Ontology:
    return shipped_ontology()


@pytest.fixture(scope="module", autouse=True)
def drop_integration_schema() -> Iterator[None]:
    """Drop the schema this module created (and only that one) once its tests are done."""

    yield

    async def drop() -> None:
        dropper = postgres_store(live_config())
        try:
            async with dropper._engine.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA IF EXISTS {schema_identifier(INT_SCHEMA)} CASCADE"))
        finally:
            await dropper.close()

    asyncio.run(drop())


@pytest.fixture
async def store(ontology: Ontology) -> AsyncIterator[PostgresStore]:
    built = build_store(live_config())
    assert isinstance(built, PostgresStore)
    await built.ensure_schema(ontology, DIM)
    yield built
    await built.close()


@pytest.fixture
async def project(store: PostgresStore) -> AsyncIterator[str]:
    project_id = f"pytest-pg-{uuid.uuid4().hex[:10]}"
    yield project_id
    async with store._engine.begin() as connection:
        await connection.execute(
            text(render_schema("DELETE FROM graph.node WHERE group_id LIKE :p", store.schema)), {"p": f"{project_id}%"}
        )


def vec(*head: float) -> list[float]:
    values = [0.0] * DIM
    for index, value in enumerate(head):
        values[index] = value
    return values


def writer(store: PostgresStore, ontology: Ontology, project_id: str, owner: str = "build") -> OwnedWriter:
    return OwnedWriter(store, ontology, owner, project_id)


async def raw(store: PostgresStore, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with store._engine.begin() as connection:
        result = await connection.execute(text(sql.replace("graph.", f'"{store.schema}".')), params)
        return [dict(row) for row in result.mappings().all()] if result.returns_rows else []


async def seed(store: PostgresStore, ontology: Ontology, project_id: str) -> OwnedWriter:
    """Modules checkout and stripe, requirements pay and assume, a document with one fragment."""

    build = writer(store, ontology, project_id)
    async with build.transaction() as tx:
        await tx.upsert_node(
            MODULE,
            "u-mod",
            {
                "spec_id": "MOD::CHECKOUT",
                "name": "Checkout",
                "external": "no",
                "spec_id_aliases": ["MOD::OLD"],
                "embedding": vec(1.0),
                "embedding_model": "m",
            },
        )
        await tx.upsert_node(MODULE, "u-stripe", {"spec_id": "MOD::STRIPE", "name": "Stripe", "external": "yes"})
        await tx.upsert_node(
            FR,
            "u-fr",
            {
                "spec_id": "REQ::PAY",
                "name": "Users can pay by card",
                "is_assumption": "no",
                "embedding": vec(0.0, 1.0),
                "embedding_model": "m",
                "statements": json.dumps([{"fragment_id": "f1", "text": "pay"}]),
            },
        )
        await tx.upsert_node(FR, "u-fr2", {"spec_id": "REQ::MAYBE", "name": "Maybe SSO", "is_assumption": "yes"})
        await tx.upsert_node("Document", "d1", {"path": "checkout.md", "status": "active"})
        await tx.upsert_node(
            "Fragment",
            "f1",
            {
                "document_id": "d1",
                "text": "Users can pay by card",
                "status": "active",
                "ordinal": 0,
                "line_start": 3,
                "line_end": 4,
            },
        )
        await tx.upsert_relationship(
            PART_OF, NodeRef(FR, "u-fr"), NodeRef(MODULE, "u-mod"), "k-part", {"basis": "stated", "fact": "pay"}
        )
        await tx.upsert_relationship(
            INTEGRATES,
            NodeRef(MODULE, "u-mod"),
            NodeRef(MODULE, "u-stripe"),
            "k-int",
            {"basis": INFERRED, "inference_rule": "INF-INT-01", "confidence": 0.9},
        )
    await raw(
        store,
        "WITH d AS (SELECT id FROM graph.node WHERE group_id = :p AND kind = 'document' AND document_id = 'd1'), "
        "f AS (SELECT id FROM graph.node WHERE group_id = :p AND kind = 'fragment' AND fragment_id = 'f1'), "
        "n AS (SELECT id FROM graph.node WHERE group_id = :p AND kind = 'spec' AND uuid = 'u-fr') "
        "INSERT INTO graph.edge (group_id, rel_type, source_id, target_id, props) "
        "SELECT :p, 'CONTAINS', d.id, f.id, CAST('{}' AS jsonb) FROM d, f UNION ALL "
        "SELECT :p, 'REFERENCED_BY', n.id, f.id, CAST('{\"line_start\": 3, \"line_end\": 4}' AS jsonb) FROM n, f",
        p=project_id,
    )
    return build


# Store, schema and error mapping


async def test_the_store_is_a_postgres_store_with_transactions(store: PostgresStore) -> None:
    assert store.info.backend == "postgres"
    assert store.info.capabilities.multi_statement_tx is True
    assert store.info.server_version
    await store.check()


async def test_ensure_schema_is_idempotent_and_reports_what_it_created(ontology: Ontology) -> None:
    schema = f"pytest_pg_{uuid.uuid4().hex[:8]}"
    first = postgres_store(live_config(schema))
    try:
        created = await first.ensure_schema(ontology, DIM)
        assert {"node", "edge", "meta", "node_spec_uuid_uq", "edge_key_uq", "fts_norm"} <= set(created)
        assert await first.ensure_schema(ontology, DIM) == ()
        with pytest.raises(ConfigurationError) as raised:
            await first.ensure_schema(ontology, DIM // 2)
        assert raised.value.code == "vector_index_mismatch"
        assert raised.value.fields["found_dimension"] == DIM
    finally:
        async with first._engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await first.close()


async def test_concurrent_schema_setup_does_not_collide(ontology: Ontology) -> None:
    schema = f"pytest_pg_{uuid.uuid4().hex[:8]}"
    stores = [postgres_store(live_config(schema)) for _ in range(8)]
    try:
        results = await asyncio.gather(*(item.ensure_schema(ontology, DIM) for item in stores), return_exceptions=True)
        assert [item for item in results if isinstance(item, BaseException)] == []
        assert sum(1 for item in results if item) == 1  # one session created it, the others found it
    finally:
        async with stores[0]._engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        for item in stores:
            await item.close()


async def test_a_wrong_password_is_a_rejected_login() -> None:
    config = live_config().model_copy(update={"password": SecretStr("wrong-password-for-test")})
    store = build_store(config)
    try:
        with pytest.raises(ConfigurationError) as raised:
            await store.check()
        assert raised.value.code == "store_auth_rejected"
        assert raised.value.fields["store"] == "postgres"
    finally:
        await store.close()


async def test_an_unknown_database_is_reported(ontology: Ontology) -> None:
    config = live_config()
    other = config.model_copy(
        update={"uri": re.sub(r"/[^/?]*(\?|$)", r"/no_such_database_pytest\1", config.uri, count=1)}
    )
    store = build_store(other)
    try:
        with pytest.raises(ConfigurationError) as raised:
            await store.check()
        assert raised.value.code == "store_database_not_found"
    finally:
        await store.close()


async def test_an_unreachable_server_is_unavailable() -> None:
    config = live_config().model_copy(update={"uri": "postgresql://127.0.0.1:1/specflow"})
    store = build_store(config)
    try:
        with pytest.raises(InfrastructureUnavailableError) as raised:
            await store.check()
        assert raised.value.code == "store_unreachable"
    finally:
        await store.close()


# Owned writes


async def test_a_spec_node_upsert_creates_merges_and_refuses(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    await build.upsert_node(MODULE, "u1", {"spec_id": "MOD::A", "name": "A", "extra": 1})
    await build.upsert_node(MODULE, "u1", {"name": "A2", "extra": None, "list": ["x", "y"]})
    rows = await raw(
        store, "SELECT labels, props, spec_id FROM graph.node WHERE group_id = :p AND uuid = 'u1'", p=project
    )
    assert rows[0]["labels"] == [MODULE]
    assert rows[0]["props"] == {"uuid": "u1", "spec_id": "MOD::A", "name": "A2", "list": ["x", "y"]}
    with pytest.raises(ConformanceError) as mismatch:
        await build.upsert_node(FR, "u1", {"name": "x"})
    assert mismatch.value.code == "node_label_mismatch"
    with pytest.raises(ConformanceError) as changed:
        await build.upsert_node(MODULE, "u1", {"spec_id": "MOD::B"})
    assert changed.value.code == "spec_id_immutable"
    await build.upsert_node(MODULE, "u1", {"spec_id": "MOD::A"})
    with pytest.raises(GraphWriteError):
        await build.upsert_node(MODULE, "u2", {"spec_id": "MOD::A", "name": "dup"})
    assert (await raw(store, "SELECT count(*) AS n FROM graph.node WHERE group_id = :p AND kind = 'spec'", p=project))[
        0
    ]["n"] == 1


async def test_the_embedding_is_a_vector_column_and_null_clears_it(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    await build.upsert_node(MODULE, "u1", {"spec_id": "MOD::A", "embedding": vec(1.0, 2.0), "embedding_model": "m"})
    rows = await raw(
        store,
        "SELECT embedding::text AS e, props ? 'embedding' AS inprops FROM graph.node WHERE group_id = :p",
        p=project,
    )
    assert rows[0]["e"].startswith("[1,2,0") and rows[0]["inprops"] is False
    await build.upsert_node(MODULE, "u1", {"embedding": None})
    rows = await raw(store, "SELECT embedding IS NULL AS gone FROM graph.node WHERE group_id = :p", p=project)
    assert rows[0]["gone"] is True


async def test_documents_fragments_and_foreign_nodes_upsert_by_their_key(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    executor = store.executor()
    for _ in range(2):
        rows = await executor.write(q.UPSERT_DOCUMENT, project, key="d1", props={"path": "a.md", "title": None})
        assert rows == [{"written": 1}]
    await executor.write(q.UPSERT_FRAGMENT, project, key="f1", props={"document_id": "d1", "text": "t"})
    await executor.write(q.upsert_foreign_node("Finding"), project, key="g1", props={"title": "x"})
    await executor.write(q.upsert_foreign_node("Finding"), project, key="g1", props={"title": "y"})
    counts = await raw(
        store, "SELECT kind, count(*) AS n FROM graph.node WHERE group_id = :p GROUP BY kind ORDER BY kind", p=project
    )
    assert counts == [{"kind": "document", "n": 1}, {"kind": "foreign", "n": 1}, {"kind": "fragment", "n": 1}]
    docs = await executor.read(q.READ_DOCUMENTS, project)
    assert docs == [{"props": {"document_id": "d1", "path": "a.md", "group_id": project}}]


async def test_a_relationship_is_written_once_conflicts_are_flagged_and_deletes_report(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    await seed(store, ontology, project)
    build = writer(store, ontology, project)
    mod, stripe, fr = NodeRef(MODULE, "u-mod"), NodeRef(MODULE, "u-stripe"), NodeRef(FR, "u-fr")
    await build.upsert_relationship(INTEGRATES, mod, stripe, "k-int", {"confidence": 0.5})
    rows = await raw(
        store, "SELECT props FROM graph.edge WHERE group_id = :p AND rel_type = 'INTEGRATES_WITH'", p=project
    )
    assert len(rows) == 1 and rows[0]["props"]["confidence"] == 0.5 and rows[0]["props"]["basis"] == INFERRED
    with pytest.raises(ConformanceError) as conflict:
        await build.upsert_relationship(INTEGRATES, stripe, mod, "k-int", {})
    assert conflict.value.code == "edge_key_endpoint_mismatch"
    with pytest.raises(ConformanceError) as missing:
        await build.upsert_relationship(INTEGRATES, mod, NodeRef(MODULE, "absent"), "k-x", {})
    assert missing.value.code == "relationship_end_missing"
    assert await build.delete_relationship(INTEGRATES, "k-int") is True
    assert await build.delete_relationship(INTEGRATES, "k-int") is False
    assert fr.key == "u-fr"


async def test_delete_node_removes_its_edges_and_a_foreign_owner_is_refused(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    await seed(store, ontology, project)
    build = writer(store, ontology, project)
    assert await build.delete_node(FR, "u-fr") is True
    assert await build.delete_node(FR, "u-fr") is False
    left = await raw(store, "SELECT count(*) AS n FROM graph.edge WHERE group_id = :p", p=project)
    assert left[0]["n"] == 2  # INTEGRATES_WITH and CONTAINS are left (PART_OF and REFERENCED_BY went with the node)
    assert await build.delete_node("Document", "d1") is True


async def write_then_fail(build: OwnedWriter) -> None:
    async with build.transaction() as tx:
        await tx.upsert_node(MODULE, "u1", {"spec_id": "MOD::A"})
        raise RuntimeError("stop")


async def test_an_explicit_transaction_rolls_back_when_it_raises(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    with pytest.raises(RuntimeError):
        await write_then_fail(build)
    assert (await raw(store, "SELECT count(*) AS n FROM graph.node WHERE group_id = :p", p=project))[0]["n"] == 0
    async with build.transaction() as tx:
        await tx.upsert_node(MODULE, "u1", {"spec_id": "MOD::A"})
        await tx.upsert_node(MODULE, "u2", {"spec_id": "MOD::B"})
    assert (await raw(store, "SELECT count(*) AS n FROM graph.node WHERE group_id = :p", p=project))[0]["n"] == 2


async def test_move_foreign_relationships_keeps_type_direction_and_self_loops(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    nodes = {key: NodeRef(FR, key) for key in ("old", "new", "other")}
    for key, ref in nodes.items():
        await build.upsert_node(FR, ref.key, {"spec_id": f"REQ::{key}", "name": key})
    await build.upsert_relationship(RELATES, nodes["old"], nodes["other"], "k1", {"basis": "stated"})
    await build.upsert_relationship(RELATES, nodes["other"], nodes["old"], "k2", {"basis": "stated"})
    await build.upsert_relationship(RELATES, nodes["old"], nodes["old"], "k3", {"basis": "stated"})
    await build.upsert_relationship(RELATES, nodes["old"], nodes["new"], "k4", {"basis": "stated"})
    # the writer owns RELATES_TO for build; "owned" excludes only built-in and build relations, so none of them move
    assert await build.move_foreign_relationships(nodes["old"], nodes["new"]) == 0
    foreign = writer(store, ontology, project, "build")
    executor = store.executor()
    async with executor.tx(project) as tx:
        rows = await tx.run(
            q.MOVE_FOREIGN_RELATIONSHIPS,
            from_key="old",
            from_label=FR,
            to_key="new",
            to_label=FR,
            owned_types=[],
        )
    assert rows == [{"moved": 4}]
    edges = await raw(
        store,
        "SELECT s.uuid AS s, t.uuid AS t, e.edge_key AS k FROM graph.edge e JOIN graph.node s ON s.id = e.source_id "
        "JOIN graph.node t ON t.id = e.target_id WHERE e.group_id = :p ORDER BY e.edge_key",
        p=project,
    )
    assert edges == [
        {"s": "new", "t": "other", "k": "k1"},
        {"s": "other", "t": "new", "k": "k2"},
        {"s": "new", "t": "new", "k": "k3"},  # a self-loop stays a self-loop (canonical Cypher, W3)
    ]  # k4 joined old and new: only deleted
    assert foreign.owner == "build"


# Reads


async def test_reads_return_the_row_contract_of_the_cypher_queries(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    await seed(store, ontology, project)
    executor = store.executor()
    labels = [MODULE, FR, FEATURE]
    rows = await executor.read(q.READ_NODE_BY_SPEC_ID, project, spec_id="REQ::PAY", retired=RETIRED)
    assert len(rows) == 1
    row = rows[0]
    assert row["labels"] == ["SpecNode", FR]
    assert row["props"]["embedding"] is None and row["props"]["name_embedding"] is None
    assert row["props"]["group_id"] == project and row["props"]["name"] == "Users can pay by card"
    assert row["provenance"] == [
        {"fragment_id": "f1", "document_id": "d1", "path": "checkout.md", "line_start": 3, "line_end": 4}
    ]
    assert (await executor.read(q.READ_NODE_BY_ALIAS, project, spec_id="MOD::OLD", retired=RETIRED))[0]["props"][
        "spec_id"
    ] == "MOD::CHECKOUT"
    nodes = await executor.read(q.READ_NODES, project, labels=labels, where={"external": "no"}, retired=RETIRED)
    assert [item["props"]["spec_id"] for item in nodes] == ["MOD::CHECKOUT"]
    by_uuid = await executor.read(q.READ_NODES_BY_UUID, project, uuids=["u-stripe", "u-fr"], retired=RETIRED)
    assert sorted(item["props"]["spec_id"] for item in by_uuid) == ["MOD::STRIPE", "REQ::PAY"]
    lines = await executor.read(
        q.READ_NODES_FOR_LINES, project, document_id="d1", line_start=4, line_end=9, labels=labels, retired=RETIRED
    )
    assert [item["props"]["spec_id"] for item in lines] == ["REQ::PAY"]
    none = await executor.read(
        q.READ_NODES_FOR_LINES, project, document_id="d1", line_start=5, line_end=9, labels=labels, retired=RETIRED
    )
    assert none == []
    ids = await executor.read(
        q.READ_NODES_BY_IDS, project, labels=labels, where={}, keys=[], spec_ids=["MOD::OLD", "REQ::MAYBE"]
    )
    assert [(item["spec_id"], item["spec_ids"]) for item in ids] == [
        ("MOD::CHECKOUT", ["MOD::CHECKOUT", "MOD::OLD"]),
        ("REQ::MAYBE", ["REQ::MAYBE"]),
    ]
    uuids = await executor.read(q.READ_NODE_UUIDS_BY_SPEC_ID, project, labels=labels, spec_ids=["MOD::OLD"])
    assert uuids == [{"uuid": "u-mod", "spec_id": "MOD::CHECKOUT"}]
    frags = await executor.read(q.READ_FRAGMENTS, project, document_id=None)
    assert frags[0]["props"]["fragment_id"] == "f1"
    assert (await executor.read(q.READ_FRAGMENTS, project, document_id="zzz")) == []
    hits = await executor.read(q.READ_FRAGMENT_HITS, project, fragment_ids=["f1", "nope"])
    assert hits[0]["document"]["document_id"] == "d1" and hits[0]["props"]["text"].startswith("Users")


async def test_the_where_filter_treats_a_missing_property_as_not_equal(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    """Three-valued logic: a node without the property must not pass an equality, and must pass a null."""

    await seed(store, ontology, project)
    executor = store.executor()
    labels = [MODULE, FR]
    kwargs: dict[str, Any] = {"labels": labels, "retired": RETIRED}
    eq = await executor.read(q.READ_NODES, project, where={"is_assumption": "yes"}, **kwargs)
    assert [item["props"]["spec_id"] for item in eq] == ["REQ::MAYBE"]
    absent = await executor.read(q.READ_NODES, project, where={"is_assumption": None}, **kwargs)
    assert [item["props"]["spec_id"] for item in absent] == ["MOD::CHECKOUT", "MOD::STRIPE"]
    both = await executor.read(q.READ_NODES, project, where={"is_assumption": "no", "external": "no"}, **kwargs)
    assert both == []
    counts = await executor.read(
        q.COUNT_SEARCH_VECTORS, project, model="m", labels=labels, where={"is_assumption": "yes"}
    )
    assert counts == [{"matching": 0, "stored": 0}]


async def test_edges_neighbours_and_impact(store: PostgresStore, ontology: Ontology, project: str) -> None:
    await seed(store, ontology, project)
    executor = store.executor()
    labels = [MODULE, FR]
    relations = [PART_OF, INTEGRATES]
    edges = await executor.read(
        q.READ_EDGES, project, relations=relations, labels=labels, include_inferred=True, inferred=INFERRED
    )
    assert [(e["relation"], e["source"], e["target"]) for e in edges] == [
        (INTEGRATES, "MOD::CHECKOUT", "MOD::STRIPE"),
        (PART_OF, "REQ::PAY", "MOD::CHECKOUT"),
    ]
    assert edges[0]["props"]["group_id"] == project and edges[0]["props"]["edge_key"] == "k-int"
    stated = await executor.read(
        q.READ_EDGES, project, relations=relations, labels=labels, include_inferred=False, inferred=INFERRED
    )
    assert [e["relation"] for e in stated] == [PART_OF]
    around = await executor.read(
        q.READ_NEIGHBOURS, project, spec_id="MOD::CHECKOUT", relations=relations, labels=labels
    )
    assert [(e["relation"], e["source"], e["target"]) for e in around] == [
        (INTEGRATES, "MOD::CHECKOUT", "MOD::STRIPE"),
        (PART_OF, "REQ::PAY", "MOD::CHECKOUT"),
    ]
    hop = await executor.read(
        q.READ_IMPACT_HOP, project, frontier=["u-mod"], seen=["u-mod"], relations=relations, labels=labels, limit=10
    )
    assert [item["spec_id"] for item in hop] == ["MOD::STRIPE", "REQ::PAY"]
    limited = await executor.read(
        q.READ_IMPACT_HOP,
        project,
        frontier=["u-mod"],
        seen=["u-mod", "u-stripe"],
        relations=relations,
        labels=labels,
        limit=1,
    )
    assert [item["spec_id"] for item in limited] == ["REQ::PAY"]


async def test_scope_walks_follow_direction_symmetry_and_the_root_barrier(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    for key in ("m", "m2", "f1", "f2", "r1"):
        label = MODULE if key.startswith("m") else FR if key.startswith("r") else FEATURE
        await build.upsert_node(label, f"u-{key}", {"spec_id": f"S::{key}", "name": key})
    await build.upsert_relationship(PART_OF, NodeRef(FEATURE, "u-f1"), NodeRef(MODULE, "u-m"), "k1", {})
    await build.upsert_relationship(PART_OF, NodeRef(FEATURE, "u-f2"), NodeRef(FEATURE, "u-f1"), "k2", {})
    await build.upsert_relationship(PART_OF, NodeRef(FR, "u-r1"), NodeRef(FEATURE, "u-f2"), "k3", {})
    await build.upsert_relationship(PART_OF, NodeRef(MODULE, "u-m"), NodeRef(MODULE, "u-m2"), "k4", {})
    await build.upsert_relationship(RELATES, NodeRef(MODULE, "u-m2"), NodeRef(FR, "u-r1"), "k5", {"basis": "stated"})
    executor = store.executor()
    params: dict[str, Any] = {
        "spec_id": "S::m",
        "labels": [MODULE, FR, FEATURE],
        "roots": [MODULE],
        "symmetric": [RELATES],
        "max_hops": 3,
    }
    via = await executor.read(q.scope_via((PART_OF,), 1, 3), project, **params)
    assert sorted(row["uuid"] for row in via) == ["u-f1", "u-f2", "u-r1"]
    capped = await executor.read(q.scope_via((PART_OF,), 1, 3), project, **{**params, "max_hops": 2})
    assert sorted(row["uuid"] for row in capped) == ["u-f1", "u-f2"]
    barrier = await executor.read(q.scope_via((PART_OF,), 1, 3), project, **{**params, "spec_id": "S::m2"})
    assert [row["uuid"] for row in barrier] == ["u-m"]  # m2 <- m, and m is a root so nothing beyond it
    either = await executor.read(q.scope_via((RELATES,), 1, 1), project, **{**params, "spec_id": "S::r1"})
    assert [row["uuid"] for row in either] == ["u-m2"]
    directed = await executor.read(q.scope_via((RELATES,), 1, 1), project, **{**params, "symmetric": []})
    assert directed == []
    sequence = await executor.read(q.scope_sequence((PART_OF, PART_OF), ()), project, **{**params, "spec_id": "S::f1"})
    assert [row["uuid"] for row in sequence] == ["u-r1"]
    either_way = await executor.read(
        q.scope_sequence((RELATES, PART_OF), (RELATES,)), project, **{**params, "spec_id": "S::f2"}
    )
    assert [row["uuid"] for row in either_way] == ["u-m2"]  # m2 -RELATES- r1 -PART_OF-> f2
    wrong_order = await executor.read(
        q.scope_sequence((PART_OF, RELATES), (RELATES,)), project, **{**params, "spec_id": "S::f2"}
    )
    assert wrong_order == []


# Search


def bm25_reference(documents: dict[str, str], query: str, k1: float = 1.2, b: float = 0.75) -> dict[str, float]:
    """Lucene-style BM25 over a field, tokens as the adapter's normalisation splits them."""

    def tokens(value: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", value.lower().replace("-", " ").replace("/", " "))

    docs = {key: tokens(value) for key, value in documents.items() if tokens(value)}
    average = sum(len(item) for item in docs.values()) / len(docs)
    scores: dict[str, float] = {}
    for term in set(tokens(query)):
        df = sum(1 for item in docs.values() if term in item)
        if not df:
            continue
        idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
        for key, item in docs.items():
            tf = Counter(item)[term]
            if tf:
                scores[key] = scores.get(key, 0.0) + idf * tf / (tf + k1 * (1 - b + b * len(item) / average))
    return scores


async def test_fulltext_search_is_bm25_with_known_scores(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    names = {
        "a": "Pay by card at checkout",
        "b": "Refund a card payment",
        "c": "Print the order slip",
        "d": "Card card card checkout",
    }
    for key, value in names.items():
        await build.upsert_node(FR, f"u-{key}", {"spec_id": f"R::{key}", "name": value})
    executor = store.executor()
    base = {"index": "i", "labels": [FR], "where": {}, "skip": 0, "page": 10}
    rows = await executor.read(q.SEARCH_NODES_FULLTEXT, project, query="card checkout", **base)
    assert rows[0]["fetched"] == len(rows[0]["hits"]) == 3
    expected = bm25_reference({f"R::{key}": value for key, value in names.items()}, "card checkout")
    got = {hit["spec_id"]: hit["score"] for hit in rows[0]["hits"]}
    assert set(got) == set(expected)
    for key, score in expected.items():
        assert got[key] == pytest.approx(score, rel=1e-9)
    assert [hit["spec_id"] for hit in rows[0]["hits"]] == sorted(expected, key=lambda key: (-expected[key], key))
    paged = await executor.read(
        q.SEARCH_NODES_FULLTEXT, project, query="card checkout", **{**base, "skip": 1, "page": 1}
    )
    assert paged[0]["fetched"] == 1 and paged[0]["hits"][0]["spec_id"] == rows[0]["hits"][1]["spec_id"]
    filtered = await executor.read(q.SEARCH_NODES_FULLTEXT, project, query="card", **{**base, "labels": [MODULE]})
    assert filtered[0]["fetched"] == 0 and filtered[0]["hits"] == []
    isolated = await executor.read(q.SEARCH_NODES_FULLTEXT, project + "x", query="card", **base)
    assert isolated[0]["hits"] == []


async def test_fragment_search_filters_status_and_document(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    executor = store.executor()
    for key, document, status, value in (
        ("f1", "d1", "active", "Users pay by card"),
        ("f2", "d1", "retired", "Users pay by card"),
        ("f3", "d2", "active", "Pay with a gift card"),
    ):
        await executor.write(
            q.UPSERT_FRAGMENT, project, key=key, props={"document_id": document, "status": status, "text": value}
        )
    base = {"index": "i", "active": "active", "skip": 0, "page": 10}
    rows = await executor.read(q.SEARCH_FRAGMENTS, project, query="card pay", document_id=None, **base)
    assert {hit["fragment_id"] for hit in rows[0]["hits"]} == {"f1", "f3"}
    only = await executor.read(q.SEARCH_FRAGMENTS, project, query="card pay", document_id="d2", **base)
    assert [hit["fragment_id"] for hit in only[0]["hits"]] == ["f3"]


def cosine_score(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return (1 + dot / (math.hypot(*a) * math.hypot(*b))) / 2


async def test_the_vector_score_is_one_plus_cosine_over_two(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    vectors = {"a": vec(1.0, 0.0), "b": vec(1.0, 1.0), "c": vec(-1.0, 0.0), "d": vec(0.0, 0.0)}
    for key, value in vectors.items():
        await build.upsert_node(FR, f"u-{key}", {"spec_id": f"R::{key}", "embedding": value, "embedding_model": "m"})
    await build.upsert_node(FR, "u-other", {"spec_id": "R::other", "embedding": vec(1.0), "embedding_model": "other"})
    executor = store.executor()
    query = vec(1.0, 0.5)
    params = {"model": "m", "vector": query, "labels": [FR], "where": {}, "limit": 10}
    rows = await executor.read(q.SEARCH_NODES_VECTOR_EXACT, project, **params)
    assert [row["spec_id"] for row in rows] == ["R::b", "R::a", "R::c"]  # the zero vector (NaN) never appears
    for row in rows:
        assert row["score"] == pytest.approx(cosine_score(query, vectors[row["spec_id"][3:]]), abs=1e-6)
    mismatched = await executor.read(q.SEARCH_NODES_VECTOR_EXACT, project, **{**params, "vector": [1.0, 2.0]})
    assert mismatched == []
    counts = await executor.read(q.COUNT_SEARCH_VECTORS, project, model="m", labels=[FR], where={})
    assert counts == [{"matching": 4, "stored": 5}]
    page = await executor.read(q.SEARCH_NODES_VECTOR, project, index="i", k=10, **params)
    assert page[0]["fetched"] >= 3
    assert [hit["spec_id"] for hit in page[0]["hits"]] == ["R::b", "R::a", "R::c"]


async def test_a_vector_query_keeps_its_plan_after_many_executions(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    """The prepared statement must not switch to a generic plan (plan_cache_mode is custom for the page)."""

    build = writer(store, ontology, project)
    for index in range(30):
        await build.upsert_node(
            FR, f"u-{index}", {"spec_id": f"R::{index:02d}", "embedding": vec(1.0, index / 30), "embedding_model": "m"}
        )
    executor = store.executor()
    params = {"model": "m", "vector": vec(1.0, 0.5), "labels": [FR], "where": {}, "limit": 5}
    first = await executor.read(q.SEARCH_NODES_VECTOR_EXACT, project, **params)
    for _ in range(12):
        again = await executor.read(q.SEARCH_NODES_VECTOR_EXACT, project, **params)
        assert again == first
        page = await executor.read(q.SEARCH_NODES_VECTOR, project, index="i", k=20, **params)
        assert len(page[0]["hits"]) == 5
    async with store.executor().tx(project) as tx:
        await tx.run(q.SEARCH_NODES_VECTOR, index="i", k=20, **params)
        mode = await raw_in(tx, store, "SELECT current_setting('plan_cache_mode') AS m")
    assert mode == "force_custom_plan"


async def raw_in(tx: Any, store: PostgresStore, sql: str) -> Any:
    rows = await tx._connection.execute(text(sql))
    return rows.scalar_one()


async def test_a_deadlock_is_retried(store: PostgresStore, ontology: Ontology, project: str) -> None:
    """Two writers take two nodes in opposite order: one gets 40P01; its managed write is retried."""

    executor = store.executor()
    for key in ("a", "b"):
        await executor.write(q.UPSERT_DOCUMENT, project, key=key, props={"path": key})

    async def locker(first: str, second: str, mine: asyncio.Event, theirs: asyncio.Event) -> None:
        async with executor.tx(project) as tx:
            await tx.run(q.UPSERT_DOCUMENT, key=first, props={"path": first + "1"})
            mine.set()
            await theirs.wait()
            await tx.run(q.UPSERT_DOCUMENT, key=second, props={"path": second + "1"})

    one, two = asyncio.Event(), asyncio.Event()
    results = await asyncio.gather(locker("a", "b", one, two), locker("b", "a", two, one), return_exceptions=True)
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) <= 1  # an explicit transaction is not retried: at most the deadlock victim fails
    if failures:
        assert isinstance(failures[0], GraphWriteError)
        assert failures[0].fields["backend_code"] == "40P01"


def test_the_shape_helper_is_importable() -> None:
    assert NodeShape("SpecNode", "uuid", check_label=True).checked().label == "SpecNode"


async def test_a_project_over_two_thousand_vectors_gets_an_hnsw_index_that_the_search_uses(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    """ensure_schema adds a partial halfvec HNSW index for the model; the vector page keeps answering as before."""

    for stale in await raw(
        store,
        "SELECT indexname FROM pg_indexes WHERE schemaname = :s AND indexname LIKE 'node_emb_hnsw_%'",
        s=store.schema,
    ):
        await raw(store, f'DROP INDEX "{store.schema}".{stale["indexname"]}')
    await raw(
        store,
        "INSERT INTO graph.node (group_id, kind, labels, props, embedding) "
        "SELECT :p, 'spec', ARRAY['FunctionalRequirement'], "
        "jsonb_build_object('uuid', 'u' || g, 'spec_id', 'R::' || lpad(g::text, 5, '0'), 'embedding_model', 'hnsw-m'), "
        "CAST((SELECT array_agg(random()) FROM generate_series(1, 8) AS d WHERE g > 0) AS vector) "
        "FROM generate_series(1, 2100) AS g",
        p=project,
    )
    created = await store.ensure_schema(ontology, DIM)
    assert [name for name in created if name.startswith("node_emb_hnsw_")] != []
    assert await store.ensure_schema(ontology, DIM) == ()
    executor = store.executor()
    query = [0.9, 0.1, 0.5, 0.5, 0.2, 0.7, 0.3, 0.4]
    params = {"model": "hnsw-m", "vector": query, "labels": [FR], "where": {}, "limit": 5}
    exact = await executor.read(q.SEARCH_NODES_VECTOR_EXACT, project, **params)
    wanted = {row["spec_id"] for row in exact}
    for _ in range(10):  # more than the five runs after which a prepared statement switches plans
        page = await executor.read(q.SEARCH_NODES_VECTOR, project, index="i", k=60, **params)
        got = {hit["spec_id"] for hit in page[0]["hits"]}
        assert len(got & wanted) >= 4  # approximate index: at least four of the five exact neighbours
        assert page[0]["fetched"] == 60
    indexes = await raw(
        store,
        "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND indexname LIKE 'node_emb_hnsw_%'",
        s=store.schema,
    )
    assert len(indexes) == 1
    assert "hnsw" in indexes[0]["indexdef"] and "halfvec(8)" in indexes[0]["indexdef"]
    assert "embedding_model = 'hnsw-m'" in indexes[0]["indexdef"]


# Build queries


async def test_a_batch_of_creates_folds_duplicate_keys_before_it_writes(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    """ON CONFLICT cannot touch one row twice: rows of one uuid fold in order, the first label wins, a null removes."""

    executor = store.executor()
    rows = [
        {"uuid": "u1", "label": MODULE, "props": {"spec_id": "M::1", "name": "first", "gone": "x", "keep": 1}},
        {"uuid": "u1", "label": FR, "props": {"name": "second", "gone": None, "embedding": vec(1.0)}},
        {"uuid": "u2", "label": FR, "props": {"spec_id": "R::2"}},
    ]
    async with executor.tx(project) as tx:
        assert await tx.run(bq.CREATE_SPEC_NODES, rows=rows, build_id="b1") == [{"written": 3}]
    found = await raw(
        store,
        "SELECT uuid, labels, props, embedding IS NOT NULL AS has_vector "
        "FROM graph.node WHERE group_id = :p ORDER BY uuid",
        p=project,
    )
    assert found[0]["labels"] == [MODULE] and found[0]["has_vector"] is True
    assert found[0]["props"] == {"uuid": "u1", "spec_id": "M::1", "name": "second", "keep": 1}
    again = [{"uuid": "u1", "label": FR, "props": {"keep": None, "added": True}}]
    async with executor.tx(project) as tx:
        assert await tx.run(bq.CREATE_SPEC_NODES, rows=again, build_id="b2") == [{"written": 1}]
    (node,) = await raw(store, "SELECT labels, props FROM graph.node WHERE group_id = :p AND uuid = 'u1'", p=project)
    assert node["labels"] == [MODULE]  # the label is set on creation only
    assert node["props"] == {"uuid": "u1", "spec_id": "M::1", "name": "second", "added": True}


async def test_a_null_removes_a_top_level_key_only(store: PostgresStore, ontology: Ontology, project: str) -> None:
    executor = store.executor()
    async with executor.tx(project) as tx:
        await tx.run(
            bq.CREATE_SPEC_NODES, rows=[{"uuid": "u1", "label": MODULE, "props": {"spec_id": "M::1"}}], build_id="b"
        )
        patch = {"uuid": "u1", "props": {"a": 1, "b": 2, "nested": {"x": None, "y": [1, None]}}}
        await tx.run(bq.UPDATE_SPEC_NODES, rows=[patch], build_id="b")
        clear = {"uuid": "u1", "props": {"a": None}}
        assert await tx.run(bq.UPDATE_SPEC_NODES, rows=[clear], build_id="b") == [{"written": 1}]
        missing = await tx.run(bq.UPDATE_SPEC_NODES, rows=[{"uuid": "none", "props": {}}], build_id="b")
        assert missing == [{"written": 0}]
    (node,) = await raw(store, "SELECT props FROM graph.node WHERE group_id = :p AND uuid = 'u1'", p=project)
    assert node["props"] == {"uuid": "u1", "spec_id": "M::1", "b": 2, "nested": {"x": None, "y": [1, None]}}


async def test_a_merge_moves_foreign_relationships_once_and_deletes_the_merged_nodes(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    """W3: equal relationships in the moved set collapse, one between the nodes goes, a self-loop stays one."""

    build = writer(store, ontology, project)
    for key in ("old1", "old2", "new", "other"):
        await build.upsert_node(FR, key, {"spec_id": f"R::{key}", "name": key})
    same = {"basis": "stated", "fact": "same"}
    seeded = [
        {"source": "old1", "target": "other", "edge_key": "k-a", "props": same},
        {"source": "old2", "target": "other", "edge_key": "k-a", "props": same},  # equal: one of the two survives
        {"source": "other", "target": "old1", "edge_key": "k-b", "props": {"basis": "stated"}},
        {"source": "old1", "target": "old1", "edge_key": "k-self", "props": {"basis": "stated"}},
        {"source": "old1", "target": "old2", "edge_key": "k-link", "props": {"basis": "stated"}},  # deleted
        {"source": "old2", "target": "new", "edge_key": "k-new", "props": {"basis": "stated"}},  # deleted
    ]
    async with store.executor().tx(project) as tx:
        assert await tx.run(bq.upsert_edges(RELATES), rows=seeded) == [{"written": 6}]
    executor = store.executor()
    row = {"survivor": "new", "merged": ["old1", "old2", "gone"], "aliases": ["R::old1", "R::old2"]}
    async with executor.tx(project) as tx:
        assert await tx.run(bq.MERGE_SPEC_NODES, rows=[row], build_id="b9", owned_types=["PART_OF"]) == [{"written": 1}]
    nodes = await raw(store, "SELECT uuid, props FROM graph.node WHERE group_id = :p ORDER BY uuid", p=project)
    assert [item["uuid"] for item in nodes] == ["new", "other"]
    survivor = nodes[0]["props"]
    assert survivor["spec_id_aliases"] == ["R::old1", "R::old2"] and survivor["last_build_id"] == "b9"
    edges = await raw(
        store,
        "SELECT s.uuid AS s, t.uuid AS t, e.edge_key AS k FROM graph.edge e JOIN graph.node s ON s.id = e.source_id "
        "JOIN graph.node t ON t.id = e.target_id WHERE e.group_id = :p ORDER BY e.edge_key, s.uuid",
        p=project,
    )
    assert edges == [
        {"s": "new", "t": "other", "k": "k-a"},
        {"s": "other", "t": "new", "k": "k-b"},
        {"s": "new", "t": "new", "k": "k-self"},
    ]
    async with executor.tx(project) as tx:  # the batch applied again: the merged nodes are gone, the survivor stays
        again = await tx.run(bq.MERGE_SPEC_NODES, rows=[row], build_id="b10", owned_types=["PART_OF"])
    assert again == [{"written": 1}]


# Review fixes


async def _race(store: PostgresStore, project: str, first: Any, second: Any) -> None:
    """Run ``first`` in a transaction that stays open while ``second`` starts in another one: ``second`` took
    its snapshot before ``first`` committed, so its insert of the same key waits for ``first`` and then meets
    the conflict. Both transactions commit."""

    executor = store.executor()
    opened = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with executor.tx(project) as tx:
            await first(tx)
            opened.set()
            await release.wait()

    async def follower() -> None:
        async with executor.tx(project) as tx:
            await second(tx)

    held = asyncio.create_task(holder())
    await opened.wait()
    waiting = asyncio.create_task(follower())
    for _ in range(LOCK_WAIT_POLLS):  # until the second writer waits for the uncommitted row of the first
        blocked = await raw(
            store,
            "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND application_name = :app",
            app="specflow-graph",
        )
        if blocked[0]["n"] or waiting.done():
            break
        await asyncio.sleep(LOCK_WAIT_POLL_SECONDS)
    assert not waiting.done() and blocked[0]["n"] == 1
    release.set()
    await asyncio.gather(held, waiting)


async def test_two_concurrent_writers_of_one_spec_node_both_land(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    def rows(props: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"uuid": "u1", "label": MODULE, "props": props}]

    async def one(tx: Any) -> None:
        await tx.run(bq.CREATE_SPEC_NODES, rows=rows({"spec_id": "M::1", "a": 1, "embedding": vec(1.0)}), build_id="b")

    async def two(tx: Any) -> None:
        assert await tx.run(bq.CREATE_SPEC_NODES, rows=rows({"b": 2, "a": None}), build_id="b") == [{"written": 1}]

    await _race(store, project, one, two)
    (node,) = await raw(
        store, "SELECT props, embedding IS NOT NULL AS v FROM graph.node WHERE group_id = :p", p=project
    )
    assert node["props"] == {"uuid": "u1", "spec_id": "M::1", "b": 2}  # both patches, the later null removed a
    assert node["v"] is True  # a patch without an embedding keeps the vector of the other writer


async def test_two_concurrent_writers_of_one_document_and_one_fragment_both_land(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    async def docs(tx: Any, props: dict[str, Any]) -> None:
        await tx.run(bq.UPSERT_DOCUMENTS, rows=[{"document_id": "d1", "props": props}], build_id="b")

    await _race(store, project, lambda tx: docs(tx, {"path": "p"}), lambda tx: docs(tx, {"title": "t"}))
    (doc,) = await raw(store, "SELECT props FROM graph.node WHERE group_id = :p AND kind = 'document'", p=project)
    assert doc["props"] == {"document_id": "d1", "path": "p", "title": "t", "last_build_id": "b"}

    async def frags(tx: Any, props: dict[str, Any]) -> None:
        rows = [{"fragment_id": "f1", "document_id": "d1", "props": props}]
        assert await tx.run(bq.UPSERT_FRAGMENTS, rows=rows, build_id="b") == [{"written": 1}]

    await _race(store, project, lambda tx: frags(tx, {"text": "x"}), lambda tx: frags(tx, {"status": "current"}))
    (frag,) = await raw(store, "SELECT props FROM graph.node WHERE group_id = :p AND kind = 'fragment'", p=project)
    assert frag["props"] == {"fragment_id": "f1", "text": "x", "status": "current", "last_build_id": "b"}
    (contains,) = await raw(store, "SELECT count(*) AS n FROM graph.edge WHERE group_id = :p", p=project)
    assert contains["n"] == 1


async def test_two_concurrent_writers_of_one_edge_both_land(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    for key in ("s", "t"):
        await build.upsert_node(FR, key, {"spec_id": f"R::{key}", "name": key})

    async def edge(tx: Any, props: dict[str, Any]) -> None:
        row = {"source": "s", "target": "t", "edge_key": "k", "props": props}
        assert await tx.run(bq.upsert_edges(RELATES), rows=[row]) == [{"written": 1}]

    await _race(store, project, lambda tx: edge(tx, {"basis": "stated"}), lambda tx: edge(tx, {"note": "n"}))
    (found,) = await raw(store, "SELECT props FROM graph.edge WHERE group_id = :p", p=project)
    assert found["props"] == {"edge_key": "k", "basis": "stated", "note": "n"}

    async def inferred(tx: Any, key: str, props: dict[str, Any]) -> None:
        row = {
            "source": "s",
            "target": "t",
            "edge_key": key,
            "rule": "r",
            "either_direction": False,
            "uuid": f"e-{key}",
            "created_at": 1,
            "props": props,
            "ref": key,
        }
        await tx.run(bq.infer_write_edges(INTEGRATES), rows=[row], inferred=INFERRED)

    await _race(
        store,
        project,
        lambda tx: inferred(tx, "ik", {"basis": INFERRED, "inference_rule": "r", "one": 1}),
        lambda tx: inferred(tx, "ik", {"basis": INFERRED, "inference_rule": "r", "two": 2}),
    )
    (found,) = await raw(
        store, "SELECT props FROM graph.edge WHERE group_id = :p AND rel_type = :t", p=project, t=INTEGRATES
    )
    assert found["props"]["one"] == 1 and found["props"]["two"] == 2


async def test_a_merge_maps_both_ends_of_an_edge_between_nodes_merged_into_different_survivors(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    for key in ("a", "c", "s1", "s2"):
        await build.upsert_node(FR, key, {"spec_id": f"R::{key}", "name": key})
    props = {"basis": "stated", "weight": 3}
    seeded = [{"source": "a", "target": "c", "edge_key": "k-ac", "props": props}]
    async with store.executor().tx(project) as tx:
        await tx.run(bq.upsert_edges(RELATES), rows=seeded)
        rows = [{"survivor": "s1", "merged": ["a"], "aliases": []}, {"survivor": "s2", "merged": ["c"], "aliases": []}]
        assert await tx.run(bq.MERGE_SPEC_NODES, rows=rows, build_id="b", owned_types=[]) == [{"written": 2}]
    edges = await raw(
        store,
        "SELECT s.uuid AS s, t.uuid AS t, e.rel_type AS r, e.props AS p FROM graph.edge e "
        "JOIN graph.node s ON s.id = e.source_id JOIN graph.node t ON t.id = e.target_id WHERE e.group_id = :p",
        p=project,
    )
    assert edges == [{"s": "s1", "t": "s2", "r": RELATES, "p": {"edge_key": "k-ac", **props}}]


async def test_moved_edges_with_one_key_and_one_pair_of_ends_collapse_to_one(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    for key in ("old1", "old2", "new", "other"):
        await build.upsert_node(FR, key, {"spec_id": f"R::{key}", "name": key})
    seeded = [
        {"source": "old1", "target": "other", "edge_key": "k", "props": {"x": 1}},
        {"source": "old2", "target": "other", "edge_key": "k", "props": {"x": 2}},  # same key, other props
    ]
    async with store.executor().tx(project) as tx:
        await tx.run(bq.upsert_edges(RELATES), rows=seeded)
        row = {"survivor": "new", "merged": ["old1", "old2"], "aliases": []}
        assert await tx.run(bq.MERGE_SPEC_NODES, rows=[row], build_id="b", owned_types=[]) == [{"written": 1}]
    (count,) = await raw(store, "SELECT count(*) AS n FROM graph.edge WHERE group_id = :p", p=project)
    assert count["n"] == 1

    other_project = project + "-2"
    other = writer(store, ontology, other_project)
    for key in ("old", "new", "t"):
        await other.upsert_node(FR, key, {"spec_id": f"R::{key}", "name": key})
    async with store.executor().tx(other_project) as tx:
        await tx.run(
            bq.upsert_edges(RELATES),
            rows=[
                {"source": "old", "target": "t", "edge_key": "k", "props": {"x": 1}},
                {"source": "new", "target": "t", "edge_key": "k", "props": {"x": 2}},
            ],
        )
        moved = await tx.run(
            q.MOVE_FOREIGN_RELATIONSHIPS, from_key="old", from_label=FR, to_key="new", to_label=FR, owned_types=[]
        )
    assert moved == [{"moved": 1}]  # the key is already on the new ends: the moved edge is dropped, no 23505
    (count,) = await raw(store, "SELECT count(*) AS n FROM graph.edge WHERE group_id = :p", p=other_project)
    assert count["n"] == 1


async def test_update_edges_applies_every_matching_row_in_order(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    for key in ("s", "t"):
        await build.upsert_node(FR, key, {"spec_id": f"R::{key}", "name": key})
    async with store.executor().tx(project) as tx:
        await tx.run(
            bq.upsert_edges(RELATES), rows=[{"source": "s", "target": "t", "edge_key": "k1", "props": {"uuid": "e1"}}]
        )
        rows = [
            {"uuid": "e1", "keys": ["k1", "k2"], "props": {"a": 1, "b": 1}},
            {"uuid": "e1", "keys": ["k1"], "props": {"b": 2, "c": 3}},
            {"uuid": "other", "keys": ["k1"], "props": {"never": True}},
        ]
        assert await tx.run(bq.update_edges(RELATES), rows=rows) == [{"written": 1}]
    (found,) = await raw(store, "SELECT props FROM graph.edge WHERE group_id = :p", p=project)
    assert found["props"] == {"edge_key": "k1", "uuid": "e1", "a": 1, "b": 2, "c": 3}


async def test_replacing_node_links_keeps_fragments_and_takes_the_last_row_of_a_node(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    await build.upsert_node(FR, "u1", {"spec_id": "R::1", "name": "one"})
    executor = store.executor()
    async with executor.tx(project) as tx:
        await tx.run(bq.UPSERT_DOCUMENTS, rows=[{"document_id": "d1", "props": {}}], build_id="b")
        await tx.run(
            bq.UPSERT_FRAGMENTS,
            rows=[{"fragment_id": "f1", "document_id": "d1", "props": {"text": "kept"}}],
            build_id="b",
        )
        link = {"fragment_id": "f1", "line_start": 1, "line_end": 2}
        rows = [
            {"uuid": "u1", "evidence": [{"fragment_id": "gone", "line_start": 1, "line_end": 1}], "mentions": []},
            {"uuid": "u1", "evidence": [link], "mentions": [{"fragment_id": "f2", "count": 2}]},
        ]
        assert await tx.run(bq.REPLACE_NODE_LINKS, rows=rows) == [{"written": 1}]
    found = await raw(
        store,
        "SELECT f.fragment_id AS f, f.props AS fp, e.rel_type AS r FROM graph.edge e "
        "JOIN graph.node f ON f.id = e.target_id "
        "WHERE e.group_id = :p AND e.rel_type <> 'CONTAINS' ORDER BY f.fragment_id",
        p=project,
    )
    assert [(row["f"], row["r"]) for row in found] == [("f1", "REFERENCED_BY"), ("f2", "MENTIONED_IN")]
    assert found[0]["fp"] == {"fragment_id": "f1", "text": "kept", "last_build_id": "b"}  # not rewritten
    (stubs,) = await raw(
        store, "SELECT count(*) AS n FROM graph.node WHERE group_id = :p AND fragment_id = 'gone'", p=project
    )
    assert stubs["n"] == 0  # the dropped row of the same uuid made no stub


async def test_a_vector_search_skips_vectors_of_another_dimension(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    await build.upsert_node(FR, "u-a", {"spec_id": "R::a", "embedding": vec(1.0), "embedding_model": "m"})
    await build.upsert_node(FR, "u-b", {"spec_id": "R::b", "embedding_model": "m"})
    await raw(store, "UPDATE graph.node SET embedding = '[1,0,0,0]' WHERE group_id = :p AND uuid = 'u-b'", p=project)
    params = {"model": "m", "vector": vec(1.0), "labels": [FR], "where": {}, "limit": 10}
    page = await store.executor().read(q.SEARCH_NODES_VECTOR, project, index="i", k=10, **params)
    assert [hit["spec_id"] for hit in page[0]["hits"]] == ["R::a"]


async def test_connections_carry_a_statement_and_a_lock_timeout(ontology: Ontology) -> None:
    default = postgres_store(live_config())
    try:
        async with default._engine.connect() as connection:
            shown = await connection.execute(text("SHOW statement_timeout"))
            assert shown.scalar_one() == "5min"
            shown = await connection.execute(text("SHOW lock_timeout"))
            assert shown.scalar_one() == "1min"
    finally:
        await default.close()
    short = StoreConfig(
        **{**live_config().model_dump(), "uri": os.environ["GRAPH_STORE_URI"] + "?statement_timeout=0.3"}
    )
    limited = postgres_store(short)
    try:
        with pytest.raises(GraphWriteError) as raised:
            await limited.executor().write(
                Query.literal("sleep", "SELECT pg_sleep(5) FROM (SELECT cast(:project_id AS text) AS p) AS x"), "p"
            )
        assert raised.value.fields["backend_code"] == "57014" and raised.value.fields["attempts"] == 1
    finally:
        await limited.close()


async def test_an_existing_schema_of_another_version_is_refused(ontology: Ontology) -> None:
    schema = f"pytest_pg_{uuid.uuid4().hex[:8]}"
    first = postgres_store(live_config(schema))
    try:
        await first.ensure_schema(ontology, DIM)
        assert await first.ensure_schema(ontology, DIM) == ()
        for version in ("1", "99", None):
            statement = (
                "UPDATE graph.meta SET meta_value = :v WHERE meta_key = 'schema_version'"
                if version
                else "DELETE FROM graph.meta WHERE meta_key = 'schema_version'"
            )
            async with first._engine.begin() as connection:
                sql = render_schema(statement, schema)
                await connection.execute(text(sql), {"v": version} if version else {})
            with pytest.raises(ConfigurationError) as raised:
                await first.ensure_schema(ontology, DIM)
            assert raised.value.code == "store_schema_outdated"
            assert raised.value.fields["found_version"] == version and raised.value.fields["expected_version"] == "2"
    finally:
        async with first._engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await first.close()
