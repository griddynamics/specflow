"""Live Neo4j tests of the graph core (plan 1.T4): schema, project isolation, owned writes, reads.

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local``. Every test works in its own
``pytest-graph-<hex>`` project ids; ``neo4j_project`` deletes them afterwards, asserts nothing is
left and that nothing outside the test projects changed. The schema test temporarily drops one
``graph_*`` range index of the shared dev database and restores it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from graph.core import (
    ConfigurationError,
    ConformanceError,
    NodeRef,
    Ontology,
    OwnedWriter,
    OwnershipError,
    ensure_schema,
    require_version,
    server_version,
)
from graph_support import (
    Neo4jProject,
    live_llm_config,
    shipped_ontology,
)
from specflow_live import requires_live

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

MODULE = "Module"
FR = "FunctionalRequirement"
FEATURE = "Feature"
FINDING = "Finding"
DOCUMENT = "Document"
FRAGMENT = "Fragment"

CHECKOUT = NodeRef(MODULE, "u-mod")
STRIPE = NodeRef(MODULE, "u-stripe")
PAY = NodeRef(FR, "u-fr")
ASSUMED = NodeRef(FR, "u-fr2")
DOC = NodeRef(DOCUMENT, "d1")
FRAG = NodeRef(FRAGMENT, "f1")
GAP_FINDING = NodeRef(FINDING, "g1")
PART_OF = "PART_OF"

PART_KEY = "requirement|id|pay|PART_OF|Module|surface|checkout"
INTEGRATES_KEY = "Module|surface|checkout|INTEGRATES_WITH|Module|surface|stripe"
STATEMENTS = json.dumps(
    [{"fragment_id": "f1", "document_id": "d1", "revision_id": "r1", "version": 1, "text": "Users can pay by card"}]
)


async def seed(project: Neo4jProject, project_id: str, ontology: Ontology) -> OwnedWriter:
    """The same small graph in any project: same keys, same spec ids, same properties."""

    build = project.writer("build", project_id=project_id, ontology=ontology)
    async with build.transaction() as tx:
        await tx.upsert_node(
            MODULE,
            CHECKOUT.key,
            {
                "spec_id": "MOD::CHECKOUT",
                "name": "Checkout",
                "spec_id_aliases": ["MOD::OLD_CHECKOUT"],
                "external": "no",
                "type": "backend",
                "stmt": "The checkout service.",
            },
        )
        await tx.upsert_node(MODULE, STRIPE.key, {"spec_id": "MOD::STRIPE", "name": "Stripe", "external": "yes"})
        await tx.upsert_node(
            FR,
            PAY.key,
            {"spec_id": "REQ::FR::PAY", "name": "Users can pay", "is_assumption": "no", "statements": STATEMENTS},
        )
        await tx.upsert_node(
            FR, ASSUMED.key, {"spec_id": "REQ::FR::ASSUME", "name": "Maybe SSO", "is_assumption": "yes"}
        )
        await tx.upsert_node(DOCUMENT, DOC.key, {"spec_id": "DOC::D1", "path": "checkout.md", "status": "active"})
        await tx.upsert_node(
            FRAGMENT,
            FRAG.key,
            {
                "spec_id": "FRG::F1",
                "document_id": "d1",
                "text": "Users can pay by card",
                "status": "active",
                "ordinal": 0,
            },
        )
        await tx.upsert_relationship(PART_OF, PAY, CHECKOUT, PART_KEY, {"basis": "stated", "fact": "pay"})
        await tx.upsert_relationship(
            "INTEGRATES_WITH",
            CHECKOUT,
            STRIPE,
            INTEGRATES_KEY,
            {
                "basis": "inferred",
                "inference_rule": "INF-INT-01",
                "confidence": 0.9,
                "evidence": json.dumps([{"fragment_id": "f1", "quote": "pay"}]),
            },
        )
    # Structural relationships are written by build's persist queries (later milestone): raw here.
    await project.run(
        "MATCH (d:Document {group_id: $p, document_id: 'd1'}), (f:Fragment {group_id: $p, fragment_id: 'f1'}), "
        "(n:SpecNode {group_id: $p, uuid: 'u-fr'}) "
        "CREATE (d)-[:CONTAINS]->(f), (n)-[:REFERENCED_BY {line_start: 3, line_end: 4}]->(f)",
        p=project_id,
    )
    return build


async def node_props(project: Neo4jProject, key: str, project_id: str | None = None) -> dict[str, Any]:
    rows = await project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: $k}) RETURN properties(n) AS props",
        p=project_id or project.project_id,
        k=key,
    )
    assert len(rows) == 1
    return rows[0]["props"]


async def assert_code(awaitable: Any, error_type: type[Exception], code: str) -> None:
    with pytest.raises(error_type) as raised:
        await awaitable
    assert raised.value.code == code


# Schema (FR-CORE-02)


async def show_objects(project: Neo4jProject) -> list[dict[str, Any]]:
    constraints = await project.run(
        "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties "
        "RETURN name, type, entityType, labelsOrTypes, properties"
    )
    indexes = await project.run(
        "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, options, state "
        "RETURN name, type, entityType, labelsOrTypes, properties, options, state"
    )
    return constraints + indexes


def find_object(
    objects: list[dict[str, Any]], types: set[str], entity: str, label: str, properties: list[str]
) -> dict[str, Any] | None:
    for row in objects:
        if (
            row["type"] in types
            and row["entityType"] == entity
            and list(row["labelsOrTypes"] or []) == [label]
            and sorted(row["properties"] or []) == sorted(properties)
        ):
            return row
    return None


async def test_schema_has_every_data_model_object_and_is_idempotent(neo4j_project: Neo4jProject) -> None:
    project, ontology, llm = neo4j_project, shipped_ontology(), live_llm_config()
    assert await ensure_schema(project.driver, ontology, llm, project.database) == ()

    objects = await show_objects(project)
    unique = {"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS", "NODE_KEY"}
    expected = [
        (unique, "NODE", "SpecNode", ["group_id", "spec_id"]),
        (unique, "NODE", "SpecNode", ["group_id", "uuid"]),
        (unique, "NODE", DOCUMENT, ["group_id", "document_id"]),
        (unique, "NODE", FRAGMENT, ["group_id", "fragment_id"]),
        ({"RANGE"}, "NODE", "SpecNode", ["group_id", "identity_key"]),
        ({"FULLTEXT"}, "NODE", "SpecNode", ["name", "statement_text"]),
        ({"FULLTEXT"}, "NODE", FRAGMENT, ["text"]),
        *[({"RANGE"}, "RELATIONSHIP", relation, ["group_id", "edge_key"]) for relation in ontology.relations],
    ]
    for types, entity, label, properties in expected:
        assert find_object(objects, types, entity, label, properties), (entity, label, properties)
    for prop in ("embedding", "name_embedding"):
        vector = find_object(objects, {"VECTOR"}, "NODE", "SpecNode", [prop])
        assert vector is not None, prop
        config = vector["options"]["indexConfig"]
        assert config["vector.dimensions"] == llm.embedding_dim
        assert config["vector.similarity_function"].lower() == "cosine"
    assert all(row["state"] == "ONLINE" for row in objects if row["name"].startswith("graph_") and "state" in row)

    # A missing object is created again under its graph_* name; the next run changes nothing.
    # The dev database is shared: drop only the library's own index, never an equivalent one a
    # developer created under another name (ensure_schema would put it back as graph_*), and
    # restore it in ``finally`` whatever happens.
    relates = find_object(objects, {"RANGE"}, "RELATIONSHIP", "RELATES_TO", ["group_id", "edge_key"])
    assert relates is not None
    if relates["name"] == "graph_edge_key_relates_to":
        await project.run("DROP INDEX graph_edge_key_relates_to")
        try:
            assert await ensure_schema(project.driver, ontology, llm, project.database) == (
                "graph_edge_key_relates_to",
            )
        finally:
            assert await ensure_schema(project.driver, ontology, llm, project.database) == ()


async def test_schema_vector_dimension_mismatch_raises_and_creates_nothing(neo4j_project: Neo4jProject) -> None:
    project = neo4j_project
    before = await show_objects(project)
    other_dim = live_llm_config().embedding_dim // 2
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(
            project.driver,
            shipped_ontology(),
            live_llm_config().model_copy(update={"embedding_dim": other_dim}),
            project.database,
        )
    assert raised.value.code == "vector_index_mismatch"
    after = await show_objects(project)
    assert sorted(row["name"] for row in after) == sorted(row["name"] for row in before)


async def test_nfr_port_01_server_is_neo4j_community_5_26_or_later(neo4j_project: Neo4jProject) -> None:
    project = neo4j_project
    version = await server_version(project.driver, project.database)
    assert version >= (5, 26)
    assert await require_version(project.driver, database=project.database) == version
    rows = await project.run("CALL dbms.components() YIELD name, edition RETURN name, edition")
    assert {row["name"]: row["edition"] for row in rows}["Neo4j Kernel"] == "community"


# Project isolation (FR-CORE-01)


async def test_two_projects_with_equal_content_are_isolated(
    neo4j_project: Neo4jProject, gap_ontology: Ontology
) -> None:
    project = neo4j_project
    a, b = project.project_id, project.new_project()
    for project_id in (a, b):
        await seed(project, project_id, gap_ontology)
        gap = project.writer("gap", project_id=project_id, ontology=gap_ontology)
        await gap.upsert_node(FINDING, GAP_FINDING.key, {"title": "Missing SLA"})
        await gap.upsert_relationship("FLAGS", GAP_FINDING, CHECKOUT, "g1|FLAGS|u-mod", {"severity": "low"})
    b_before = await project.snapshot(b)

    # Every write operation in A, on keys that exist in B too.
    build_a = project.writer("build", project_id=a, ontology=gap_ontology)
    await build_a.upsert_node(MODULE, CHECKOUT.key, {"name": "Checkout service"})
    await build_a.upsert_relationship("INTEGRATES_WITH", CHECKOUT, STRIPE, INTEGRATES_KEY, {"fact": "changed"})
    assert await build_a.delete_relationship(PART_OF, PART_KEY) is True
    assert await build_a.delete_node(FR, ASSUMED.key) is True
    assert await build_a.move_foreign_relationships(CHECKOUT, STRIPE) == 1
    gap_a = project.writer("gap", project_id=a, ontology=gap_ontology)
    await gap_a.upsert_node(FINDING, GAP_FINDING.key, {"title": "changed"})
    async with build_a.transaction() as tx:
        await tx.upsert_node(MODULE, "u-new", {"spec_id": "MOD::NEW", "name": "New"})

    assert await project.snapshot(b) == b_before

    # Reads of A see only A; reads of B see only B.
    reader = project.reader(ontology=gap_ontology)
    node_a, node_b = await reader.node(a, "MOD::CHECKOUT"), await reader.node(b, "MOD::CHECKOUT")
    assert node_a is not None and node_a.name == "Checkout service"
    assert node_b is not None and node_b.name == "Checkout"
    assert (await reader.node(a, "MOD::OLD_CHECKOUT")).name == "Checkout service"
    assert (await reader.node(b, "MOD::OLD_CHECKOUT")).name == "Checkout"
    assert await reader.node(b, "MOD::NEW") is None
    assert [n.spec_id for n in await reader.nodes(a)] == ["MOD::CHECKOUT", "MOD::NEW", "MOD::STRIPE", "REQ::FR::PAY"]
    assert [n.spec_id for n in await reader.nodes(b)] == [
        "MOD::CHECKOUT",
        "MOD::STRIPE",
        "REQ::FR::ASSUME",
        "REQ::FR::PAY",
    ]
    edges_a, edges_b = await reader.edges(a), await reader.edges(b)
    assert [(e.relation, e.fact) for e in edges_a] == [("INTEGRATES_WITH", "changed")]
    assert sorted(e.relation for e in edges_b) == ["INTEGRATES_WITH", PART_OF]
    assert len(await reader.neighbours(a, "MOD::CHECKOUT")) == 1
    assert len(await reader.neighbours(b, "MOD::CHECKOUT")) == 2
    assert [d.document_id for d in await reader.documents(a)] == ["d1"]
    assert [f.fragment_id for f in await reader.fragments(a)] == ["f1"]
    pay_a = await reader.node(a, "REQ::FR::PAY")
    assert pay_a is not None and [p.fragment_id for p in pay_a.provenance] == ["f1"]


# Ownership (FR-CORE-04)


async def test_writer_refuses_types_of_another_owner_and_writes_nothing(
    neo4j_project: Neo4jProject, gap_ontology: Ontology
) -> None:
    project = neo4j_project
    build = await seed(project, project.project_id, gap_ontology)
    gap = project.writer("gap", ontology=gap_ontology)
    await gap.upsert_node(FINDING, GAP_FINDING.key, {"title": "Missing SLA"})
    before = await project.snapshot()

    refused = [
        gap.upsert_node(MODULE, "u-gap-module", {"name": "X"}),
        gap.upsert_node(MODULE, CHECKOUT.key, {"name": "X"}),
        gap.upsert_relationship(PART_OF, PAY, CHECKOUT, "gap-part", {}),
        gap.delete_node(MODULE, CHECKOUT.key),
        gap.delete_relationship(PART_OF, PART_KEY),
        gap.move_foreign_relationships(CHECKOUT, STRIPE),
        build.upsert_node(FINDING, "g2", {"title": "x"}),
        build.upsert_relationship("FLAGS", GAP_FINDING, CHECKOUT, "build-flags", {}),
        build.delete_node(FINDING, GAP_FINDING.key),
        build.delete_relationship("FLAGS", "build-flags"),
    ]
    for call in refused:
        with pytest.raises(OwnershipError):
            await call

    # A refused operation inside a grouped transaction rolls the whole group back.
    async def refused_group() -> None:
        async with gap.transaction() as tx:
            await tx.upsert_node(FINDING, "g-rolled-back", {"title": "rolled back"})
            await tx.upsert_node(MODULE, "u-x", {"name": "bad"})

    with pytest.raises(OwnershipError):
        await refused_group()

    assert await project.snapshot() == before


async def test_gap_cannot_delete_its_node_with_relationships_of_another_owner(
    neo4j_project: Neo4jProject, gap_ontology: Ontology
) -> None:
    project = neo4j_project
    await seed(project, project.project_id, gap_ontology)
    gap = project.writer("gap", ontology=gap_ontology)
    await gap.upsert_node(FINDING, GAP_FINDING.key, {"title": "Missing SLA"})
    await gap.upsert_relationship("CITES", GAP_FINDING, FRAG, "g1|CITES|f1", {})
    await project.run(
        "MATCH (g:Finding {group_id: $p, uuid: 'g1'}), (f:Fragment {group_id: $p, fragment_id: 'f1'}) "
        "CREATE (g)-[:MENTIONED_IN]->(f)",
        p=project.project_id,
    )
    before = await project.snapshot()
    await assert_code(gap.delete_node(FINDING, GAP_FINDING.key), OwnershipError, "node_has_foreign_relationships")
    assert await project.snapshot() == before

    await project.run("MATCH (:Finding {group_id: $p})-[r:MENTIONED_IN]->() DELETE r", p=project.project_id)
    assert await gap.delete_node(FINDING, GAP_FINDING.key) is True
    assert await gap.delete_node(FINDING, GAP_FINDING.key) is False


# Owned properties (FR-CORE-05)


async def test_build_update_leaves_foreign_properties_unchanged(
    neo4j_project: Neo4jProject, gap_ontology: Ontology
) -> None:
    project = neo4j_project
    build = await seed(project, project.project_id, gap_ontology)
    await project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: 'u-mod'}) SET n.gap_note = 'keep me' "
        "WITH n MATCH (n)<-[r:PART_OF]-() SET r.gap_weight = 7",
        p=project.project_id,
    )

    await build.upsert_node(MODULE, CHECKOUT.key, {"name": "Checkout service", "stmt": None})
    await build.upsert_relationship(PART_OF, PAY, CHECKOUT, PART_KEY, {"fact": "pays"})

    props = await node_props(project, CHECKOUT.key)
    assert props["gap_note"] == "keep me"
    assert props["name"] == "Checkout service"
    assert props["external"] == "no"  # build-owned, not in this write: unchanged
    assert "stmt" not in props  # None removes only that property
    assert props["group_id"] == project.project_id
    rows = await project.run(
        "MATCH ()-[r:PART_OF {group_id: $p, edge_key: $k}]->() RETURN properties(r) AS props",
        p=project.project_id,
        k=PART_KEY,
    )
    assert len(rows) == 1
    assert rows[0]["props"]["gap_weight"] == 7
    assert rows[0]["props"]["fact"] == "pays"
    assert rows[0]["props"]["basis"] == "stated"


async def test_upserts_are_idempotent_and_labels_never_change(neo4j_project: Neo4jProject) -> None:
    project = neo4j_project
    build = await seed(project, project.project_id, shipped_ontology())
    before = await project.snapshot()
    await build.upsert_node(MODULE, CHECKOUT.key, {"name": "Checkout"})
    await build.upsert_relationship(PART_OF, PAY, CHECKOUT, PART_KEY, {"basis": "stated"})
    assert await project.snapshot() == before
    rows = await project.run(
        "MATCH (n:SpecNode {group_id: $p, uuid: 'u-mod'}) RETURN labels(n) AS labels", p=project.project_id
    )
    assert sorted(rows[0]["labels"]) == [MODULE, "SpecNode"]
    await assert_code(build.upsert_node(FEATURE, CHECKOUT.key, {"name": "x"}), ConformanceError, "node_label_mismatch")
    assert await project.snapshot() == before


async def test_spec_id_and_uuid_are_fixed_at_creation(neo4j_project: Neo4jProject) -> None:
    """data-model.md: ``uuid`` and ``spec_id`` are fixed at creation; a change writes nothing."""

    project = neo4j_project
    build = await seed(project, project.project_id, shipped_ontology())
    before = await project.snapshot()
    for props in (
        {"spec_id": "MOD::RENAMED", "name": "changed"},
        {"spec_id": None, "name": "changed"},
    ):
        await assert_code(build.upsert_node(MODULE, CHECKOUT.key, props), ConformanceError, "spec_id_immutable")
    await assert_code(
        build.upsert_node(MODULE, CHECKOUT.key, {"uuid": "u-other"}), ConformanceError, "property_reserved"
    )

    async def refused_group() -> None:
        async with build.transaction() as tx:
            await tx.upsert_node(MODULE, "u-grouped", {"spec_id": "MOD::GROUPED", "name": "grouped"})
            await tx.upsert_node(MODULE, CHECKOUT.key, {"spec_id": "MOD::OTHER"})

    with pytest.raises(ConformanceError):
        await refused_group()
    assert await project.snapshot() == before

    # The same spec_id is no change; a node without one may get its first.
    await build.upsert_node(MODULE, CHECKOUT.key, {"spec_id": "MOD::CHECKOUT", "name": "Checkout v2"})
    await build.upsert_node(MODULE, "u-new", {"name": "New"})
    await build.upsert_node(MODULE, "u-new", {"spec_id": "MOD::NEW"})
    await assert_code(
        build.upsert_node(MODULE, "u-new", {"spec_id": "MOD::NEWER"}), ConformanceError, "spec_id_immutable"
    )
    assert (await node_props(project, CHECKOUT.key))["spec_id"] == "MOD::CHECKOUT"
    assert (await node_props(project, CHECKOUT.key))["name"] == "Checkout v2"
    assert (await node_props(project, "u-new"))["spec_id"] == "MOD::NEW"
    # The label check still comes first.
    await assert_code(
        build.upsert_node(FEATURE, CHECKOUT.key, {"spec_id": "FEAT::X"}), ConformanceError, "node_label_mismatch"
    )


# The foreign relationship move (W3)


async def test_move_foreign_relationships(neo4j_project: Neo4jProject, gap_ontology: Ontology) -> None:
    project = neo4j_project
    build = project.writer("build", ontology=gap_ontology)
    gap = project.writer("gap", ontology=gap_ontology)
    modules = {name: NodeRef(MODULE, f"u-{name.lower()}") for name in ("OLD", "NEW", "X")}
    for name, ref in modules.items():
        await build.upsert_node(MODULE, ref.key, {"spec_id": f"MOD::{name}", "name": name})
    old, new, x = modules["OLD"], modules["NEW"], modules["X"]
    await build.upsert_node(DOCUMENT, DOC.key, {"path": "a.md"})
    await build.upsert_node(FRAGMENT, FRAG.key, {"document_id": "d1", "text": "t"})
    await gap.upsert_node(FINDING, GAP_FINDING.key, {"title": "f"})

    await gap.upsert_relationship("FLAGS", GAP_FINDING, old, "e-f-old", {"w": 1})
    await gap.upsert_relationship("FLAGS", old, new, "e-old-new", {"w": 2})
    await gap.upsert_relationship("FLAGS", new, old, "e-new-old", {"w": 3})
    await gap.upsert_relationship("FLAGS", old, old, "e-loop", {"w": 4})
    await gap.upsert_relationship("FLAGS", old, x, "e-old-x", {"w": 5})
    # An equal relationship already on the survivor is not duplicated.
    await project.run(
        "MATCH (f:Finding {group_id: $p, uuid: 'g1'}), (o:SpecNode {group_id: $p, uuid: 'u-old'}), "
        "(n:SpecNode {group_id: $p, uuid: 'u-new'}) "
        "CREATE (f)-[:FLAGS {group_id: $p, edge_key: 'dup', w: 6}]->(o), "
        "(f)-[:FLAGS {group_id: $p, edge_key: 'dup', w: 6}]->(n), "
        "(o)-[:REFERENCED_BY {line_start: 1}]->(:Fragment {group_id: $p, fragment_id: 'f-old'})",
        p=project.project_id,
    )
    await build.upsert_relationship("INTEGRATES_WITH", old, x, "own", {})

    # Refusals first: nothing moves.
    await assert_code(gap.move_foreign_relationships(old, new), OwnershipError, "move_requires_build")
    await assert_code(
        build.move_foreign_relationships(old, NodeRef(MODULE, "u-missing")), ConformanceError, "move_target_missing"
    )
    await assert_code(build.move_foreign_relationships(GAP_FINDING, new), ConformanceError, "move_needs_spec_nodes")
    assert await build.move_foreign_relationships(old, old) == 0
    assert await project.run("MATCH ()-[r:FLAGS {group_id: $p}]->() RETURN count(r) AS n", p=project.project_id) == [
        {"n": 7}
    ]

    assert await build.move_foreign_relationships(old, new) == 6

    rows = await project.run(
        "MATCH (s)-[r:FLAGS {group_id: $p}]->(t) "
        "RETURN coalesce(s.name, 'F') AS s, coalesce(t.name, 'F') AS t, r.edge_key AS k, r.w AS w",
        p=project.project_id,
    )
    assert sorted((r["s"], r["t"], r["k"], r["w"]) for r in rows) == [
        ("F", "NEW", "dup", 6),
        ("F", "NEW", "e-f-old", 1),
        ("NEW", "NEW", "e-loop", 4),  # a self-loop of OLD becomes a self-loop of NEW
        ("NEW", "X", "e-old-x", 5),
    ]  # OLD<->NEW relationships are deleted, not turned into self-loops
    kept = await project.run(
        "MATCH (o:SpecNode {group_id: $p, uuid: 'u-old'})-[r]-() RETURN type(r) AS t ORDER BY t",
        p=project.project_id,
    )
    assert [row["t"] for row in kept] == ["INTEGRATES_WITH", "REFERENCED_BY"]  # build-owned stay
    assert await build.move_foreign_relationships(old, new) == 0


# Structural relationship types (built-in, no edge_key)


@pytest.mark.parametrize("rel_type", ["CONTAINS", "REFERENCED_BY", "MENTIONED_IN"])
async def test_owned_writer_refuses_structural_relationship_types(
    neo4j_project: Neo4jProject, gap_ontology: Ontology, rel_type: str
) -> None:
    project = neo4j_project
    build = await seed(project, project.project_id, gap_ontology)
    gap = project.writer("gap", ontology=gap_ontology)
    before = await project.snapshot()
    source = DOC if rel_type == "CONTAINS" else CHECKOUT
    code = "structural_relationship_not_supported"
    await assert_code(build.upsert_relationship(rel_type, source, FRAG, "k", {}), ConformanceError, code)
    await assert_code(build.delete_relationship(rel_type, "k"), ConformanceError, code)
    await assert_code(gap.upsert_relationship(rel_type, source, FRAG, "k", {}), ConformanceError, code)
    assert await project.snapshot() == before


async def test_foreign_relation_to_fragment_is_written_once(
    neo4j_project: Neo4jProject, gap_ontology: Ontology
) -> None:
    project = neo4j_project
    await seed(project, project.project_id, gap_ontology)
    gap = project.writer("gap", ontology=gap_ontology)
    await gap.upsert_node(FINDING, GAP_FINDING.key, {"title": "f"})
    await gap.upsert_relationship("CITES", GAP_FINDING, FRAG, "cite-1", {"note": "x"})
    await gap.upsert_relationship("CITES", GAP_FINDING, FRAG, "cite-1", {"note": "y"})
    rows = await project.run(
        "MATCH (:Finding {group_id: $p})-[r:CITES]->(:Fragment {group_id: $p}) RETURN r.note AS note, r.group_id AS g",
        p=project.project_id,
    )
    assert rows == [{"note": "y", "g": project.project_id}]
    assert await gap.delete_relationship("CITES", "cite-1") is True
    assert await gap.delete_relationship("CITES", "cite-1") is False


# Relationship keys and endpoints


async def test_edge_key_names_one_pair_of_endpoints(neo4j_project: Neo4jProject) -> None:
    project = neo4j_project
    build = project.writer("build")
    refs = {name: NodeRef(MODULE, f"u-{name.lower()}") for name in ("A", "B", "C")}
    for name, ref in refs.items():
        await build.upsert_node(MODULE, ref.key, {"spec_id": f"MOD::{name}", "name": name})
    a, b, c = refs["A"], refs["B"], refs["C"]

    await build.upsert_relationship("INTEGRATES_WITH", a, b, "k1", {"fact": "1"})
    await build.upsert_relationship("INTEGRATES_WITH", a, b, "k1", {"fact": "2"})
    before = await project.snapshot()
    mismatch = "edge_key_endpoint_mismatch"
    await assert_code(build.upsert_relationship("INTEGRATES_WITH", a, c, "k1", {}), ConformanceError, mismatch)
    await assert_code(build.upsert_relationship("INTEGRATES_WITH", b, a, "k1", {}), ConformanceError, mismatch)
    await assert_code(
        build.upsert_relationship("INTEGRATES_WITH", a, NodeRef(MODULE, "u-missing"), "k9", {}),
        ConformanceError,
        "relationship_end_missing",
    )
    await assert_code(
        build.upsert_relationship(PART_OF, a, NodeRef(FR, "u-fr"), "k8", {}),
        ConformanceError,
        "relationship_pair_illegal",
    )
    await assert_code(build.upsert_relationship("NOT_DECLARED", a, b, "k7", {}), OwnershipError, "type_not_owned")
    assert await project.snapshot() == before
    rows = await project.run(
        "MATCH (s)-[r:INTEGRATES_WITH {group_id: $p, edge_key: 'k1'}]->(t) RETURN s.name AS s, t.name AS t, r.fact "
        "AS fact",
        p=project.project_id,
    )
    assert rows == [{"s": "A", "t": "B", "fact": "2"}]

    # Re-keying an edge is delete, then upsert.
    assert await build.delete_relationship("INTEGRATES_WITH", "k1") is True
    await build.upsert_relationship("INTEGRATES_WITH", a, c, "k1", {})
    rows = await project.run(
        "MATCH (s)-[r:INTEGRATES_WITH {group_id: $p, edge_key: 'k1'}]->(t) RETURN s.name AS s, t.name AS t",
        p=project.project_id,
    )
    assert rows == [{"s": "A", "t": "C"}]


# Reader


async def test_reader_resolves_merged_node_through_alias(neo4j_project: Neo4jProject, gap_ontology: Ontology) -> None:
    """FR-READ-01: given A merged into B, ``node(A)`` returns B."""

    project = neo4j_project
    build = project.writer("build", ontology=gap_ontology)
    a, b = NodeRef(MODULE, "u-a"), NodeRef(MODULE, "u-b")
    await build.upsert_node(MODULE, a.key, {"spec_id": "MOD::ORDERS_SERVICE", "name": "Orders service"})
    await build.upsert_node(MODULE, b.key, {"spec_id": "MOD::ORDER_MANAGEMENT", "name": "Order management service"})
    gap = project.writer("gap", ontology=gap_ontology)
    await gap.upsert_node(FINDING, GAP_FINDING.key, {"title": "f"})
    await gap.upsert_relationship("FLAGS", GAP_FINDING, a, "g1|FLAGS|a", {})

    # The merge as build performs it: move foreign relationships, delete A, keep A's id as alias of B.
    assert await build.move_foreign_relationships(a, b) == 1
    assert await build.delete_node(MODULE, a.key) is True
    await build.upsert_node(MODULE, b.key, {"spec_id_aliases": ["MOD::ORDERS_SERVICE"]})

    reader = project.reader(ontology=gap_ontology)
    merged = await reader.node(project.project_id, "MOD::ORDERS_SERVICE")
    assert merged is not None
    assert (merged.spec_id, merged.uuid, merged.name) == ("MOD::ORDER_MANAGEMENT", "u-b", "Order management service")
    assert merged.spec_id_aliases == ("MOD::ORDERS_SERVICE",)
    assert await reader.node(project.project_id, "MOD::ORDER_MANAGEMENT") == merged
    assert await reader.node(project.project_id, "MOD::UNKNOWN") is None


async def test_reader_never_returns_foreign_nodes_documents_or_fragments(
    neo4j_project: Neo4jProject, gap_ontology: Ontology
) -> None:
    """FR-READ-06."""

    project = neo4j_project
    await seed(project, project.project_id, gap_ontology)
    gap = project.writer("gap", ontology=gap_ontology)
    await gap.upsert_node(FINDING, GAP_FINDING.key, {"spec_id": "FIND::MISSING_SLA", "name": "Missing SLA"})
    await gap.upsert_relationship("FLAGS", GAP_FINDING, CHECKOUT, "g1|FLAGS|u-mod", {})
    await gap.upsert_relationship("FLAGS", CHECKOUT, STRIPE, "u-mod|FLAGS|u-stripe", {})
    # A node carrying SpecNode plus a foreign label only (never written by the library).
    await project.run(
        "CREATE (:SpecNode:Finding {group_id: $p, uuid: 'raw', spec_id: 'FIND::RAW', name: 'raw'})",
        p=project.project_id,
    )

    reader = project.reader(ontology=gap_ontology)
    spec_ids = [n.spec_id for n in await reader.nodes(project.project_id)]
    assert spec_ids == ["MOD::CHECKOUT", "MOD::STRIPE", "REQ::FR::ASSUME", "REQ::FR::PAY"]
    for foreign in ("FIND::MISSING_SLA", "FIND::RAW", "DOC::D1", "FRG::F1"):
        assert await reader.node(project.project_id, foreign) is None, foreign
    neighbours = await reader.neighbours(project.project_id, "MOD::CHECKOUT")
    assert sorted(e.relation for e in neighbours) == ["INTEGRATES_WITH", PART_OF]
    assert all(e.relation != "FLAGS" for e in await reader.edges(project.project_id))
    await assert_code(reader.nodes(project.project_id, labels=[FINDING]), ConfigurationError, "unknown_label")
    await assert_code(reader.nodes(project.project_id, labels=[DOCUMENT]), ConfigurationError, "unknown_label")
    await assert_code(
        reader.neighbours(project.project_id, "MOD::CHECKOUT", relations=["FLAGS"]),
        ConfigurationError,
        "unknown_relation",
    )


async def test_reader_filters_by_attribute_values(neo4j_project: Neo4jProject) -> None:
    """FR-READ-08: ``where`` filters; an unknown attribute name raises ``ConfigurationError``."""

    project = neo4j_project
    await seed(project, project.project_id, shipped_ontology())
    reader = project.reader()
    pid = project.project_id

    assert [n.spec_id for n in await reader.nodes(pid, where={"is_assumption": "yes"})] == ["REQ::FR::ASSUME"]
    assert [n.spec_id for n in await reader.nodes(pid, where={"is_assumption": "no"})] == ["REQ::FR::PAY"]
    assert [n.spec_id for n in await reader.nodes(pid, labels=[MODULE], where={"external": "yes"})] == ["MOD::STRIPE"]
    assert [
        n.spec_id for n in await reader.nodes(pid, labels=[MODULE], where={"type": "backend", "external": "no"})
    ] == ["MOD::CHECKOUT"]
    assert await reader.nodes(pid, labels=[MODULE], where={"type": "datastore"}) == []
    assert [n.spec_id for n in await reader.nodes(pid, labels=[MODULE])] == ["MOD::CHECKOUT", "MOD::STRIPE"]

    for labels, where, code in [
        (None, {"severity": "high"}, "unknown_attribute"),
        (None, {"module": "Checkout"}, "unknown_attribute"),  # a ref attribute is never stored
        ([MODULE], {"is_assumption": "yes"}, "unknown_attribute"),  # not declared by the selected label
        (None, {"is_assumption": "maybe"}, "invalid_attribute_value"),
    ]:
        await assert_code(reader.nodes(pid, labels=labels, where=where), ConfigurationError, code)


async def test_retired_fragments_are_not_provenance(neo4j_project: Neo4jProject) -> None:
    project = neo4j_project
    build = await seed(project, project.project_id, shipped_ontology())
    await build.upsert_node(
        FRAGMENT, "f2", {"document_id": "d1", "text": "Old wording", "status": "retired", "ordinal": 1}
    )
    await build.upsert_node(FRAGMENT, "f3", {"document_id": "d1", "text": "Pay by card", "ordinal": 2})  # no status
    await project.run(
        "MATCH (d:Document {group_id: $p, document_id: 'd1'}), (n:SpecNode {group_id: $p, uuid: 'u-fr'}), "
        "(f2:Fragment {group_id: $p, fragment_id: 'f2'}), (f3:Fragment {group_id: $p, fragment_id: 'f3'}) "
        "CREATE (d)-[:CONTAINS]->(f2), (d)-[:CONTAINS]->(f3), "
        "(n)-[:REFERENCED_BY {line_start: 9, line_end: 9}]->(f2), (n)-[:REFERENCED_BY {line_start: 12, line_end: "
        "13}]->(f3)",
        p=project.project_id,
    )
    reader = project.reader()
    pay = await reader.node(project.project_id, "REQ::FR::PAY")
    assert pay is not None
    assert [(p.fragment_id, p.line_start) for p in pay.provenance] == [("f1", 3), ("f3", 12)]
    # the retired fragment is still a fragment of its document, just not evidence
    assert [f.fragment_id for f in await reader.fragments(project.project_id, "d1")] == ["f1", "f2", "f3"]
    await build.upsert_node(FRAGMENT, "f3", {"status": "retired"})
    pay = await reader.node(project.project_id, "REQ::FR::PAY")
    assert pay is not None and [p.fragment_id for p in pay.provenance] == ["f1"]
    assert [p.fragment_id for p in (await reader.nodes(project.project_id, labels=[FR]))[1].provenance] == ["f1"]


async def test_reader_basics(neo4j_project: Neo4jProject) -> None:
    project = neo4j_project
    await seed(project, project.project_id, shipped_ontology())
    reader = project.reader()
    pid = project.project_id

    pay = await reader.node(pid, "REQ::FR::PAY")
    assert pay is not None
    assert (pay.label, pay.uuid, pay.attributes) == (FR, "u-fr", {"is_assumption": "no"})
    assert [(s.fragment_id, s.text) for s in pay.statements] == [("f1", "Users can pay by card")]
    assert [(p.fragment_id, p.path, p.line_start, p.line_end, p.quote) for p in pay.provenance] == [
        ("f1", "checkout.md", 3, 4, "Users can pay by card")
    ]
    checkout = await reader.node(pid, "MOD::CHECKOUT")
    assert checkout is not None
    assert checkout.attributes == {"external": "no", "type": "backend", "stmt": "The checkout service."}

    neighbours = await reader.neighbours(pid, "MOD::OLD_CHECKOUT")
    assert sorted((e.relation, e.source, e.target) for e in neighbours) == [
        ("INTEGRATES_WITH", "MOD::CHECKOUT", "MOD::STRIPE"),
        (PART_OF, "REQ::FR::PAY", "MOD::CHECKOUT"),
    ]
    assert [e.relation for e in await reader.neighbours(pid, "MOD::CHECKOUT", relations=[PART_OF])] == [PART_OF]
    assert await reader.neighbours(pid, "MOD::UNKNOWN") == []

    edges = await reader.edges(pid)
    inferred = [e for e in edges if e.basis == "inferred"]
    assert len(edges) == 2 and len(inferred) == 1
    assert (inferred[0].inference_rule, inferred[0].confidence) == ("INF-INT-01", 0.9)
    assert [(q.fragment_id, q.quote) for q in inferred[0].evidence] == [("f1", "pay")]
    assert [e.relation for e in await reader.edges(pid, include_inferred=False)] == [PART_OF]

    assert [(d.document_id, d.path) for d in await reader.documents(pid)] == [("d1", "checkout.md")]
    assert [f.fragment_id for f in await reader.fragments(pid, "d1")] == ["f1"]
    assert await reader.fragments(pid, "unknown") == []
