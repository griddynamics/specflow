"""Offline tests of ``ensure_schema`` (plan 1.T4; FR-CORE-02, FR-CORE-03; data-model.md "Constraints and indexes").

A stateful fake driver backs ``SHOW CONSTRAINTS`` / ``SHOW INDEXES`` with the exact columns
``graph.core.schema`` reads, applies the DDL statements ``schema_objects`` reads from the static DDL
files (graph/core/ddl/), and answers ``dbms.components()`` and ``db.awaitIndexes``. Nothing connects
to Neo4j.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from neo4j.exceptions import AuthError, ClientError, DatabaseError, ServiceUnavailable

from graph.core import ConfigurationError, InfrastructureUnavailableError, LLMConfig, Ontology, ensure_schema
from graph.core.errors import ConformanceError
from graph.core.queries import (
    AWAIT_INDEXES,
    CORE_DDL,
    CORE_DDL_FILES,
    FOREIGN_KEY,
    SERVER_COMPONENTS,
    SHOW_CONSTRAINTS,
    SHOW_INDEXES,
    foreign_node_constraint,
    load_core_ddl,
)
from graph.core.query_files import read_ddl_file
from graph.core.schema import SchemaObject, foreign_labels, parse_ddl_statement, schema_objects
from graph_support import GAP_DDL_TEXT, llm_config, load_gap_ddl, load_gap_ontology, shipped_ontology

DIM = 16
POPULATING = "POPULATING"
# The Cypher schema file validation/cypher checks the queries and the DDL files against.
GRAPH_SCHEMA_FILE = Path(__file__).resolve().parents[1] / "graph-schema.yaml"
FOREIGN_PLACEHOLDER = "__FOREIGN_LABEL__"


def index_type(item: SchemaObject) -> str:
    return {"range": "RANGE", "vector": "VECTOR", "fulltext": "FULLTEXT"}[item.kind]


def index_row(
    item: SchemaObject,
    *,
    name: str | None = None,
    type_: str | None = None,
    properties: list[str] | None = None,
    state: str = "ONLINE",
    dimension: int = DIM,
    similarity: str = "COSINE",
) -> dict[str, Any]:
    """A ``SHOW INDEXES`` row (the columns ``SHOW_INDEXES`` yields) for ``item`` or a variant of it."""

    options: dict[str, Any] = {"indexProvider": "range-1.0", "indexConfig": {}}
    if item.kind == "vector":
        options = {
            "indexProvider": "vector-2.0",
            "indexConfig": {"vector.dimensions": dimension, "vector.similarity_function": similarity},
        }
    return {
        "name": name or item.name,
        "type": type_ or index_type(item),
        "entityType": item.entity,
        "labelsOrTypes": [item.label],
        "properties": list(properties if properties is not None else item.properties),
        "state": state,
        "options": options,
    }


def constraint_rows(item: SchemaObject, *, name: str | None = None, type_: str | None = None) -> tuple[dict, dict]:
    """A ``SHOW CONSTRAINTS`` row and the backing range index row Neo4j shows with it."""

    constraint = {
        "name": name or item.name,
        "type": type_ or "NODE_PROPERTY_UNIQUENESS",
        "entityType": item.entity,
        "labelsOrTypes": [item.label],
        "properties": list(item.properties),
    }
    backing = index_row(item, name=name or item.name, type_="RANGE")
    return constraint, backing


class SchemaDriver:
    """Fake async driver with a schema state; the DDL statements of ``objects`` add their rows.

    ``fail_on`` maps a query name (``Query.name`` of the text) to an exception raised when it runs;
    ``stays`` keeps every created index in that state (e.g. ``POPULATING`` after awaiting).
    """

    def __init__(self, ontology: Ontology, *, version: str = "5.26.0", ddl: tuple[str, ...] | None = None) -> None:
        self.version = version
        self.objects = {item.query().text: item for item in schema_objects(ontology, ddl)}
        self.constraints: list[dict[str, Any]] = []
        self.indexes: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.create_params: dict[str, dict[str, Any]] = {}
        self.texts: list[str] = []
        self.fail_on: dict[str, Exception] = {}
        self.stays: str = "ONLINE"

    def add_constraint(self, item: SchemaObject, **options: Any) -> None:
        constraint, backing = constraint_rows(item, **options)
        self.constraints.append(constraint)
        self.indexes.append(backing)

    def add_index(self, item: SchemaObject, **options: Any) -> None:
        self.indexes.append(index_row(item, **options))

    def add_all(self, *, rename: bool = False) -> None:
        """Every object of the data model; ``rename`` gives each a name that is not ``graph_*``."""

        for item in self.objects.values():
            name = f"legacy_{item.name}" if rename else None
            if item.kind == "unique":
                self.add_constraint(item, name=name)
            else:
                self.add_index(item, name=name)

    async def execute_query(self, text: str, params: dict[str, Any] | None = None, **_kwargs: Any) -> Any:
        self.texts.append(text)
        name = self._name(text)
        if name in self.fail_on:
            raise self.fail_on[name]
        if text == SERVER_COMPONENTS.text:
            return records([{"name": "Neo4j Kernel", "versions": [self.version], "edition": "community"}])
        if text == SHOW_CONSTRAINTS.text:
            return records(copy.deepcopy(self.constraints))
        if text == SHOW_INDEXES.text:
            return records(copy.deepcopy(self.indexes))
        if text == AWAIT_INDEXES.text:
            assert params == {"timeout_s": 300}
            return records([])
        item = self.objects.get(text)
        assert item is not None, f"unexpected query: {text}"
        self.created.append(item.name)
        self.create_params[item.name] = dict(params or {})
        if item.kind == "unique":
            self.add_constraint(item)
            self.indexes[-1]["state"] = self.stays
        else:
            dimension = (params or {}).get("dimension", DIM)
            self.add_index(item, state=self.stays, dimension=dimension)
        return records([])

    def _name(self, text: str) -> str:
        for query in (SERVER_COMPONENTS, SHOW_CONSTRAINTS, SHOW_INDEXES, AWAIT_INDEXES):
            if text == query.text:
                return query.name
        item = self.objects.get(text)
        return item.query().name if item else "?"


def records(rows: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(records=rows)


@pytest.fixture
def ontology() -> Ontology:
    return shipped_ontology()


@pytest.fixture
def config() -> LLMConfig:
    return llm_config(embedding_dim=DIM)


def objects_by_name(ontology: Ontology, ddl: tuple[str, ...] | None = None) -> dict[str, SchemaObject]:
    return {item.name: item for item in schema_objects(ontology, ddl)}


# The static DDL files are the source of truth (drift guard)


def expected_shipped_objects(ontology: Ontology) -> list[tuple[str, str, str, str, tuple[str, ...]]]:
    """The objects of data-model.md "Constraints and indexes" for ``ontology`` (no foreign label)."""

    return [
        ("graph_spec_node_spec_id", "unique", "NODE", "SpecNode", ("group_id", "spec_id")),
        ("graph_spec_node_uuid", "unique", "NODE", "SpecNode", ("group_id", "uuid")),
        ("graph_document_id", "unique", "NODE", "Document", ("group_id", "document_id")),
        ("graph_fragment_id", "unique", "NODE", "Fragment", ("group_id", "fragment_id")),
        ("graph_spec_node_identity_key", "range", "NODE", "SpecNode", ("group_id", "identity_key")),
        ("graph_spec_node_embedding", "vector", "NODE", "SpecNode", ("embedding",)),
        ("graph_spec_node_name_embedding", "vector", "NODE", "SpecNode", ("name_embedding",)),
        ("graph_spec_node_text", "fulltext", "NODE", "SpecNode", ("name", "statement_text")),
        ("graph_fragment_text", "fulltext", "NODE", "Fragment", ("text",)),
        *[
            (f"graph_edge_key_{relation.lower()}", "range", "RELATIONSHIP", relation, ("group_id", "edge_key"))
            for relation in sorted(ontology.relations)
        ],
    ]


def test_the_shipped_ddl_defines_exactly_the_objects_of_the_shipped_ontology(ontology: Ontology) -> None:
    """Drift guard: every relation of the shipped ontology has its edge-key index in the DDL files, and
    the DDL defines nothing else than the data model's objects."""

    found = [(item.name, item.kind, item.entity, item.label, item.properties) for item in schema_objects(ontology)]
    assert found == expected_shipped_objects(ontology)
    assert len(found) == 9 + len(ontology.relations)


def test_every_ddl_statement_is_one_idempotent_create_with_its_text(ontology: Ontology) -> None:
    assert tuple(statement for name in CORE_DDL_FILES for statement in load_core_ddl(name)) == CORE_DDL
    assert [item.statement for item in schema_objects(ontology)] == list(CORE_DDL)
    for item in schema_objects(ontology):
        assert item.statement.startswith("CREATE ") and f" {item.name} IF NOT EXISTS FOR " in item.statement
        assert item.query().name == f"create_{item.name}" and not item.query().project_scoped
    by_name = objects_by_name(ontology)
    assert by_name["graph_spec_node_spec_id"].statement == (
        "CREATE CONSTRAINT graph_spec_node_spec_id IF NOT EXISTS FOR (n:SpecNode) "
        "REQUIRE (n.group_id, n.spec_id) IS UNIQUE"
    )
    assert by_name["graph_edge_key_part_of"].statement == (
        "CREATE INDEX graph_edge_key_part_of IF NOT EXISTS FOR ()-[r:PART_OF]-() ON (r.group_id, r.edge_key)"
    )
    assert by_name["graph_spec_node_identity_key"].statement == (
        "CREATE INDEX graph_spec_node_identity_key IF NOT EXISTS FOR (n:SpecNode) ON (n.group_id, n.identity_key)"
    )
    assert by_name["graph_spec_node_embedding"].statement == (
        "CREATE VECTOR INDEX graph_spec_node_embedding IF NOT EXISTS FOR (n:SpecNode) ON (n.embedding) OPTIONS "
        "{indexConfig: {`vector.dimensions`: $dimension, `vector.similarity_function`: 'cosine'}}"
    )
    assert by_name["graph_spec_node_text"].statement == (
        "CREATE FULLTEXT INDEX graph_spec_node_text IF NOT EXISTS FOR (n:SpecNode) ON EACH [n.name, n.statement_text]"
    )


def test_a_ddl_file_is_split_into_statements_without_comments(tmp_path: Path) -> None:
    path = tmp_path / "two.cypher"
    path.write_text(
        "// header\nCREATE INDEX graph_a IF NOT EXISTS\n  FOR (n:A) ON (n.group_id, n.a);\n\n"
        "// second\nCREATE CONSTRAINT graph_b IF NOT EXISTS FOR (n:B)\n  REQUIRE (n.group_id, n.b) IS UNIQUE;\n",
        encoding="utf-8",
    )
    assert read_ddl_file(path) == (
        "CREATE INDEX graph_a IF NOT EXISTS FOR (n:A) ON (n.group_id, n.a)",
        "CREATE CONSTRAINT graph_b IF NOT EXISTS FOR (n:B) REQUIRE (n.group_id, n.b) IS UNIQUE",
    )


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE INDEX graph_x FOR (n:SpecNode) ON (n.group_id, n.x)",  # not idempotent
        "CREATE INDEX other_x IF NOT EXISTS FOR (n:SpecNode) ON (n.group_id, n.x)",  # not a graph_* name
        "CREATE INDEX graph_x IF NOT EXISTS FOR (n:SpecNode) ON (m.group_id, n.x)",  # another variable
        "CREATE TEXT INDEX graph_x IF NOT EXISTS FOR (n:SpecNode) ON (n.x)",  # unknown kind
        (  # literal dimension
            "CREATE VECTOR INDEX graph_x IF NOT EXISTS FOR (n:SpecNode) ON (n.embedding) OPTIONS "
            "{indexConfig: {`vector.dimensions`: 16, `vector.similarity_function`: 'cosine'}}"
        ),
        (  # another similarity
            "CREATE VECTOR INDEX graph_x IF NOT EXISTS FOR (n:SpecNode) ON (n.embedding) OPTIONS "
            "{indexConfig: {`vector.dimensions`: $dimension, `vector.similarity_function`: 'euclidean'}}"
        ),
        (  # two vector properties
            "CREATE VECTOR INDEX graph_x IF NOT EXISTS FOR (n:SpecNode) ON (n.a, n.b) OPTIONS "
            "{indexConfig: {`vector.dimensions`: $dimension, `vector.similarity_function`: 'cosine'}}"
        ),
        "MATCH (n) DETACH DELETE n",
    ],
)
def test_a_statement_of_no_known_form_is_refused(ontology: Ontology, statement: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        parse_ddl_statement(statement, position=3)
    assert (raised.value.code, raised.value.fields) == ("schema_ddl_unrecognised", {"position": 3})
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(ontology, (*CORE_DDL, statement))
    assert (raised.value.code, raised.value.fields["position"]) == ("schema_ddl_unrecognised", len(CORE_DDL))


def test_a_name_defined_twice_is_refused(ontology: Ontology) -> None:
    again = CORE_DDL[0].replace("(n.group_id, n.spec_id)", "(n.group_id, n.other)")
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(ontology, (*CORE_DDL, again))
    assert (raised.value.code, raised.value.fields) == (
        "schema_ddl_duplicate_name",
        {"name": "graph_spec_node_spec_id"},
    )


async def test_a_relation_without_its_ddl_statement_fails_loudly(ontology: Ontology, config: LLMConfig) -> None:
    """The ontology never renders a statement: a relation the DDL does not cover stops the setup."""

    ddl = tuple(statement for statement in CORE_DDL if "graph_edge_key_part_of " not in statement)
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(ontology, ddl)
    assert raised.value.code == "schema_ddl_incomplete"
    assert raised.value.fields == {
        "missing": 1,
        "kind": "range",
        "entity": "RELATIONSHIP",
        "label": "PART_OF",
        "properties": "group_id, edge_key",
    }
    driver = SchemaDriver(ontology)
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, ontology, config, ddl=ddl)
    assert raised.value.code == "schema_ddl_incomplete"
    assert driver.texts == []  # nothing reached Neo4j


def test_a_foreign_label_without_its_ddl_statement_fails_loudly(tmp_path: Path) -> None:
    gap = load_gap_ontology(tmp_path)
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(gap)  # the shipped DDL covers neither the gap relations nor its foreign label
    assert raised.value.code == "schema_ddl_incomplete"
    assert raised.value.fields["missing"] == 3
    ddl = tuple(statement for statement in load_gap_ddl(tmp_path) if "graph_foreign_uuid_finding " not in statement)
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(gap, ddl)
    assert raised.value.fields == {
        "missing": 1,
        "kind": "unique",
        "entity": "NODE",
        "label": "Finding",
        "properties": "group_id, uuid",
    }


@pytest.mark.parametrize(
    ("dropped", "fields"),
    [
        (
            "graph_document_id ",
            {
                "missing": 1,
                "kind": "unique",
                "entity": "NODE",
                "label": "Document",
                "properties": "group_id, document_id",
            },
        ),
        (
            "graph_spec_node_name_embedding ",
            {"missing": 1, "kind": "vector", "entity": "NODE", "label": "SpecNode", "properties": "name_embedding"},
        ),
    ],
)
def test_a_core_object_without_its_ddl_statement_fails_loudly(
    ontology: Ontology, dropped: str, fields: dict[str, Any]
) -> None:
    ddl = tuple(statement for statement in CORE_DDL if dropped not in statement)
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(ontology, ddl)
    assert (raised.value.code, raised.value.fields) == ("schema_ddl_incomplete", fields)


def test_a_search_index_under_another_name_fails_loudly(ontology: Ontology) -> None:
    ddl = tuple(statement.replace("graph_fragment_text ", "graph_fragment_words ") for statement in CORE_DDL)
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(ontology, ddl)
    assert (raised.value.code, raised.value.fields) == (
        "schema_ddl_incomplete",
        {"missing": 1, "name": "graph_fragment_text"},
    )


def test_a_schema_object_without_ddl_has_no_query() -> None:
    with pytest.raises(ConformanceError) as raised:
        SchemaObject("graph_x", "range", "NODE", "SpecNode", ("group_id", "x")).query()
    assert raised.value.code == "schema_object_without_ddl"


# Creation and idempotence


async def test_empty_database_gets_every_object_and_a_second_run_creates_none(
    ontology: Ontology, config: LLMConfig
) -> None:
    driver = SchemaDriver(ontology)
    wanted = [item.name for item in schema_objects(ontology)]
    assert len(wanted) == 9 + len(ontology.relations)

    created = await ensure_schema(driver, ontology, config)

    assert created == tuple(wanted)
    assert driver.created == wanted
    for name in ("graph_spec_node_embedding", "graph_spec_node_name_embedding"):
        assert driver.create_params[name] == {"dimension": DIM}
    assert all(row["state"] == "ONLINE" for row in driver.indexes)
    assert {row["name"] for row in driver.constraints} == {
        "graph_spec_node_spec_id",
        "graph_spec_node_uuid",
        "graph_document_id",
        "graph_fragment_id",
    }
    assert {f"graph_edge_key_{relation.lower()}" for relation in ontology.relations} <= {
        row["name"] for row in driver.indexes
    }

    driver.texts.clear()
    assert await ensure_schema(driver, ontology, config) == ()
    assert driver.created == wanted  # nothing new
    assert driver.texts == [
        SERVER_COMPONENTS.text,
        SHOW_CONSTRAINTS.text,
        SHOW_INDEXES.text,
        AWAIT_INDEXES.text,
        SHOW_INDEXES.text,
    ]


# Foreign labels (declared by a submodule ontology file of another owner than build)


def test_the_shipped_ontology_has_no_foreign_label(ontology: Ontology) -> None:
    assert foreign_labels(ontology) == ()
    assert not [item for item in schema_objects(ontology) if item.name.startswith("graph_foreign_uuid_")]


def test_the_cypher_schema_maps_the_foreign_placeholder_to_the_foreign_labels(ontology: Ontology) -> None:
    """Drift guard: graph-schema.yaml declares the ``__FOREIGN_LABEL__.uuid`` constraint of the DDL template,
    and its ``policy.placeholder_labels`` maps ``__FOREIGN_LABEL__`` to exactly ``foreign_labels`` of the
    shipped ontology, the concrete labels whose DDL the cypher parity check then asks for."""

    data = yaml.safe_load(GRAPH_SCHEMA_FILE.read_text(encoding="utf-8"))
    assert f"{FOREIGN_PLACEHOLDER}.{FOREIGN_KEY}" in data["constraints"]["unique"]
    assert FOREIGN_PLACEHOLDER in data["nodes"]
    assert tuple(data["policy"]["placeholder_labels"][FOREIGN_PLACEHOLDER]) == foreign_labels(ontology)
    (template,) = load_core_ddl("foreign_node_constraint.cypher")
    assert f"FOR (n:{FOREIGN_PLACEHOLDER}) REQUIRE (n.group_id, n.{FOREIGN_KEY}) IS UNIQUE" in template


def test_every_foreign_label_gets_a_group_and_uuid_unique_constraint(tmp_path: Path) -> None:
    gap = load_gap_ontology(tmp_path)
    labels = foreign_labels(gap)
    assert labels == tuple(sorted(label for label in gap.labels if gap.owner_of(label) != "build"))
    assert "Finding" in labels
    assert "graph_foreign_uuid_finding" in GAP_DDL_TEXT
    by_name = objects_by_name(gap, load_gap_ddl(tmp_path))
    for label in labels:
        item = by_name[f"graph_foreign_uuid_{label.lower()}"]
        assert (item.kind, item.entity, item.label, item.properties) == ("unique", "NODE", label, ("group_id", "uuid"))
        assert item.query().text == (
            f"CREATE CONSTRAINT {item.name} IF NOT EXISTS FOR (n:{label}) REQUIRE (n.group_id, n.uuid) IS UNIQUE"
        )
    assert {item.label for item in by_name.values() if item.kind == "unique"} == {
        "SpecNode",
        "Document",
        "Fragment",
        *labels,
    }


def test_the_foreign_constraint_template_renders_the_statement_a_submodule_ddl_holds(tmp_path: Path) -> None:
    gap = load_gap_ontology(tmp_path)
    ddl = load_gap_ddl(tmp_path)
    rendered = tuple(foreign_node_constraint(label) for label in foreign_labels(gap))
    assert set(rendered) <= set(ddl)
    assert foreign_node_constraint("Finding") == (
        "CREATE CONSTRAINT graph_foreign_uuid_finding IF NOT EXISTS FOR (n:Finding) "
        "REQUIRE (n.group_id, n.uuid) IS UNIQUE"
    )
    without = tuple(statement for statement in ddl if statement not in rendered)
    assert "graph_foreign_uuid_finding" in {item.name for item in schema_objects(gap, (*without, *rendered))}
    assert not set(rendered) & set(CORE_DDL)
    for label in ("Document", "Fragment", "SpecNode"):
        with pytest.raises(ConformanceError) as raised:
            foreign_node_constraint(label)
        assert raised.value.code == "node_key_unconstrained"


async def test_foreign_constraints_are_created_once(tmp_path: Path, config: LLMConfig) -> None:
    gap = load_gap_ontology(tmp_path)
    ddl = load_gap_ddl(tmp_path)
    driver = SchemaDriver(gap, ddl=ddl)
    foreign = tuple(f"graph_foreign_uuid_{label.lower()}" for label in foreign_labels(gap))
    assert foreign == tuple(item.name for item in schema_objects(gap, ddl) if item.label in foreign_labels(gap))

    created = await ensure_schema(driver, gap, config, ddl=ddl)

    assert created[-len(foreign) :] == foreign
    assert {"graph_edge_key_cites", "graph_edge_key_flags"} <= set(created)
    assert set(foreign) <= {row["name"] for row in driver.constraints}
    assert await ensure_schema(driver, gap, config, ddl=ddl) == ()


async def test_a_foreign_constraint_over_duplicate_nodes_fails_the_setup(tmp_path: Path, config: LLMConfig) -> None:
    """Neo4j refuses a uniqueness constraint over existing duplicates: setup fails as any refused create."""

    gap = load_gap_ontology(tmp_path)
    ddl = load_gap_ddl(tmp_path)
    driver = SchemaDriver(gap, ddl=ddl)
    driver.add_all()
    item = objects_by_name(gap, ddl)["graph_foreign_uuid_finding"]
    driver.constraints = [row for row in driver.constraints if row["name"] != item.name]
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    error = ClientError._hydrate_neo4j(
        code="Neo.ClientError.Schema.ConstraintCreationFailed", message=f"duplicates: {SERVER_REPLY}"
    )
    driver.fail_on[item.query().name] = error
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, gap, config, ddl=ddl)
    assert raised.value.code == "schema_setup_failed"
    assert raised.value.fields["query"] == f"create_{item.name}"
    assert raised.value.fields["neo4j_code"] == "Neo.ClientError.Schema.ConstraintCreationFailed"
    assert "secret-xyz" not in str(raised.value)
    assert driver.created == []


async def test_only_the_missing_object_is_created(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    driver.indexes = [row for row in driver.indexes if row["name"] != "graph_spec_node_text"]
    assert await ensure_schema(driver, ontology, config) == ("graph_spec_node_text",)


async def test_equivalent_objects_under_other_names_are_accepted(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all(rename=True)
    assert await ensure_schema(driver, ontology, config) == ()
    assert driver.created == []


@pytest.mark.parametrize("constraint", ["UNIQUENESS", "NODE_PROPERTY_UNIQUENESS", "NODE_KEY"])
async def test_a_unique_or_node_key_constraint_counts_as_the_unique_constraint(
    ontology: Ontology, config: LLMConfig, constraint: str
) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    item = objects_by_name(ontology)["graph_spec_node_uuid"]
    driver.constraints = [row for row in driver.constraints if row["name"] != item.name]
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    driver.add_constraint(item, name="node_key_uuid", type_=constraint)
    assert await ensure_schema(driver, ontology, config) == ()


async def test_a_relationship_uniqueness_constraint_is_not_a_node_one(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    item = objects_by_name(ontology)["graph_document_id"]
    driver.constraints = [row for row in driver.constraints if row["name"] != item.name]
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    driver.constraints.append({**constraint_rows(item, name="rel_unique")[0], "entityType": "RELATIONSHIP"})
    assert await ensure_schema(driver, ontology, config) == ("graph_document_id",)


async def test_fulltext_property_order_does_not_matter(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    item = objects_by_name(ontology)["graph_spec_node_text"]
    assert item.properties == ("name", "statement_text")
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    driver.add_index(item, name="text_reversed", properties=["statement_text", "name"])
    assert await ensure_schema(driver, ontology, config) == ()


async def test_range_and_unique_property_order_matters(ontology: Ontology, config: LLMConfig) -> None:
    """A composite index on ``(identity_key, group_id)`` cannot serve ``group_id`` prefix seeks."""

    driver = SchemaDriver(ontology)
    driver.add_all()
    by_name = objects_by_name(ontology)
    range_item, unique_item = by_name["graph_spec_node_identity_key"], by_name["graph_fragment_id"]
    driver.indexes = [row for row in driver.indexes if row["name"] not in {range_item.name, unique_item.name}]
    driver.constraints = [row for row in driver.constraints if row["name"] != unique_item.name]
    driver.add_index(range_item, name="reversed_range", properties=["identity_key", "group_id"])
    driver.constraints.append(
        {**constraint_rows(unique_item, name="reversed_unique")[0], "properties": ["fragment_id", "group_id"]}
    )
    assert set(await ensure_schema(driver, ontology, config)) == {range_item.name, unique_item.name}


async def test_a_fulltext_index_does_not_stand_in_for_a_range_index(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    item = objects_by_name(ontology)["graph_spec_node_identity_key"]
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    driver.add_index(item, name="wrong_type", type_="FULLTEXT")
    assert await ensure_schema(driver, ontology, config) == (item.name,)


# Refusals: every check runs before the first create


async def test_a_graph_name_held_by_another_object_is_a_conflict(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    other = objects_by_name(ontology)["graph_fragment_text"]
    # The name of the identity-key range index is taken by an index on other properties.
    driver.add_index(other, name="graph_spec_node_identity_key")
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, ontology, config)
    assert raised.value.code == "schema_name_conflict"
    assert raised.value.fields["name"] == "graph_spec_node_identity_key"
    assert driver.created == []


async def test_a_graph_name_held_by_a_constraint_is_a_conflict(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    item = objects_by_name(ontology)["graph_spec_node_text"]
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    unrelated = objects_by_name(ontology)["graph_document_id"]
    driver.constraints.append({**constraint_rows(unrelated)[0], "name": item.name, "labelsOrTypes": ["Other"]})
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, ontology, config)
    assert (raised.value.code, raised.value.fields["name"]) == ("schema_name_conflict", item.name)
    assert driver.created == []


@pytest.mark.parametrize(
    ("dimension", "similarity"),
    [(DIM, "EUCLIDEAN"), (DIM * 2, "COSINE"), (DIM // 2, "cosine"), (None, "COSINE")],
)
@pytest.mark.parametrize("name", ["graph_spec_node_embedding", "graph_spec_node_name_embedding"])
async def test_a_mismatching_vector_index_raises_and_creates_nothing(
    ontology: Ontology, config: LLMConfig, name: str, dimension: int | None, similarity: str
) -> None:
    driver = SchemaDriver(ontology)
    item = objects_by_name(ontology)[name]
    driver.add_index(item, name="someone_elses_vectors", dimension=dimension, similarity=similarity)
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, ontology, config)
    error = raised.value
    assert error.code == "vector_index_mismatch"
    assert error.fields["index"] == "someone_elses_vectors"
    assert error.fields["expected_dimension"] == DIM
    assert error.fields["found_dimension"] == dimension
    assert error.fields["similarity"] == similarity.lower()
    assert driver.created == []


async def test_a_lower_case_cosine_vector_index_of_the_right_dimension_is_accepted(
    ontology: Ontology, config: LLMConfig
) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    item = objects_by_name(ontology)["graph_spec_node_embedding"]
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    driver.add_index(item, name="vectors", similarity="cosine")
    assert await ensure_schema(driver, ontology, config) == ()


async def test_an_index_still_populating_after_the_wait_raises(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.stays = POPULATING
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, ontology, config)
    assert raised.value.code == "schema_index_not_online"
    assert raised.value.fields["state"] == POPULATING
    assert raised.value.fields["index"] in {item.name for item in schema_objects(ontology)}


async def test_an_equivalent_existing_index_that_is_not_online_raises(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    item = objects_by_name(ontology)["graph_fragment_text"]
    driver.indexes = [row for row in driver.indexes if row["name"] != item.name]
    driver.add_index(item, name="fragment_text_failed", state="FAILED")
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, ontology, config)
    assert (raised.value.code, raised.value.fields["index"], raised.value.fields["state"]) == (
        "schema_index_not_online",
        "fragment_text_failed",
        "FAILED",
    )


async def test_an_unrelated_index_that_is_not_online_is_ignored(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology)
    driver.add_all()
    unrelated = SchemaObject("someone_else", "range", "NODE", "Elsewhere", ("x",))
    driver.add_index(unrelated, state=POPULATING)
    assert await ensure_schema(driver, ontology, config) == ()


async def test_old_neo4j_creates_nothing(ontology: Ontology, config: LLMConfig) -> None:
    driver = SchemaDriver(ontology, version="5.25.9")
    with pytest.raises(ConfigurationError) as raised:
        await ensure_schema(driver, ontology, config)
    assert raised.value.code == "neo4j_version_unsupported"
    assert driver.texts == [SERVER_COMPONENTS.text]


# Driver errors


SERVER_REPLY = "server said secret-xyz"


@pytest.mark.parametrize(
    "stage", ["show_constraints", "show_indexes", "create_graph_spec_node_spec_id", "await_indexes"]
)
@pytest.mark.parametrize(
    ("make_error", "expected", "code"),
    [
        (lambda: AuthError(SERVER_REPLY), ConfigurationError, "neo4j_auth_rejected"),
        (lambda: ServiceUnavailable(SERVER_REPLY), InfrastructureUnavailableError, "neo4j_unreachable"),
        (lambda: OSError(SERVER_REPLY), InfrastructureUnavailableError, "neo4j_unreachable"),
        (
            lambda: ClientError._hydrate_neo4j(
                code="Neo.ClientError.Schema.EquivalentSchemaRuleAlreadyExists", message=SERVER_REPLY
            ),
            ConfigurationError,
            "schema_setup_failed",
        ),
        (
            lambda: DatabaseError._hydrate_neo4j(
                code="Neo.DatabaseError.Schema.IndexCreationFailed", message=SERVER_REPLY
            ),
            ConfigurationError,
            "schema_setup_failed",
        ),
    ],
)
async def test_driver_errors_map_to_graph_errors(
    ontology: Ontology, config: LLMConfig, stage: str, make_error: Any, expected: type, *, code: str
) -> None:
    driver = SchemaDriver(ontology)
    error = make_error()
    driver.fail_on[stage] = error
    with pytest.raises(expected) as raised:
        await ensure_schema(driver, ontology, config)
    assert type(raised.value) is expected
    assert raised.value.code == code
    if code == "schema_setup_failed":
        assert raised.value.fields["query"] == stage
        assert raised.value.fields["neo4j_code"] == error.code
        assert raised.value.fields["error_type"] == type(error).__name__
    if code == "neo4j_unreachable":
        assert raised.value.fields["error_type"] == type(error).__name__
    assert "secret-xyz" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
