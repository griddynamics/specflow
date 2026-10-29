"""Schema setup: the ``graph_*`` constraints and indexes of docs/graph/data-model.md.

The objects are defined in the static DDL files under ``graph/core/ddl/`` (``CORE_DDL``), one
idempotent statement per object. ``schema_objects`` parses each statement into a ``SchemaObject`` and
checks that the DDL covers the ontology it runs with: the core keys and search indexes, a
``(group_id, edge_key)`` range index per declared relation and a ``(group_id, uuid)`` uniqueness
constraint per foreign label. The ontology never renders a statement; a gap fails loudly with
``ConfigurationError("schema_ddl_incomplete")``.

``ensure_schema`` checks the Neo4j version, then runs the statement of every missing object and waits
until the indexes are online. It is idempotent: an object that already exists, under its ``graph_*``
name or as an equivalent object under another name, is left alone. Schema setup is not project-scoped
(rule I3), so it runs on the driver directly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Final, Literal

from neo4j.exceptions import AuthError, DriverError, Neo4jError, ServiceUnavailable, SessionExpired

from graph.core.config import LLMConfig
from graph.core.db import require_version
from graph.core.errors import ConfigurationError, ConformanceError, InfrastructureUnavailableError
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.queries import (
    AWAIT_INDEXES,
    CORE_DDL,
    EMBEDDING,
    FOREIGN_KEY,
    NAME_EMBEDDING,
    SAFE_NAME,
    SHOW_CONSTRAINTS,
    SHOW_INDEXES,
    SPEC_NODE,
    DdlStatement,
    Query,
    ddl_query,
)
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.core.schema")

INDEX_ONLINE_TIMEOUT_S: Final = 300
DOCUMENT: Final = "Document"
FRAGMENT: Final = "Fragment"
GROUP_KEY: Final = "group_id"
EDGE_KEY: Final = "edge_key"
# Index names the reader searches with.
SPEC_NODE_EMBEDDING_INDEX: Final = "graph_spec_node_embedding"
SPEC_NODE_TEXT_INDEX: Final = "graph_spec_node_text"
FRAGMENT_TEXT_INDEX: Final = "graph_fragment_text"
_UNIQUE_TYPES: Final = frozenset({"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS", "NODE_KEY"})
_ONLINE: Final = "ONLINE"

ObjectKind = Literal["unique", "range", "vector", "fulltext"]
EntityType = Literal["NODE", "RELATIONSHIP"]


@dataclass(frozen=True)
class SchemaObject:
    """One constraint or index the data model needs; ``statement`` is its DDL statement."""

    name: str
    kind: ObjectKind
    entity: EntityType
    label: str
    properties: tuple[str, ...]
    statement: str = ""

    def query(self) -> Query:
        """The DDL statement that creates this object (``ConformanceError`` for one without DDL)."""

        if not self.statement:
            raise ConformanceError("schema_object_without_ddl", name=self.name)
        return ddl_query(DdlStatement(self.name, self.statement))


# The statement forms of the DDL files. Each statement must match one form in full: a ``graph_*``
# name, ``IF NOT EXISTS``, one node or relationship pattern and its property list. A vector index
# takes its dimension from the parameter ``$dimension`` and uses cosine similarity.
_IDENT: Final = r"[A-Za-z][A-Za-z0-9_]{0,63}"
_NODE_TARGET: Final = rf"\((?P<var>[a-z]\w*):(?P<label>{_IDENT})\)"
_RELATIONSHIP_TARGET: Final = rf"\(\)-\[(?P<var>[a-z]\w*):(?P<label>{_IDENT})\]-\(\)"
_PROPERTY_LIST: Final = r"(?P<properties>[^()\[\]]+)"
_VECTOR_OPTIONS: Final = (
    r"OPTIONS\s+\{indexConfig:\s*\{`vector\.dimensions`:\s*\$dimension,"
    r"\s*`vector\.similarity_function`:\s*'cosine'\}\}"
)


def _form(verb: str, target: str, tail: str) -> re.Pattern[str]:
    return re.compile(rf"CREATE\s+{verb}\s+(?P<name>graph_[a-z0-9_]+)\s+IF\s+NOT\s+EXISTS\s+FOR\s+{target}\s+{tail}")


_FORMS: Final[tuple[tuple[ObjectKind, EntityType, re.Pattern[str]], ...]] = (
    ("unique", "NODE", _form("CONSTRAINT", _NODE_TARGET, rf"REQUIRE\s+\({_PROPERTY_LIST}\)\s+IS\s+UNIQUE")),
    ("range", "NODE", _form(r"(?:RANGE\s+)?INDEX", _NODE_TARGET, rf"ON\s+\({_PROPERTY_LIST}\)")),
    ("range", "RELATIONSHIP", _form(r"(?:RANGE\s+)?INDEX", _RELATIONSHIP_TARGET, rf"ON\s+\({_PROPERTY_LIST}\)")),
    ("vector", "NODE", _form(r"VECTOR\s+INDEX", _NODE_TARGET, rf"ON\s+\({_PROPERTY_LIST}\)\s+{_VECTOR_OPTIONS}")),
    ("fulltext", "NODE", _form(r"FULLTEXT\s+INDEX", _NODE_TARGET, rf"ON\s+EACH\s+\[{_PROPERTY_LIST}\]")),
)

# What the DDL must define whatever the ontology: the keys owned writes MERGE on, the identity-key
# index, the search vectors and fulltext texts, as (kind, entity, label, properties).
_CORE_OBJECTS: Final = (
    ("unique", "NODE", SPEC_NODE, (GROUP_KEY, "spec_id")),
    ("unique", "NODE", SPEC_NODE, (GROUP_KEY, "uuid")),
    ("unique", "NODE", DOCUMENT, (GROUP_KEY, "document_id")),
    ("unique", "NODE", FRAGMENT, (GROUP_KEY, "fragment_id")),
    ("range", "NODE", SPEC_NODE, (GROUP_KEY, "identity_key")),
    ("vector", "NODE", SPEC_NODE, (EMBEDDING,)),
    ("vector", "NODE", SPEC_NODE, (NAME_EMBEDDING,)),
    ("fulltext", "NODE", SPEC_NODE, ("name", "statement_text")),
    ("fulltext", "NODE", FRAGMENT, ("text",)),
)
# The indexes the reader names in its searches.
_SEARCH_INDEXES: Final = (SPEC_NODE_EMBEDDING_INDEX, SPEC_NODE_TEXT_INDEX, FRAGMENT_TEXT_INDEX)


def parse_ddl_statement(statement: str, *, position: int = 0) -> SchemaObject:
    """The ``SchemaObject`` one DDL statement creates; ``ConfigurationError("schema_ddl_unrecognised")``
    names the ``position`` of a statement that matches no known form."""

    for kind, entity, form in _FORMS:
        found = form.fullmatch(statement)
        if found is None:
            continue
        properties = _ddl_properties(found["var"], found["properties"])
        if properties and (kind != "vector" or len(properties) == 1):
            return SchemaObject(found["name"], kind, entity, found["label"], properties, statement)
    raise ConfigurationError("schema_ddl_unrecognised", position=position)


def _ddl_properties(var: str, listed: str) -> tuple[str, ...]:
    """The property names of ``var.a, var.b``; empty when an entry is not ``var.<safe name>``."""

    names: list[str] = []
    for entry in listed.split(","):
        owner, dot, name = entry.strip().partition(".")
        if owner != var or not dot or SAFE_NAME.fullmatch(name) is None:
            return ()
        names.append(name)
    return tuple(names)


@cache
def _ddl_objects(statements: tuple[str, ...]) -> tuple[SchemaObject, ...]:
    objects = tuple(parse_ddl_statement(text, position=index) for index, text in enumerate(statements))
    seen: set[str] = set()
    for item in objects:
        if item.name in seen:
            raise ConfigurationError("schema_ddl_duplicate_name", name=item.name)
        seen.add(item.name)
    return objects


def schema_objects(ontology: Ontology, ddl: Sequence[str] | None = None) -> tuple[SchemaObject, ...]:
    """Every ``graph_*`` object of data-model.md "Constraints and indexes", in DDL order.

    ``ddl`` None reads the shipped DDL files (``CORE_DDL``); a sequence of statements (tests, an
    ontology with submodule files) replaces them. The DDL must define the core objects, a
    ``(group_id, edge_key)`` range index for every declared relation (owned writes of any owner find a
    relationship by its key with an index seek) and a ``(group_id, uuid)`` uniqueness constraint for
    every foreign label (a declared label of another owner than build, from a submodule ontology file;
    the key its owned upsert MERGEs on). A missing one raises ``ConfigurationError("schema_ddl_incomplete")``.
    """

    objects = _ddl_objects(tuple(CORE_DDL if ddl is None else ddl))
    defined = {(item.kind, item.entity, item.label, item.properties) for item in objects}
    required = [
        *_CORE_OBJECTS,
        *(("range", "RELATIONSHIP", relation, (GROUP_KEY, EDGE_KEY)) for relation in sorted(ontology.relations)),
        *(("unique", "NODE", label, (GROUP_KEY, FOREIGN_KEY)) for label in foreign_labels(ontology)),
    ]
    missing = [item for item in required if item not in defined]
    if missing:
        kind, entity, label, properties = missing[0]
        raise ConfigurationError(
            "schema_ddl_incomplete",
            missing=len(missing),
            kind=kind,
            entity=entity,
            label=label,
            properties=", ".join(properties),
        )
    names = {item.name for item in objects}
    for name in _SEARCH_INDEXES:
        if name not in names:
            raise ConfigurationError("schema_ddl_incomplete", missing=1, name=name)
    return objects


def foreign_labels(ontology: Ontology) -> tuple[str, ...]:
    """The declared labels owned by another owner than build, sorted (stored without ``SpecNode``)."""

    return tuple(label for label in ontology.labels if ontology.owner_of(label) != BUILD_OWNER)


async def ensure_schema(
    driver: Any,
    ontology: Ontology,
    llm_config: LLMConfig,
    database: str | None = None,
    *,
    ddl: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Create the missing ``graph_*`` constraints and indexes; return the names created.

    The objects and their statements come from the DDL (``schema_objects``; ``ddl`` None: the shipped
    files), which must cover ``ontology`` (else ``ConfigurationError("schema_ddl_incomplete")``, before
    anything reaches Neo4j). Raises ``ConfigurationError`` when Neo4j is older than 5.26 (nothing is
    created), when an existing vector index on a ``SpecNode`` embedding has another dimension or
    similarity, or when a ``graph_*`` name is taken by a different object. All checks run before the
    first create. A create the server refuses (for example a uniqueness constraint over nodes that
    already hold duplicate keys) raises ``ConfigurationError("schema_setup_failed")`` naming the query;
    the objects created before it stay.
    """

    wanted = schema_objects(ontology, ddl)
    version = await require_version(driver, database=database)
    dimension = llm_config.embedding_dim
    constraints = await _run(driver, SHOW_CONSTRAINTS, database)
    indexes = await _run(driver, SHOW_INDEXES, database)
    missing = [item for item in wanted if not _present(item, constraints, indexes, dimension)]
    taken = {row["name"] for row in [*constraints, *indexes]}
    for item in missing:
        if item.name in taken:
            raise ConfigurationError("schema_name_conflict", name=item.name)
    created: list[str] = []
    for item in missing:
        await _run(driver, item.query(), database, dimension=dimension)
        created.append(item.name)
    await _run(driver, AWAIT_INDEXES, database, timeout_s=INDEX_ONLINE_TIMEOUT_S)
    _require_online(wanted, await _run(driver, SHOW_INDEXES, database))
    log_event(
        logger,
        INFO,
        "graph.schema.ensured",
        objects_created=len(created),
        objects_existing=len(wanted) - len(created),
        server_version=".".join(map(str, version)),
    )
    return tuple(created)


def _present(
    item: SchemaObject,
    constraints: Sequence[Mapping[str, Any]],
    indexes: Sequence[Mapping[str, Any]],
    dimension: int,
) -> bool:
    """Whether ``item`` or an equivalent object exists; a mismatching vector index raises."""

    if item.kind == "unique":
        return any(row["type"] in _UNIQUE_TYPES and _same_schema(item, row, ordered=True) for row in constraints)
    if item.kind == "range":
        return any(row["type"] == "RANGE" and _same_schema(item, row, ordered=True) for row in indexes)
    if item.kind == "fulltext":
        return any(row["type"] == "FULLTEXT" and _same_schema(item, row, ordered=False) for row in indexes)
    for row in indexes:
        if row["type"] == "VECTOR" and _same_schema(item, row, ordered=True):
            config = (row.get("options") or {}).get("indexConfig") or {}
            found = config.get("vector.dimensions")
            similarity = str(config.get("vector.similarity_function", "")).lower()
            if found != dimension or similarity != "cosine":
                raise ConfigurationError(
                    "vector_index_mismatch",
                    index=row["name"],
                    found_dimension=found,
                    expected_dimension=dimension,
                    similarity=similarity,
                )
            return True
    return False


def _same_schema(item: SchemaObject, row: Mapping[str, Any], *, ordered: bool) -> bool:
    labels = tuple(row.get("labelsOrTypes") or ())
    properties = tuple(row.get("properties") or ())
    if row.get("entityType") != item.entity or labels != (item.label,):
        return False
    if ordered:
        return properties == item.properties
    return sorted(properties) == sorted(item.properties)


def _require_online(wanted: Sequence[SchemaObject], indexes: Sequence[Mapping[str, Any]]) -> None:
    """Raise when an index the data model needs is not online after waiting."""

    for row in indexes:
        if row.get("state") == _ONLINE:
            continue
        if any(_same_schema(item, row, ordered=False) for item in wanted):
            raise ConfigurationError("schema_index_not_online", index=row["name"], state=row["state"])


async def _run(driver: Any, query: Query, database: str | None, **params: Any) -> list[dict[str, Any]]:
    try:
        result = await driver.execute_query(query.text, params, database_=database)
    except AuthError:
        raise ConfigurationError("neo4j_auth_rejected") from None
    except (ServiceUnavailable, SessionExpired, OSError) as error:
        raise InfrastructureUnavailableError("neo4j_unreachable", error_type=type(error).__name__) from None
    except (Neo4jError, DriverError) as error:
        code = getattr(error, "code", None)
        raise ConfigurationError(
            "schema_setup_failed",
            query=query.name,
            error_type=type(error).__name__,
            neo4j_code=code if isinstance(code, str) else None,
        ) from None
    return [dict(record) for record in result.records]
