"""Offline tests of the ArcadeDB schema: coverage, mapping of the canonical DDL, idempotence, refusals.

The server is a fake that remembers what it was told: Cypher DDL goes to a fake Bolt driver, SQL to a fake
admin client; both fill one table of indexes, which the next ``ensure_schema`` run reads back.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from graph.core.errors import ConfigurationError
from graph.core.queries import CORE_DDL
from graph.stores._bolt.schema import parse_ddl_statement
from graph.stores.arcadedb.schema import (
    SPEC_ID_INDEX,
    SPEC_KEY_INDEX,
    ArcadeObject,
    check_names,
    ensure_arcadedb_schema,
    schema_objects,
)
from graph_support import load_gap_ontology, shipped_ontology

DIM = 64


class FakeServer:
    """The state of one ArcadeDB database as far as the schema code looks: its indexes."""

    def __init__(self, version: tuple[int, int, int] = (26, 10, 2)) -> None:
        self.version = version
        self.indexes: list[dict[str, Any]] = []
        self.commands: list[str] = []
        self.database_created = False
        self.exists = False
        self.sql_error: str | None = None

    # the admin client
    async def require_version(self) -> tuple[int, int, int]:
        if self.version < (26, 10, 0):
            raise ConfigurationError("store_version_unsupported", store="arcadedb")
        return self.version

    async def ensure_database(self, name: str) -> bool:
        self.commands.append(f"database {name}")
        created = not self.exists
        self.exists = True
        self.database_created = self.database_created or created
        return created

    async def types(self, _database: str) -> list[dict[str, Any]]:
        by_type: dict[str, list[dict[str, Any]]] = {}
        for index in self.indexes:
            by_type.setdefault(index["typeName"], []).append(index)
        return [{"name": name, "indexes": indexes} for name, indexes in by_type.items()]

    async def sql(self, _database: str, command: str) -> list[dict[str, Any]]:
        self.commands.append(command)
        if self.sql_error and self.sql_error in command:
            raise ConfigurationError("schema_setup_failed", store="arcadedb", status=400)
        found = re.fullmatch(r"CREATE INDEX IF NOT EXISTS ON (\w+) \(([^)]*)\) (.*)", command)
        if found:
            label, columns, tail = found.groups()
            properties = [c.strip() for c in columns.split(",")]
            if tail.startswith("LSM_VECTOR"):
                dimension = int(re.search(r'"dimensions": (\d+)', tail).group(1))  # type: ignore[union-attr]
                self._add(label, properties, "LSM_VECTOR", dimensions=dimension, scoring="distance_lower_is_better:COSINE")
            elif tail == "FULL_TEXT":
                self._add(label, properties, "FULL_TEXT")
            else:
                self._add(label, properties, "LSM_TREE", unique=True)
        return []

    def _add(self, label: str, properties: list[str], kind: str, *, unique: bool = False, **more: Any) -> None:
        name = more.pop("name", f"{label}[{','.join(properties)}]")
        if all(i["name"] != name for i in self.indexes):
            self.indexes.append(
                {"name": name, "typeName": label, "type": kind, "unique": unique, "properties": properties, **more}
            )

    # the Bolt driver
    async def execute_query(self, text: str, **_kwargs: Any) -> Any:
        self.commands.append(text)
        item = parse_ddl_statement(text)
        unique = item.kind == "unique"
        name = f"{item.label}[{','.join(item.properties)}]" if unique else item.name
        self._add(item.label, list(item.properties), "LSM_TREE", unique=unique, name=name)
        return SimpleNamespace(records=[])


def ensure(server: FakeServer, ontology: Any = None, dim: int = DIM, **kwargs: Any):
    return ensure_arcadedb_schema(server, server, "graphdb", ontology or shipped_ontology(), dim, **kwargs)


# the objects


def test_each_canonical_object_maps_to_what_arcadedb_supports() -> None:
    objects = {item.name: item for item in schema_objects(shipped_ontology())}
    assert len(objects) == len(CORE_DDL) + 1  # the spec_id uniqueness is two objects
    spec_id = objects[SPEC_ID_INDEX]
    assert (spec_id.kind, spec_id.properties) == ("range", ("group_id", "spec_id"))
    assert spec_id.statement == (
        "CREATE INDEX graph_spec_node_spec_id IF NOT EXISTS FOR (n:SpecNode) ON (n.group_id, n.spec_id)"
    )
    key = objects[SPEC_KEY_INDEX]
    assert (key.kind, key.label, key.properties, key.statement) == ("spec_key", "SpecNode", ("spec_key",), "")
    for name in ("graph_spec_node_embedding", "graph_spec_node_name_embedding"):
        assert objects[name].kind == "vector" and objects[name].statement == ""
    for name in ("graph_spec_node_text", "graph_fragment_text"):
        assert objects[name].kind == "fulltext" and objects[name].statement == ""
    for name in ("graph_spec_node_uuid", "graph_document_id", "graph_fragment_id", "graph_edge_key_part_of"):
        assert objects[name].statement.startswith("CREATE ")
    assert "graph_spec_node_spec_key" not in {item.name for item in schema_objects(shipped_ontology()) if item.statement}


def test_a_ddl_that_does_not_cover_the_ontology_is_refused_before_anything_else() -> None:
    ddl = [statement for statement in CORE_DDL if "graph_edge_key_part_of" not in statement]
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(shipped_ontology(), ddl)
    assert raised.value.code == "schema_ddl_incomplete"


def test_a_foreign_label_needs_its_uuid_constraint_like_on_neo4j(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(load_gap_ontology(tmp_path))
    assert raised.value.code == "schema_ddl_incomplete"


def test_names_that_differ_only_by_case_are_refused() -> None:
    ontology = SimpleNamespace(labels=("Module", "module"), relations=("PART_OF",))
    with pytest.raises(ConfigurationError) as raised:
        check_names(ontology)  # type: ignore[arg-type]
    assert raised.value.code == "schema_name_case_collision"
    assert raised.value.fields == {"first": "Module", "second": "module"}


def test_a_label_and_a_relation_share_one_name_space() -> None:
    ontology = SimpleNamespace(labels=("Flags",), relations=("FLAGS",))
    with pytest.raises(ConfigurationError):
        check_names(ontology)  # type: ignore[arg-type]


def test_the_shipped_ontology_has_no_case_collision() -> None:
    check_names(shipped_ontology())


# ensure_schema


async def test_a_first_run_creates_everything_and_reports_the_library_names() -> None:
    server = FakeServer()
    created, version = await ensure(server)
    wanted = [item.name for item in schema_objects(shipped_ontology())]
    assert list(created) == wanted and version == (26, 10, 2)
    assert server.database_created


async def test_the_second_run_creates_nothing() -> None:
    server = FakeServer()
    await ensure(server)
    server.commands.clear()
    created, _ = await ensure(server)
    assert created == ()
    assert server.commands == ["database graphdb"]


async def test_the_vector_index_is_created_by_sql_with_the_dimension_as_a_literal() -> None:
    server = FakeServer()
    await ensure(server, dim=1536)
    vector = [c for c in server.commands if "LSM_VECTOR" in c]
    assert vector == [
        'CREATE INDEX IF NOT EXISTS ON SpecNode (embedding) LSM_VECTOR METADATA {"dimensions": 1536, "similarity": "COSINE"}',
        'CREATE INDEX IF NOT EXISTS ON SpecNode (name_embedding) LSM_VECTOR METADATA {"dimensions": 1536, "similarity": "COSINE"}',
    ]
    position = server.commands.index(vector[0])
    assert server.commands[position - 2 : position] == [
        "CREATE VERTEX TYPE SpecNode IF NOT EXISTS",
        "CREATE PROPERTY SpecNode.embedding IF NOT EXISTS ARRAY_OF_FLOATS",
    ]


async def test_fulltext_and_spec_key_are_created_by_sql() -> None:
    server = FakeServer()
    await ensure(server)
    commands = server.commands
    assert "CREATE INDEX IF NOT EXISTS ON SpecNode (name, statement_text) FULL_TEXT" in commands
    assert "CREATE PROPERTY SpecNode.statement_text IF NOT EXISTS STRING" in commands
    assert "CREATE INDEX IF NOT EXISTS ON Fragment (text) FULL_TEXT" in commands
    assert "CREATE PROPERTY SpecNode.spec_key IF NOT EXISTS STRING" in commands
    assert "CREATE INDEX IF NOT EXISTS ON SpecNode (spec_key) UNIQUE NULL_STRATEGY SKIP" in commands


async def test_constraints_and_range_indexes_are_created_by_their_canonical_cypher() -> None:
    server = FakeServer()
    await ensure(server)
    cypher = [c for c in server.commands if c.startswith("CREATE ") and not c.startswith("CREATE INDEX IF NOT")]
    assert "CREATE CONSTRAINT graph_spec_node_uuid IF NOT EXISTS FOR (n:SpecNode) REQUIRE (n.group_id, n.uuid) IS UNIQUE" in cypher
    assert not any("graph_spec_node_spec_id IF NOT EXISTS FOR (n:SpecNode) REQUIRE" in c for c in cypher)
    assert any(c.startswith("CREATE INDEX graph_edge_key_part_of") for c in cypher)
    assert not any("VECTOR INDEX" in c or "FULLTEXT INDEX" in c for c in cypher)


async def test_a_vector_index_of_another_dimension_is_refused_before_any_create() -> None:
    server = FakeServer()
    await ensure(server, dim=32)
    server.commands.clear()
    with pytest.raises(ConfigurationError) as raised:
        await ensure(server, dim=64)
    assert raised.value.code == "vector_index_mismatch"
    assert raised.value.fields["found_dimension"] == 32 and raised.value.fields["expected_dimension"] == 64
    assert not any(c.startswith("CREATE") for c in server.commands)


async def test_a_vector_index_of_another_similarity_is_refused() -> None:
    server = FakeServer()
    await ensure(server)
    for index in server.indexes:
        if index["type"] == "LSM_VECTOR":
            index["scoring"] = "distance_lower_is_better:EUCLIDEAN"
    with pytest.raises(ConfigurationError) as raised:
        await ensure(server)
    assert raised.value.code == "vector_index_mismatch" and raised.value.fields["similarity"] == "euclidean"


async def test_an_old_server_creates_nothing() -> None:
    server = FakeServer(version=(25, 9, 1))
    with pytest.raises(ConfigurationError) as raised:
        await ensure(server)
    assert raised.value.code == "store_version_unsupported"
    assert server.commands == [] and not server.database_created


async def test_an_incomplete_ddl_or_a_name_collision_touches_no_server() -> None:
    server = FakeServer()
    with pytest.raises(ConfigurationError):
        await ensure(server, ddl=list(CORE_DDL[:3]))
    assert server.commands == []


async def test_a_graph_name_taken_by_a_different_index_is_a_conflict() -> None:
    server = FakeServer()
    server._add("SpecNode", ["name"], "LSM_TREE", name="graph_spec_node_identity_key")
    with pytest.raises(ConfigurationError) as raised:
        await ensure(server)
    assert raised.value.code == "schema_name_conflict"
    assert raised.value.fields == {"name": "graph_spec_node_identity_key"}


async def test_a_refused_sql_create_stops_the_run_and_keeps_what_was_created() -> None:
    server = FakeServer()
    server.sql_error = "name_embedding"
    with pytest.raises(ConfigurationError) as raised:
        await ensure(server)
    assert raised.value.code == "schema_setup_failed"
    assert any(i["properties"] == ["embedding"] for i in server.indexes)


async def test_an_object_missing_after_the_creates_is_reported() -> None:
    server = FakeServer()
    original = server.execute_query

    async def swallow(text: str, **kwargs: Any) -> Any:
        if "graph_edge_key_relates_to" in text:
            server.commands.append(text)
            return SimpleNamespace(records=[])
        return await original(text, **kwargs)

    server.execute_query = swallow  # type: ignore[method-assign]
    with pytest.raises(ConfigurationError) as raised:
        await ensure(server)
    assert raised.value.code == "schema_index_not_online" and raised.value.fields["state"] == "MISSING"


def test_an_object_is_a_frozen_value() -> None:
    item: ArcadeObject = schema_objects(shipped_ontology())[0]
    with pytest.raises(AttributeError):
        item.name = "x"  # type: ignore[misc]
    assert isinstance(item.properties, tuple) and isinstance(CORE_DDL, Sequence)
