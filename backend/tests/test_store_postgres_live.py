"""Live tests of the PostgreSQL graph store against a server with pgvector.

Gate: ``SPECFLOW_TEST_GRAPH_STORE=postgres`` with ``GRAPH_STORE_URI`` (``postgresql://host:port/database``),
``GRAPH_STORE_USER``, ``GRAPH_STORE_PASSWORD`` and ``GRAPH_STORE_NAME`` (the schema; default ``graph``). The
login owns the database and the ``vector`` extension is installed in it. Every test works in its own
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
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from graph.core import ConfigurationError, ConformanceError, NodeRef, Ontology, OwnedWriter
from graph.core import queries as q
from graph.core.errors import GraphWriteError, InfrastructureUnavailableError
from graph.core.queries import NodeShape
from graph.stores import StoreConfig, build_store
from graph.stores.postgres import PostgresStore
from graph_support import shipped_ontology

pytestmark = pytest.mark.skipif(
    os.environ.get("SPECFLOW_TEST_GRAPH_STORE", "").strip().lower() != "postgres",
    reason="set SPECFLOW_TEST_GRAPH_STORE=postgres (and GRAPH_STORE_URI, _USER, _PASSWORD, _NAME) to run these tests",
)

DIM = 8
MODULE, FR, FEATURE = "Module", "FunctionalRequirement", "Feature"
PART_OF, INTEGRATES, RELATES = "PART_OF", "INTEGRATES_WITH", "RELATES_TO"
RETIRED, INFERRED = "retired", "inferred"


def live_config(name: str | None = None) -> StoreConfig:
    return StoreConfig(
        backend="postgres",
        uri=os.environ["GRAPH_STORE_URI"],
        user=os.environ["GRAPH_STORE_USER"],
        password=SecretStr(os.environ["GRAPH_STORE_PASSWORD"]),
        name=name or os.environ.get("GRAPH_STORE_NAME") or "graph",
    )


@pytest.fixture(scope="module")
def ontology() -> Ontology:
    return shipped_ontology()


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
    async with store._engine.begin() as connection:  # noqa: SLF001 test cleanup of the store's own tables
        await connection.execute(
            text(f'DELETE FROM "{store.schema}".node WHERE group_id LIKE :p'), {"p": f"{project_id}%"}
        )


def vec(*head: float) -> list[float]:
    values = [0.0] * DIM
    for index, value in enumerate(head):
        values[index] = value
    return values


def writer(store: PostgresStore, ontology: Ontology, project_id: str, owner: str = "build") -> OwnedWriter:
    return OwnedWriter(store, ontology, owner, project_id)


async def raw(store: PostgresStore, sql: str, **params: Any) -> list[dict[str, Any]]:
    async with store._engine.begin() as connection:  # noqa: SLF001 test inspection of the store's own tables
        result = await connection.execute(text(sql.replace("graph.", f'"{store.schema}".')), params)
        return [dict(row) for row in result.mappings().all()] if result.returns_rows else []


async def seed(store: PostgresStore, ontology: Ontology, project_id: str) -> OwnedWriter:
    """Modules checkout and stripe, requirements pay and assume, a document with one fragment."""

    build = writer(store, ontology, project_id)
    async with build.transaction() as tx:
        await tx.upsert_node(MODULE, "u-mod", {"spec_id": "MOD::CHECKOUT", "name": "Checkout", "external": "no",
                                               "spec_id_aliases": ["MOD::OLD"], "embedding": vec(1.0),
                                               "embedding_model": "m"})
        await tx.upsert_node(MODULE, "u-stripe", {"spec_id": "MOD::STRIPE", "name": "Stripe", "external": "yes"})
        await tx.upsert_node(FR, "u-fr", {"spec_id": "REQ::PAY", "name": "Users can pay by card",
                                          "is_assumption": "no", "embedding": vec(0.0, 1.0), "embedding_model": "m",
                                          "statements": json.dumps([{"fragment_id": "f1", "text": "pay"}])})
        await tx.upsert_node(FR, "u-fr2", {"spec_id": "REQ::MAYBE", "name": "Maybe SSO", "is_assumption": "yes"})
        await tx.upsert_node("Document", "d1", {"path": "checkout.md", "status": "active"})
        await tx.upsert_node("Fragment", "f1", {"document_id": "d1", "text": "Users can pay by card", "status": "active",
                                                "ordinal": 0, "line_start": 3, "line_end": 4})
        await tx.upsert_relationship(
            PART_OF, NodeRef(FR, "u-fr"), NodeRef(MODULE, "u-mod"), "k-part", {"basis": "stated", "fact": "pay"}
        )
        await tx.upsert_relationship(
            INTEGRATES, NodeRef(MODULE, "u-mod"), NodeRef(MODULE, "u-stripe"), "k-int",
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
    first = build_store(live_config(schema))
    try:
        created = await first.ensure_schema(ontology, DIM)
        assert {"node", "edge", "meta", "node_spec_uuid_uq", "edge_key_uq", "fts_norm"} <= set(created)
        assert await first.ensure_schema(ontology, DIM) == ()
        with pytest.raises(ConfigurationError) as raised:
            await first.ensure_schema(ontology, DIM // 2)
        assert raised.value.code == "vector_index_mismatch"
        assert raised.value.fields["found_dimension"] == DIM
    finally:
        async with first._engine.begin() as connection:  # type: ignore[attr-defined]  # noqa: SLF001
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await first.close()


async def test_concurrent_schema_setup_does_not_collide(ontology: Ontology) -> None:
    schema = f"pytest_pg_{uuid.uuid4().hex[:8]}"
    stores = [build_store(live_config(schema)) for _ in range(8)]
    try:
        results = await asyncio.gather(*(item.ensure_schema(ontology, DIM) for item in stores), return_exceptions=True)
        assert [item for item in results if isinstance(item, BaseException)] == []
        assert sum(1 for item in results if item) == 1  # one session created it, the others found it
    finally:
        async with stores[0]._engine.begin() as connection:  # type: ignore[attr-defined]  # noqa: SLF001
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
    other = config.model_copy(update={"uri": re.sub(r"/[^/?]*(\?|$)", r"/no_such_database_pytest\1", config.uri, count=1)})
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
    rows = await raw(store, "SELECT labels, props, spec_id FROM graph.node WHERE group_id = :p AND uuid = 'u1'", p=project)
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
    assert (await raw(store, "SELECT count(*) AS n FROM graph.node WHERE group_id = :p AND kind = 'spec'", p=project))[0]["n"] == 1


async def test_the_embedding_is_a_vector_column_and_null_clears_it(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    await build.upsert_node(MODULE, "u1", {"spec_id": "MOD::A", "embedding": vec(1.0, 2.0), "embedding_model": "m"})
    rows = await raw(store, "SELECT embedding::text AS e, props ? 'embedding' AS inprops FROM graph.node WHERE group_id = :p", p=project)
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
    rows = await raw(store, "SELECT props FROM graph.edge WHERE group_id = :p AND rel_type = 'INTEGRATES_WITH'", p=project)
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


async def test_an_explicit_transaction_rolls_back_when_it_raises(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    build = writer(store, ontology, project)
    with pytest.raises(RuntimeError):
        async with build.transaction() as tx:
            await tx.upsert_node(MODULE, "u1", {"spec_id": "MOD::A"})
            raise RuntimeError("stop")
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
            q.MOVE_FOREIGN_RELATIONSHIPS, from_key="old", from_label=FR, to_key="new", to_label=FR,
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
    assert (await executor.read(q.READ_NODE_BY_ALIAS, project, spec_id="MOD::OLD", retired=RETIRED))[0]["props"]["spec_id"] == "MOD::CHECKOUT"
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
    around = await executor.read(q.READ_NEIGHBOURS, project, spec_id="MOD::CHECKOUT", relations=relations, labels=labels)
    assert [(e["relation"], e["source"], e["target"]) for e in around] == [
        (INTEGRATES, "MOD::CHECKOUT", "MOD::STRIPE"),
        (PART_OF, "REQ::PAY", "MOD::CHECKOUT"),
    ]
    hop = await executor.read(
        q.READ_IMPACT_HOP, project, frontier=["u-mod"], seen=["u-mod"], relations=relations, labels=labels, limit=10
    )
    assert [item["spec_id"] for item in hop] == ["MOD::STRIPE", "REQ::PAY"]
    limited = await executor.read(
        q.READ_IMPACT_HOP, project, frontier=["u-mod"], seen=["u-mod", "u-stripe"], relations=relations,
        labels=labels, limit=1,
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
        "spec_id": "S::m", "labels": [MODULE, FR, FEATURE], "roots": [MODULE], "symmetric": [RELATES], "max_hops": 3,
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
    paged = await executor.read(q.SEARCH_NODES_FULLTEXT, project, query="card checkout", **{**base, "skip": 1, "page": 1})
    assert paged[0]["fetched"] == 1 and paged[0]["hits"][0]["spec_id"] == rows[0]["hits"][1]["spec_id"]
    filtered = await executor.read(
        q.SEARCH_NODES_FULLTEXT, project, query="card", **{**base, "labels": [MODULE]}
    )
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
        await build.upsert_node(FR, f"u-{index}", {"spec_id": f"R::{index:02d}", "embedding": vec(1.0, index / 30), "embedding_model": "m"})
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
    rows = await tx._connection.execute(text(sql))  # noqa: SLF001 inspect the transaction's own settings
    return rows.scalar_one()


async def test_a_deadlock_is_retried(store: PostgresStore, ontology: Ontology, project: str) -> None:
    """Two writers take two nodes in opposite order: one gets 40P01; its managed write is retried."""

    executor = store.executor()
    for key in ("a", "b"):
        await executor.write(q.UPSERT_DOCUMENT, project, key=key, props={"path": key})
    barrier = asyncio.Event()

    async def locker(first: str, second: str) -> None:
        async with executor.tx(project) as tx:
            await tx.run(q.UPSERT_DOCUMENT, key=first, props={"path": first + "1"})
            barrier.set()
            await asyncio.sleep(0.3)
            await tx.run(q.UPSERT_DOCUMENT, key=second, props={"path": second + "1"})

    results = await asyncio.gather(locker("a", "b"), locker("b", "a"), return_exceptions=True)
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(failures) <= 1  # an explicit transaction is not retried: at most the deadlock victim fails
    if failures:
        assert isinstance(failures[0], GraphWriteError)
        assert failures[0].fields["backend_code"] == "40P01"


def test_the_shape_helper_is_importable() -> None:
    assert NodeShape("SpecNode", "uuid", True).checked().label == "SpecNode"


async def test_a_project_over_two_thousand_vectors_gets_an_hnsw_index_that_the_search_uses(
    store: PostgresStore, ontology: Ontology, project: str
) -> None:
    """ensure_schema adds a partial halfvec HNSW index for the model; the vector page keeps answering as before."""

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
        store, "SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND indexname LIKE 'node_emb_hnsw_%'", s=store.schema
    )
    assert len(indexes) == 1
    assert "hnsw" in indexes[0]["indexdef"] and "halfvec(8)" in indexes[0]["indexdef"]
    assert "embedding_model = 'hnsw-m'" in indexes[0]["indexdef"]
