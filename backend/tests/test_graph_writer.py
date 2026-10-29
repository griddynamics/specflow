"""Offline tests of ``OwnedWriter`` and ``GraphReader`` guards (plan 1.T4; FR-CORE-04, NFR-SEC-02, I4).

A fake ``Executor`` records every query the writer or reader would run (text and parameters) and
answers with fixed rows, so the tests see exactly what reaches Neo4j: for a refused operation,
nothing. The renamed-ontology tests run the same code over a copy of the shipped ontology whose
labels, relations and id prefixes are all renamed (rule I4: library code knows the format only).
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

import graph.core.ontology as ontology_package
from graph.core import (
    ConfigurationError,
    ConformanceError,
    Executor,
    GraphError,
    GraphReader,
    NodeRef,
    Ontology,
    OwnedWriter,
    OwnershipError,
    load_ontology,
)
from graph.core.db import _bind
from graph.core.queries import CORE_DDL, CORE_DDL_FILES, UPSERT_DOCUMENT, UPSERT_FRAGMENT, Query, upsert_foreign_node
from graph.core.query_files import read_ddl_file
from graph.core.schema import schema_objects
from graph_support import load_gap_ddl, shipped_ontology

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

PROJECT = "proj-1"
MODULE, FR, FINDING = "Module", "FunctionalRequirement", "Finding"
CHECKOUT, STRIPE, PAY = NodeRef(MODULE, "u-mod"), NodeRef(MODULE, "u-stripe"), NodeRef(FR, "u-fr")
DOC, FRAG = NodeRef("Document", "d1"), NodeRef("Fragment", "f1")

DEFAULT_ROWS: dict[str, list[dict[str, Any]]] = {
    "upsert_spec_node": [{"label_mismatch": False, "spec_id_changed": False}],
    "upsert_node_": [{"written": 1}],
    "upsert_relationship_": [{"conflict": False}],
    "delete_": [{"deleted": 1}],
    "relationship_types_": [{"types": []}],
    "spec_node_relationship_types": [{"types": []}],
    "spec_node_exists": [{"found": 1}],
    "move_foreign_relationships": [{"moved": 0}],
}


class FakeExecutor(Executor):
    """Records ``(mode, query, params)`` of every query; refuses unscoped queries like the real one."""

    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows = {**DEFAULT_ROWS, **(rows or {})}
        self.runs: list[tuple[str, Query, dict[str, Any]]] = []
        self.transactions = 0

    def answer(self, mode: str, query: Query, project_id: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        bound = _bind(query, project_id, params)
        self.runs.append((mode, query, bound))
        for prefix, rows in self.rows.items():
            if query.name.startswith(prefix):
                return [dict(row) for row in rows]
        return []

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        return self.answer("read", query, project_id, params)

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        return self.answer("write", query, project_id, params)

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[Any]:
        self.transactions += 1
        executor = self

        class Transaction:
            async def run(self, query: Query, /, **params: Any) -> list[dict[str, Any]]:
                return executor.answer("tx", query, project_id, params)

        yield Transaction()

    def texts(self) -> list[str]:
        return [query.text for _, query, _ in self.runs]

    def dump(self) -> str:
        """Every query text and parameter of the run, as one string to search."""

        return json.dumps([[query.text, params] for _, query, params in self.runs], sort_keys=True, default=str)


def writer(
    owner: str = "build", ontology: Ontology | None = None, executor: FakeExecutor | None = None
) -> tuple[OwnedWriter, FakeExecutor]:
    executor = executor or FakeExecutor()
    return OwnedWriter(executor, ontology or shipped_ontology(), owner, PROJECT), executor


async def refused(awaitable: Any, error_type: type[GraphError], code: str) -> None:
    with pytest.raises(error_type) as raised:
        await awaitable
    assert raised.value.code == code, raised.value


# Writer guards (FR-CORE-04 and the writer's own checks)


@pytest.mark.parametrize("owner", ["planner", "Build", "", "core"])
def test_an_unknown_owner_is_refused(owner: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        OwnedWriter(FakeExecutor(), shipped_ontology(), owner, PROJECT)
    assert raised.value.code == "unknown_owner"


@pytest.mark.parametrize(
    ("label", "key", "props"),
    [
        (MODULE, "u-mod", {"name": "x", "group_id": "other-project"}),
        (MODULE, "u-mod", {"uuid": "u-other"}),
        ("Document", "d1", {"document_id": "d2"}),
        ("Document", "d1", {"group_id": "other-project"}),
        ("Fragment", "f1", {"fragment_id": "f2"}),
    ],
)
async def test_node_writes_refuse_reserved_properties(label: str, key: str, props: dict[str, Any]) -> None:
    build, executor = writer()
    await refused(build.upsert_node(label, key, props), ConformanceError, "property_reserved")
    assert executor.runs == []


async def test_foreign_node_writes_refuse_reserved_properties(gap_ontology: Ontology) -> None:
    gap, executor = writer("gap", gap_ontology)
    for props in ({"uuid": "other"}, {"group_id": "other-project"}):
        await refused(gap.upsert_node(FINDING, "g1", props), ConformanceError, "property_reserved")
    assert executor.runs == []


@pytest.mark.parametrize("props", [{"group_id": "other-project"}, {"edge_key": "other-key"}])
async def test_relationship_writes_refuse_reserved_properties(props: dict[str, Any]) -> None:
    build, executor = writer()
    await refused(
        build.upsert_relationship("PART_OF", PAY, CHECKOUT, "k", props), ConformanceError, "property_reserved"
    )
    assert executor.runs == []


async def test_a_nested_writer_transaction_is_refused() -> None:
    build, executor = writer()

    async def nested() -> None:
        async with build.transaction() as tx:
            await tx.upsert_node(MODULE, "u-1", {"name": "one"})
            async with tx.transaction():
                pytest.fail("a nested transaction was opened")

    with pytest.raises(ConformanceError) as raised:
        await nested()
    assert raised.value.code == "writer_transaction_nested"
    assert executor.transactions == 1
    assert [mode for mode, _, _ in executor.runs] == ["tx"]


async def test_the_spec_node_label_itself_is_not_a_node_label() -> None:
    build, executor = writer()
    await refused(build.upsert_node("SpecNode", "u-1", {}), ConformanceError, "not_a_node_label")
    await refused(build.delete_node("SpecNode", "u-1"), ConformanceError, "not_a_node_label")
    assert executor.runs == []


async def test_a_label_is_not_a_relationship_type() -> None:
    build, executor = writer()
    await refused(
        build.upsert_relationship(MODULE, CHECKOUT, STRIPE, "k", {}), ConformanceError, "not_a_relationship_type"
    )
    await refused(build.delete_relationship(MODULE, "k"), ConformanceError, "not_a_relationship_type")
    assert executor.runs == []


async def test_gap_cannot_write_documents_or_fragments(gap_ontology: Ontology) -> None:
    gap, executor = writer("gap", gap_ontology)
    for ref in (DOC, FRAG):
        await refused(gap.upsert_node(ref.label, ref.key, {"text": "x"}), OwnershipError, "type_not_owned")
        await refused(gap.delete_node(ref.label, ref.key), OwnershipError, "type_not_owned")
    assert executor.runs == []


async def test_keyed_node_upserts_merge_on_a_uniquely_constrained_key(gap_ontology: Ontology, tmp_path: Path) -> None:
    """Document and Fragment use their fixed-label queries; a foreign node MERGEs its label on uuid.

    Each key is the one a ``graph_*`` uniqueness constraint holds (``schema_objects``), so the MERGE
    takes the constraint's lock and seeks its index.
    """

    build, build_runs = writer()
    await build.upsert_node(DOC.label, DOC.key, {"path": "a.md"})
    await build.upsert_node(FRAG.label, FRAG.key, {"text": "t"})
    gap, gap_runs = writer("gap", gap_ontology)
    await gap.upsert_node(FINDING, "g1", {"title": "Missing SLA"})

    queries = [query for _, query, _ in [*build_runs.runs, *gap_runs.runs]]
    assert [query.name for query in queries] == [UPSERT_DOCUMENT.name, UPSERT_FRAGMENT.name, "upsert_node_Finding"]
    assert queries[:2] == [UPSERT_DOCUMENT, UPSERT_FRAGMENT]
    assert "MERGE (n:Document {group_id: $project_id, document_id: $key})" in queries[0].text
    assert "MERGE (n:Fragment {group_id: $project_id, fragment_id: $key})" in queries[1].text
    assert "MERGE (n:Finding {group_id: $project_id, uuid: $key})" in queries[2].text
    assert [params["key"] for _, _, params in [*build_runs.runs, *gap_runs.runs]] == ["d1", "f1", "g1"]
    ddl = load_gap_ddl(tmp_path)
    unique = {(item.label, item.properties) for item in schema_objects(gap_ontology, ddl) if item.kind == "unique"}
    assert {
        ("Document", ("group_id", "document_id")),
        ("Fragment", ("group_id", "fragment_id")),
        (FINDING, ("group_id", "uuid")),
    } <= unique


@pytest.mark.parametrize("label", ["Document", "Fragment", "SpecNode"])
def test_a_built_in_label_is_not_upserted_as_a_foreign_node(label: str) -> None:
    """Their key is not ``uuid`` (or, for ``SpecNode``, the label is checked): no foreign MERGE for them."""

    with pytest.raises(ConformanceError) as raised:
        upsert_foreign_node(label)
    assert raised.value.code == "node_key_unconstrained"


async def test_a_spec_id_change_is_reported_as_immutable() -> None:
    """The query reports it (live test: ``test_spec_id_and_uuid_are_fixed_at_creation``); the writer raises."""

    build, executor = writer(
        executor=FakeExecutor({"upsert_spec_node": [{"label_mismatch": False, "spec_id_changed": True}]})
    )
    await refused(build.upsert_node(MODULE, "u-mod", {"spec_id": "MOD::OTHER"}), ConformanceError, "spec_id_immutable")
    [(_, query, params)] = executor.runs
    assert query.name == "upsert_spec_node"
    assert params["props"] == {"spec_id": "MOD::OTHER"} and params["label"] == MODULE
    label_first = FakeExecutor({"upsert_spec_node": [{"label_mismatch": True, "spec_id_changed": True}]})
    build, _ = writer(executor=label_first)
    await refused(
        build.upsert_node(MODULE, "u-mod", {"spec_id": "MOD::OTHER"}), ConformanceError, "node_label_mismatch"
    )


# NFR-SEC-02: injection-shaped names never reach Cypher

INJECTIONS = [
    "Module) DETACH DELETE (n",
    "PART_OF]->() DETACH DELETE (n) //",
    "Module`",
    "Module:SpecNode",
    "Module {group_id: 'x'}",
    "",
]


@pytest.mark.parametrize("name", INJECTIONS)
async def test_writer_refuses_injection_shaped_names_and_writes_nothing(name: str, gap_ontology: Ontology) -> None:
    for owner in ("build", "gap"):
        owned, executor = writer(owner, gap_ontology)
        calls = [
            owned.upsert_node(name, "k", {"name": "x"}),
            owned.delete_node(name, "k"),
            owned.upsert_relationship(name, CHECKOUT, STRIPE, "k", {}),
            owned.delete_relationship(name, "k"),
            owned.upsert_relationship("PART_OF", NodeRef(name, "k"), CHECKOUT, "k", {}),
            owned.upsert_relationship("PART_OF", PAY, NodeRef(name, "k"), "k", {}),
            owned.move_foreign_relationships(NodeRef(name, "k"), CHECKOUT),
            owned.move_foreign_relationships(CHECKOUT, NodeRef(name, "k")),
        ]
        for call in calls:
            with pytest.raises((OwnershipError, ConformanceError)):
                await call
        assert executor.runs == [], owner


@pytest.mark.parametrize("name", INJECTIONS)
async def test_reader_refuses_injection_shaped_names_and_reads_nothing(name: str) -> None:
    executor = FakeExecutor()
    reader = GraphReader(executor, shipped_ontology())
    await refused(reader.nodes(PROJECT, labels=[name]), ConfigurationError, "unknown_label")
    await refused(reader.nodes(PROJECT, where={name: "x"}), ConfigurationError, "unknown_attribute")
    await refused(reader.neighbours(PROJECT, "MOD::X", relations=[name]), ConfigurationError, "unknown_relation")
    assert executor.runs == []


# I4: a fully renamed ontology flows through schema, writer and reader

LABELS = {
    "Module": "Component",
    "Feature": "Trait",
    "Capability": "Ability",
    "FunctionalRequirement": "BehaviourNeed",
    "QualityRequirement": "QualityNeed",
    "Constraint": "Limit",
    "BusinessRule": "PolicyRule",
    "IntegrationRequirement": "InterfaceNeed",
    "DataRequirement": "DataNeed",
}
RELATIONS = {
    "PART_OF": "BELONGS_TO",
    "GROUPED_BY": "CLUSTERED_UNDER",
    "REALIZED_BY": "DELIVERED_BY",
    "INTEGRATES_WITH": "TALKS_TO",
    "RELATES_TO": "LINKED_WITH",
}
PREFIXES = {"MOD::": "CMP::", "FEAT::": "TRT::", "CAP::": "ABL::", "REQ::": "NEED::"}
OLD_NAMES = re.compile(r"\b(" + "|".join([*LABELS, *RELATIONS]) + r")\b|" + "|".join(map(re.escape, PREFIXES)))


@pytest.fixture(scope="module")
def renamed(tmp_path_factory: pytest.TempPathFactory) -> Ontology:
    shipped_dir = Path(ontology_package.__file__).parent
    text = (shipped_dir / "spec-ontology.yaml").read_text(encoding="utf-8")
    for old, new in {**LABELS, **RELATIONS}.items():
        text = re.sub(rf"\b{old}\b", new, text)
    for old, new in PREFIXES.items():
        text = text.replace(old, new)
    directory = tmp_path_factory.mktemp("renamed")
    shutil.copytree(shipped_dir / "judge_prompts", directory / "judge_prompts")
    (directory / "ontology.yaml").write_text(text, encoding="utf-8")
    ontology = load_ontology(directory / "ontology.yaml", submodule_files=[])
    assert set(ontology.labels) == set(LABELS.values())
    assert set(ontology.relations) == set(RELATIONS.values())
    assert ontology.id_prefix("Component") == "CMP::"
    return ontology


@pytest.fixture(scope="module")
def renamed_ddl(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, ...]:
    """The shipped DDL files with every relation renamed as in ``renamed`` (the DDL a renamed ontology ships)."""

    shipped_dir = Path(ontology_package.__file__).parents[1] / "ddl"
    directory = tmp_path_factory.mktemp("renamed-ddl")
    statements: list[str] = []
    for name in CORE_DDL_FILES:
        text = (shipped_dir / name).read_text(encoding="utf-8")
        for old, new in RELATIONS.items():
            text = re.sub(rf"\b{old}\b", new, text).replace(
                f"graph_edge_key_{old.lower()}", f"graph_edge_key_{new.lower()}"
            )
        (directory / name).write_text(text, encoding="utf-8")
        statements.extend(read_ddl_file(directory / name))
    return tuple(statements)


def test_the_shipped_ddl_does_not_cover_the_renamed_relations(renamed: Ontology) -> None:
    """The ontology never renders a statement: renamed relations without their DDL stop schema setup."""

    with pytest.raises(ConfigurationError) as raised:
        schema_objects(renamed, CORE_DDL)
    assert raised.value.code == "schema_ddl_incomplete"
    assert raised.value.fields["missing"] == len(RELATIONS)


def test_renamed_schema_objects_use_the_renamed_relations(renamed: Ontology, renamed_ddl: tuple[str, ...]) -> None:
    objects = schema_objects(renamed, renamed_ddl)
    edge_indexes = {item.label: item.name for item in objects if item.entity == "RELATIONSHIP"}
    assert edge_indexes == {new: f"graph_edge_key_{new.lower()}" for new in RELATIONS.values()}
    texts = " ".join(item.query().text for item in objects)
    assert OLD_NAMES.search(texts) is None
    assert ":BELONGS_TO]" in texts


async def test_renamed_writer_builds_queries_from_the_renamed_names(renamed: Ontology) -> None:
    build, executor = writer("build", renamed)
    component, need = NodeRef("Component", "u-c"), NodeRef("BehaviourNeed", "u-n")
    await build.upsert_node("Component", component.key, {"spec_id": "CMP::CHECKOUT", "name": "Checkout"})
    await build.upsert_node("BehaviourNeed", need.key, {"spec_id": "NEED::FR::PAY", "name": "Pay"})
    await build.upsert_relationship("BELONGS_TO", need, component, "k1", {"basis": "stated"})
    assert await build.delete_relationship("TALKS_TO", "k2") is True
    await build.move_foreign_relationships(component, NodeRef("Component", "u-other"))

    labels = [params["label"] for _, query, params in executor.runs if query.name == "upsert_spec_node"]
    assert labels == ["Component", "BehaviourNeed"]
    texts = executor.texts()
    assert any("[r:BELONGS_TO {group_id: $project_id, edge_key: $key}]" in text for text in texts)
    assert any("[r:TALKS_TO {group_id: $project_id, edge_key: $key}]" in text for text in texts)
    [owned] = [
        params["owned_types"] for _, query, params in executor.runs if query.name == "move_foreign_relationships"
    ]
    assert owned == sorted({"CONTAINS", "REFERENCED_BY", "MENTIONED_IN", *RELATIONS.values()})
    assert OLD_NAMES.search(executor.dump()) is None

    executor.runs.clear()
    await refused(build.upsert_node("Module", "u-x", {}), OwnershipError, "type_not_owned")
    await refused(build.upsert_relationship("PART_OF", need, component, "k", {}), OwnershipError, "type_not_owned")
    await refused(
        build.upsert_relationship("BELONGS_TO", component, need, "k", {}), ConformanceError, "relationship_pair_illegal"
    )
    assert executor.runs == []


async def test_renamed_reader_filters_by_the_renamed_names(renamed: Ontology) -> None:
    node_row = {
        "labels": ["SpecNode", "Component"],
        "props": {"spec_id": "CMP::CHECKOUT", "uuid": "u-c", "name": "Checkout"},
        "provenance": [],
    }
    executor = FakeExecutor({"read_node_by_spec_id": [node_row], "read_nodes": [node_row]})
    reader = GraphReader(executor, renamed)

    nodes = await reader.nodes(PROJECT)
    assert [(n.spec_id, n.label) for n in nodes] == [("CMP::CHECKOUT", "Component")]
    await reader.nodes(PROJECT, labels=["Component", "Trait"])
    await reader.edges(PROJECT)
    await reader.neighbours(PROJECT, "CMP::CHECKOUT", relations=["BELONGS_TO"])

    by_name = [(query.name, params) for _, query, params in executor.runs]
    assert set(by_name[0][1]["labels"]) == set(LABELS.values())
    assert by_name[1][1]["labels"] == ["Component", "Trait"]
    assert by_name[2][0] == "read_edges"
    assert set(by_name[2][1]["relations"]) == set(RELATIONS.values())
    assert set(by_name[2][1]["labels"]) == set(LABELS.values())
    assert by_name[-1][0] == "read_neighbours" and by_name[-1][1]["relations"] == ["BELONGS_TO"]
    assert OLD_NAMES.search(executor.dump()) is None

    executor.runs.clear()
    await refused(reader.nodes(PROJECT, labels=["Module"]), ConfigurationError, "unknown_label")
    await refused(
        reader.neighbours(PROJECT, "CMP::CHECKOUT", relations=["PART_OF"]), ConfigurationError, "unknown_relation"
    )
    assert executor.runs == []
