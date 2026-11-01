"""PostgreSQL schema manager: the tables, indexes and functions of the graph schema (design D5, D7).

The objects are defined in ``ddl/schema.sql``, one idempotent statement per object. ``schema_objects`` parses
each statement into a ``SchemaObject`` and checks that the DDL covers what the library needs: the unique
keys owned writes ``ON CONFLICT`` on, the edge key and traversal indexes, the vector column and the
full-text columns. A gap fails loudly with ``ConfigurationError("schema_ddl_incomplete")``.

``ensure_postgres_schema`` runs in one transaction under an advisory lock, so concurrent starts of several
processes do not collide. It checks (never creates) the ``vector`` extension, refuses a vector dimension
other than the one the schema was created for, creates what is missing and, where a project holds more than
``HNSW_MIN_VECTORS`` vectors of one model (dimension up to ``HNSW_MAX_DIMENSIONS``), a partial HNSW
``halfvec`` index for that model. It is idempotent.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Final, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from graph.core.errors import ConfigurationError
from graph.core.ontology import Ontology
from graph.core.queries import SERVER_COMPONENTS
from graph.core.query_files import render_query
from graph.core.store import STORE_VERSION_UNSUPPORTED, foreign_labels
from graph.stores.postgres.dialect import BACKEND, PostgresDialect, render_schema
from graph.stores.postgres.executor import DATABASE_ERRORS, map_error, sqlstate
from graph.stores.postgres.sqlfiles import load_ddl_sql, split_statements
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.stores.postgres.schema")

EXTENSION: Final = "vector"
MINIMUM_SERVER: Final = (15,)
MINIMUM_VECTOR: Final = (0, 8)
HNSW_MIN_VECTORS: Final = 2000
HNSW_MAX_DIMENSIONS: Final = 4000
META_SCHEMA_VERSION: Final = "schema_version"
META_EMBEDDING_DIM: Final = "embedding_dim"
META_TABLE: Final = "meta"
SCHEMA_VERSION: Final = "1"
SIMILARITY: Final = "cosine"
# Codes of the errors the schema manager raises beyond the executor's neutral ones.
SCHEMA_SETUP_FAILED: Final = "schema_setup_failed"
EXTENSION_MISSING: Final = "store_extension_missing"

ObjectKind = Literal["schema", "function", "table", "index"]
_VERSION: Final = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_HEAD: Final = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?P<unique>UNIQUE\s+)?(?P<kind>SCHEMA|FUNCTION|TABLE|INDEX)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?:graph\.)?(?P<name>\w+)",
    re.IGNORECASE,
)
_INDEX: Final = re.compile(
    r"CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+\w+\s+ON\s+graph\.(?P<table>\w+)\s*"
    r"\((?P<columns>.*?)\)(?:\s+WHERE\s+(?P<predicate>.*))?$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class SchemaObject:
    """One object the graph schema needs; ``statement`` is its DDL statement.

    For an index, ``table`` and ``columns`` (as written, whitespace collapsed) say what it covers and
    ``unique`` whether it is a key; ``predicate`` is its partial-index condition ("" for none).
    """

    name: str
    kind: ObjectKind
    statement: str
    table: str = ""
    columns: str = ""
    unique: bool = False
    predicate: str = ""


def _collapse(value: str) -> str:
    return " ".join(value.split())


def parse_ddl_statement(statement: str, *, position: int = 0) -> SchemaObject:
    """The ``SchemaObject`` one DDL statement creates; ``ConfigurationError("schema_ddl_unrecognised")``
    names the ``position`` of a statement that matches no known form."""

    head = _HEAD.match(statement)
    if head is None:
        raise ConfigurationError("schema_ddl_unrecognised", position=position)
    kind: ObjectKind = head["kind"].lower()  # type: ignore[assignment]
    if kind == "index":
        index = _INDEX.match(_collapse(statement))
        if index is None:
            raise ConfigurationError("schema_ddl_unrecognised", position=position)
        return SchemaObject(
            head["name"],
            "index",
            statement,
            table=index["table"],
            columns=_collapse(index["columns"]),
            unique=index["unique"] is not None,
            predicate=_collapse(index["predicate"] or ""),
        )
    return SchemaObject(head["name"], kind, statement)


@cache
def _core_statements() -> tuple[str, ...]:
    return split_statements(load_ddl_sql("schema.sql"))


# What the DDL must define: (table, columns, predicate) of each unique key and traversal index, the
# generated key and search columns of the node table.
_REQUIRED_INDEXES: Final = (
    ("node", "group_id, uuid", "kind = 'spec'", True),
    ("node", "group_id, spec_id", "kind = 'spec' AND spec_id IS NOT NULL", True),
    ("node", "group_id, document_id", "kind = 'document'", True),
    ("node", "group_id, fragment_id", "kind = 'fragment'", True),
    ("edge", "group_id, type, edge_key", "", True),
    ("edge", "source_id, type", "", False),
    ("edge", "target_id, type", "", False),
)
_FOREIGN_INDEX: Final = ("node", "group_id, (labels[1]), uuid", "kind = 'foreign'", True)
_REQUIRED_COLUMNS: Final = (
    "embedding vector",
    "embedding_model text GENERATED",
    "name_tsv tsvector GENERATED",
    "stmt_tsv tsvector GENERATED",
    "text_tsv tsvector GENERATED",
)


def schema_objects(ontology: Ontology, ddl: Sequence[str] | None = None) -> tuple[SchemaObject, ...]:
    """Every object of the graph schema, in DDL order.

    ``ddl`` None reads the shipped file (``ddl/schema.sql``); a sequence of statements (tests) replaces it.
    The DDL must define the unique keys of spec nodes (``uuid`` and ``spec_id``), documents, fragments and
    relationships (``type`` and ``edge_key``, which covers every relation the ontology declares), the key
    of foreign nodes when the ontology declares a foreign label, the two traversal indexes of the edge
    table and the vector and full-text columns. A gap raises ``ConfigurationError("schema_ddl_incomplete")``.
    """

    statements = tuple(_core_statements() if ddl is None else ddl)
    objects = tuple(parse_ddl_statement(statement, position=index) for index, statement in enumerate(statements))
    seen: set[str] = set()
    for item in objects:
        if item.name in seen:
            raise ConfigurationError("schema_ddl_duplicate_name", name=item.name)
        seen.add(item.name)
    indexes = {(item.table, item.columns, item.predicate, item.unique) for item in objects if item.kind == "index"}
    required = list(_REQUIRED_INDEXES)
    if foreign_labels(ontology):
        required.append(_FOREIGN_INDEX)
    for table, columns, predicate, unique in required:
        if (table, columns, _collapse(predicate), unique) not in indexes:
            raise ConfigurationError(
                "schema_ddl_incomplete",
                missing=1,
                kind="unique" if unique else "range",
                entity=table,
                properties=columns,
            )
    node_tables = [_collapse(item.statement) for item in objects if item.kind == "table" and item.name == "node"]
    for column in _REQUIRED_COLUMNS:
        if not node_tables or column not in node_tables[0]:
            raise ConfigurationError("schema_ddl_incomplete", missing=1, kind="column", entity="node", properties=column)
    return objects


def _version(value: str) -> tuple[int, ...]:
    found = _VERSION.match(value)
    if found is None:
        return ()
    return tuple(int(part) for part in found.groups() if part is not None)


def _setup_error(error: BaseException, name: str) -> Exception:
    """The error of a statement of schema setup: connection, login and access failures as the executor
    maps them, anything else as ``schema_setup_failed`` naming the object."""

    mapped = map_error(error, name, write=True)
    if mapped.code != "store_write_failed":
        return mapped
    return ConfigurationError(
        SCHEMA_SETUP_FAILED,
        store=BACKEND,
        query=name,
        error_type=type(error).__name__,
        backend_code=sqlstate(error),
    )


async def _rows(connection: AsyncConnection, sql: str, **params: Any) -> list[dict[str, Any]]:
    result = await connection.execute(text(sql), params)
    if not result.returns_rows:
        return []
    return [dict(row) for row in result.mappings().all()]


async def _server_versions(connection: AsyncConnection, dialect: PostgresDialect) -> tuple[str, str]:
    """The server version and the vector extension version ('' when it is not installed)."""

    plan = dialect.plan(SERVER_COMPONENTS, {})
    rows = await _rows(connection, plan.statements[0].text)
    if not rows:
        raise ConfigurationError(STORE_VERSION_UNSUPPORTED, store=BACKEND)
    return str(rows[0]["versions"][0]).split()[0], str(rows[0]["vector_version"])


def _require_environment(server: str, vector: str) -> None:
    """Raise when the vector extension is missing (an administrator creates it once per database) or when the
    server or the extension is older than this adapter supports; nothing is created then."""

    if not vector:
        raise ConfigurationError(EXTENSION_MISSING, store=BACKEND, extension=EXTENSION)
    if _version(server) < MINIMUM_SERVER:
        raise ConfigurationError(STORE_VERSION_UNSUPPORTED, store=BACKEND, found=server)
    if _version(vector) < MINIMUM_VECTOR:
        raise ConfigurationError(STORE_VERSION_UNSUPPORTED, store=BACKEND, extension=EXTENSION, found=vector)


async def _recorded_dimension(connection: AsyncConnection, schema: str) -> int | None:
    rows = await _rows(connection, render_schema(load_ddl_sql("read_meta.sql"), schema), key=META_EMBEDDING_DIM)
    return int(rows[0]["value"]) if rows else None


async def ensure_postgres_schema(
    engine: AsyncEngine,
    dialect: PostgresDialect,
    ontology: Ontology,
    embedding_dim: int,
    *,
    ddl: Sequence[str] | None = None,
) -> tuple[tuple[str, ...], str]:
    """Create the missing objects of the graph schema; return their names and the server version.

    The objects come from ``schema_objects`` (``ddl`` None: the shipped file), which must cover
    ``ontology`` (else ``ConfigurationError("schema_ddl_incomplete")``, before anything reaches the
    server). Everything runs in one transaction under the schema advisory lock. Raises
    ``ConfigurationError`` when the ``vector`` extension is missing (``store_extension_missing``), when the
    server or the extension is too old, or when the schema was created for another vector dimension
    (``vector_index_mismatch``); all checks run before the first create.
    """

    wanted = schema_objects(ontology, ddl)
    schema = dialect.schema
    created: list[str] = []
    server = ""
    try:
        async with engine.connect() as connection, connection.begin():
            server, vector = await _server_versions(connection, dialect)
            _require_environment(server, vector)
            await _rows(connection, load_ddl_sql("lock.sql"))
            present = {
                row["name"] for row in await _rows(connection, load_ddl_sql("catalog.sql"), schema=schema)
            }
            if META_TABLE in present:
                recorded = await _recorded_dimension(connection, schema)
                if recorded is not None and recorded != embedding_dim:
                    raise ConfigurationError(
                        "vector_index_mismatch",
                        index=META_EMBEDDING_DIM,
                        found_dimension=recorded,
                        expected_dimension=embedding_dim,
                        similarity=SIMILARITY,
                    )
            for item in wanted:
                if (schema if item.kind == "schema" else item.name) in present:
                    continue
                await connection.exec_driver_sql(render_schema(item.statement, schema))
                created.append(item.name)
            for key, value in ((META_SCHEMA_VERSION, SCHEMA_VERSION), (META_EMBEDDING_DIM, str(embedding_dim))):
                await _rows(connection, render_schema(load_ddl_sql("write_meta.sql"), schema), key=key, value=value)
            created.extend(await _ensure_hnsw(connection, schema, embedding_dim))
    except ConfigurationError:
        raise
    except DATABASE_ERRORS as error:
        raise _setup_error(error, "ensure_schema") from None
    log_event(
        logger,
        INFO,
        "graph.schema.ensured",
        objects_created=len(created),
        objects_existing=len(wanted) - len(created),
        server_version=server,
    )
    return tuple(created), server



async def _ensure_hnsw(connection: AsyncConnection, schema: str, embedding_dim: int) -> list[str]:
    """Create the HNSW index of each embedding model of ``embedding_dim`` dimensions that a project holds
    more than ``HNSW_MIN_VECTORS`` vectors of (dimension up to ``HNSW_MAX_DIMENSIONS``); return the names."""

    if embedding_dim > HNSW_MAX_DIMENSIONS:
        return []
    candidates = await _rows(
        connection,
        render_schema(load_ddl_sql("hnsw_candidates.sql"), schema),
        threshold=HNSW_MIN_VECTORS,
    )
    created: list[str] = []
    for row in candidates:
        if row["dim"] != embedding_dim:
            continue
        name = f"node_emb_hnsw_{row['model_hash']}"
        present = await _rows(connection, render_schema(load_ddl_sql("catalog.sql"), schema), schema=schema)
        if any(item["name"] == name for item in present):
            continue
        statement = render_query(
            load_ddl_sql("hnsw_index.sql"),
            index=row["model_hash"],
            dim=str(int(row["dim"])),
            model=str(row["model_literal"]),
        )
        await connection.exec_driver_sql(render_schema(statement, schema))
        created.append(name)
    return created
