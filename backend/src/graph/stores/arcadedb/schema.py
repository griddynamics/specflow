"""ArcadeDB schema: the library's ``graph_*`` objects realised on ArcadeDB 26.10 (design D5, D8).

The objects are those of the canonical DDL files (``graph/core/ddl/``, the one DDL home of the Bolt
stores); ``schema_objects`` checks that they cover the ontology (the shared check) and maps each to what
ArcadeDB can do:

- a uniqueness constraint or a range index: the canonical Cypher statement, run over Bolt;
- ``(group_id, spec_id)`` uniqueness: ArcadeDB indexes a null key of a composite unique index, so two
  nodes without a ``spec_id`` collide. It is realised as a plain composite index (the lookup) and a
  derived single-property ``spec_key`` (``group_id + ':' + spec_id``) with a unique index that skips
  null (``NULL_STRATEGY SKIP``); every ArcadeDB statement that sets ``spec_id`` sets ``spec_key``;
- vector and fulltext indexes: SQL over the HTTP admin API (Cypher ``CREATE VECTOR INDEX`` and
  ``CREATE FULLTEXT INDEX`` are rejected). SQL needs the type and the property first; the dimension is a
  literal; the server names the index ``Type[prop,...]`` (``names.py`` maps the library's names).

``ensure_arcadedb_schema`` reads the existing indexes from the HTTP schema (the one place that also gives
a vector index's dimension and similarity), creates what is missing and checks it is there. It is
idempotent and refuses a vector index of another dimension or similarity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from neo4j.exceptions import AuthError, DriverError, Neo4jError, ServiceUnavailable, SessionExpired

from graph.core.errors import ConfigurationError, InfrastructureUnavailableError
from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.queries import SPEC_NODE, safe_name
from graph.core.store import STORE_AUTH_REJECTED, STORE_UNREACHABLE
from graph.stores._bolt.schema import SchemaObject
from graph.stores._bolt.schema import schema_objects as canonical_objects
from graph.stores.arcadedb.admin import AdminClient
from graph.stores.arcadedb.names import SERVER_NAMED_KINDS
from graph.stores.arcadedb.statements import cypher, sql
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.stores.arcadedb.schema")

BACKEND: Final = "arcadedb"
SPEC_KEY: Final = "spec_key"
SPEC_KEY_INDEX: Final = "graph_spec_node_spec_key"
SPEC_ID_INDEX: Final = "graph_spec_node_spec_id"
_SPEC_ID_STATEMENT: Final = cypher("ddl", "spec_id_index.cypher")
_VECTOR: Final = "LSM_VECTOR"
_FULLTEXT: Final = "FULL_TEXT"
_PROPERTY_TYPES: Final = {"vector": "ARRAY_OF_FLOATS", "fulltext": "STRING", "spec_key": "STRING"}

ObjectKind = Literal["unique", "range", "vector", "fulltext", "spec_key"]


@dataclass(frozen=True)
class ArcadeObject:
    """One schema object as ArcadeDB holds it. ``statement`` is its Cypher DDL for the kinds created over
    Bolt, empty for the kinds created by SQL. ``entity`` is NODE or RELATIONSHIP."""

    name: str
    kind: ObjectKind
    entity: str
    label: str
    properties: tuple[str, ...]
    statement: str = ""


def _map(item: SchemaObject) -> tuple[ArcadeObject, ...]:
    if item.kind == "unique" and item.properties[-1:] == ("spec_id",):
        return (
            ArcadeObject(item.name, "range", item.entity, item.label, item.properties, _SPEC_ID_STATEMENT),
            ArcadeObject(SPEC_KEY_INDEX, "spec_key", item.entity, item.label, (SPEC_KEY,)),
        )
    statement = "" if item.kind in SERVER_NAMED_KINDS else item.statement
    return (ArcadeObject(item.name, item.kind, item.entity, item.label, item.properties, statement),)


def schema_objects(ontology: Ontology, ddl: Sequence[str] | None = None) -> tuple[ArcadeObject, ...]:
    """The schema objects ArcadeDB needs for ``ontology``, in DDL order.

    The canonical DDL must cover the ontology (``ConfigurationError("schema_ddl_incomplete")``, the shared
    check); each of its objects then maps to the form ArcadeDB supports (module doc)."""

    return tuple(mapped for item in canonical_objects(ontology, ddl) for mapped in _map(item))


def check_names(ontology: Ontology) -> None:
    """Raise ``ConfigurationError("schema_name_case_collision")`` when two type names of the ontology differ
    only by case (ArcadeDB types, vertex and edge alike, share one name space, and names read as the same
    by a case-insensitive lookup must not be two types)."""

    seen: dict[str, str] = {}
    for name in (*ontology.labels, *ontology.relations, SPEC_NODE, "Document", "Fragment"):
        other = seen.setdefault(name.lower(), name)
        if other != name:
            raise ConfigurationError("schema_name_case_collision", first=other, second=name)


def spec_labels(ontology: Ontology) -> tuple[str, ...]:
    """The domain labels spec nodes carry (those build owns), sorted."""

    return tuple(sorted(label for label in ontology.labels if ontology.owner_of(label) == BUILD_OWNER))


def _has_composite(types: Sequence[Mapping[str, Any]], label: str) -> bool:
    """Whether the type of nodes labelled ``SpecNode`` and ``label`` exists (a type named ``A~B`` whose
    parents are the two labels)."""

    return any(set(entry.get("parentTypes", ())) == {SPEC_NODE, label} for entry in types)


def _indexes(types: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**index, "typeName": index.get("typeName", entry.get("name"))}
        for entry in types
        for index in entry.get("indexes", ())
    ]


def _find(item: ArcadeObject, indexes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The index that realises ``item``, if there is one."""

    for index in indexes:
        if index["typeName"] != item.label:
            continue
        properties = tuple(index.get("properties", ()))
        unique = bool(index.get("unique"))
        kind = index.get("type")
        if item.kind == "vector" and kind == _VECTOR and properties == item.properties:
            return index
        if item.kind == "fulltext" and kind == _FULLTEXT and sorted(properties) == sorted(item.properties):
            return index
        if item.kind in ("unique", "spec_key") and unique and properties == item.properties:
            return index
        if item.kind == "range" and not unique and kind not in (_VECTOR, _FULLTEXT) and properties == item.properties:
            return index
    return None


def _similarity(index: Mapping[str, Any]) -> str:
    scoring = str(index.get("scoring") or index.get("similarity") or "")
    return scoring.rsplit(":", 1)[-1].lower()


def _check_vector(item: ArcadeObject, index: Mapping[str, Any], dimension: int) -> None:
    found = index.get("dimensions")
    similarity = _similarity(index)
    if found != dimension or similarity != "cosine":
        raise ConfigurationError(
            "vector_index_mismatch",
            index=item.name,
            found_dimension=found,
            expected_dimension=dimension,
            similarity=similarity,
        )


def _sql(item: ArcadeObject, dimension: int) -> tuple[str, ...]:
    """The SQL commands that create ``item``: its type, its properties, then the index."""

    steps = [sql("create_vertex_type", type=item.label)]
    steps += [
        sql("create_property", type=item.label, property=name, kind=_PROPERTY_TYPES[item.kind])
        for name in item.properties
    ]
    names = {"type": item.label, "columns": ", ".join(item.properties)}
    if item.kind == "vector":
        steps.append(sql("create_index_vector", dimension=str(int(dimension)), **names))
    elif item.kind == "fulltext":
        steps.append(sql("create_index_fulltext", **names))
    else:
        steps.append(sql("create_index_unique", **names))
    return tuple(steps)


async def _cypher(driver: Any, database: str, name: str, statement: str, **params: Any) -> None:
    try:
        await driver.execute_query(statement, params, database_=database)
    except AuthError:
        raise ConfigurationError(STORE_AUTH_REJECTED, store=BACKEND) from None
    except (ServiceUnavailable, SessionExpired, OSError) as error:
        raise InfrastructureUnavailableError(
            STORE_UNREACHABLE, store=BACKEND, error_type=type(error).__name__
        ) from None
    except (Neo4jError, DriverError) as error:
        code = getattr(error, "code", None)
        raise ConfigurationError(
            "schema_setup_failed",
            store=BACKEND,
            query=f"create_{name}",
            error_type=type(error).__name__,
            backend_code=code if isinstance(code, str) else None,
        ) from None


async def ensure_arcadedb_schema(
    driver: Any,
    admin: AdminClient,
    database: str,
    ontology: Ontology,
    embedding_dim: int,
    *,
    ddl: Sequence[str] | None = None,
) -> tuple[tuple[str, ...], tuple[int, int, int]]:
    """Create the missing ``graph_*`` objects in ``database``; return the names created and the server version.

    Order: the ontology checks (nothing reaches the server when one fails), the version gate, the database
    (created when missing), the existing indexes, the vector dimension check, the creates (Cypher DDL over
    ``driver``, SQL over ``admin``), then a check that every object is there. A create the server refuses
    raises ``ConfigurationError("schema_setup_failed")``; the objects created before it stay.
    """

    wanted = schema_objects(ontology, ddl)
    check_names(ontology)
    version = await admin.require_version()
    await admin.ensure_database(database)
    indexes = _indexes(await admin.types(database))
    missing = []
    for item in wanted:
        found = _find(item, indexes)
        if found is None:
            if item.statement and any(index.get("name") == item.name for index in indexes):
                raise ConfigurationError("schema_name_conflict", name=item.name)
            missing.append(item)
        elif item.kind == "vector":
            _check_vector(item, found, embedding_dim)
    created: list[str] = []
    for item in missing:
        if item.statement:
            await _cypher(driver, database, item.name, item.statement)
        else:
            for command in _sql(item, embedding_dim):
                await admin.sql(database, command)
        created.append(item.name)
    composites = await _ensure_composite_types(driver, admin, database, spec_labels(ontology))
    await _cypher(
        driver, database, "placeholder_cleanup", cypher("ddl", "placeholder_cleanup.cypher"), group=PLACEHOLDER_GROUP
    )
    if created:
        _require_present(wanted, _indexes(await admin.types(database)), embedding_dim)
    log_event(
        logger,
        INFO,
        "graph.schema.ensured",
        objects_created=len(created),
        objects_existing=len(wanted) - len(created),
        composite_types_created=composites,
        server_version=".".join(map(str, version)),
    )
    return tuple(created), version


# A node with two labels is a record of the composite type ``A~B``, which ArcadeDB creates when the first
# such node is written; two transactions that do it at once fail the second one with a SemanticError that a
# retry does not cure (measured: 5 of 18 concurrent first writes). A placeholder node, created and deleted
# once per domain label, makes every type exist before a build writes.
PLACEHOLDER_GROUP: Final = "__schema__"


async def _ensure_composite_types(driver: Any, admin: AdminClient, database: str, labels: Sequence[str]) -> int:
    """Create the composite type of each of ``labels`` that is missing; return how many were created.

    A start that races another one (both found the type missing) re-reads the schema after a refusal and
    accepts the type when it is there."""

    created = 0
    for label in labels:
        if _has_composite(await admin.types(database), label):
            continue
        name, params = f"composite_{label}", {"group": PLACEHOLDER_GROUP, "uuid": f"__{label}__"}
        try:
            await _cypher(
                driver, database, name, cypher("ddl", "placeholder_create.cypher", label=safe_name(label)), **params
            )
            created += 1
        except ConfigurationError:
            if not _has_composite(await admin.types(database), label):
                raise
        await _cypher(
            driver, database, name, cypher("ddl", "placeholder_delete.cypher", label=safe_name(label)), **params
        )
    return created


def _require_present(wanted: Sequence[ArcadeObject], indexes: Sequence[Mapping[str, Any]], dimension: int) -> None:
    """Raise when an object the data model needs is not in the schema after the creates."""

    for item in wanted:
        found = _find(item, indexes)
        if found is None:
            raise ConfigurationError("schema_index_not_online", index=item.name, state="MISSING")
        if item.kind == "vector":
            _check_vector(item, found, dimension)
