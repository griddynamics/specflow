"""Core Cypher: every query text the core runs, as named ``Query`` constants (architecture.md I3).

The statements live in ``.cypher`` files under ``queries/`` next to this module and are loaded through
``load_core_query``; a query whose label or relationship type is only known at run time is a file with
``__NAME__`` tokens that its factory fills with validated names. The schema's constraints and indexes
are static DDL files under ``ddl/``, loaded through ``load_core_ddl``.

A project-scoped query binds ``$project_id``; the executor refuses a scoped query whose text does
not, and injects the parameter itself. Unscoped queries (readiness, version gate, schema setup) run
on the driver directly and never touch project data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final

from graph.core.errors import ConformanceError
from graph.core.query_files import read_ddl_file, read_query_file, render_query

PROJECT_ID_PARAMETER: Final = "project_id"
_PROJECT_ID_REFERENCE: Final = re.compile(r"\$project_id\b")
_QUERY_DIRECTORY: Final = Path(__file__).resolve().parent / "queries"
_DDL_DIRECTORY: Final = Path(__file__).resolve().parent / "ddl"


def load_core_query(name: str) -> str:
    """The text of the core query file ``name`` (relative to ``queries/``)."""

    return read_query_file(_QUERY_DIRECTORY, name)


@dataclass(frozen=True, slots=True)
class Query:
    """One named Cypher text; ``project_scoped`` queries must bind ``$project_id``."""

    name: str
    text: str
    project_scoped: bool = True

    def binds_project_id(self) -> bool:
        """Whether the text references the ``$project_id`` parameter."""

        return _PROJECT_ID_REFERENCE.search(self.text) is not None


PING: Final = Query("ping", load_core_query("ping.cypher"), project_scoped=False)

SERVER_COMPONENTS: Final = Query("server_components", load_core_query("server_components.cypher"), project_scoped=False)

# Names rendered into query text
#
# Neo4j 5.26 plans a dynamic label or type in MATCH/MERGE as a full scan (spike 1.2b), so labels and
# relationship types used for lookups are rendered into the text. Only names that passed
# ``Ontology.is_declared`` or are built-in may reach these factories (the caller checks that); every
# factory also checks the safe-name pattern. Values are always parameters. Factories are cached.

SAFE_NAME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SPEC_NODE: Final = "SpecNode"
# Spec node vector properties (data-model.md): written by build, never returned by reads.
EMBEDDING: Final = "embedding"
NAME_EMBEDDING: Final = "name_embedding"


def safe_name(name: object) -> str:
    """Return ``name`` when it may be rendered into Cypher, else raise ``ConformanceError``."""

    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise ConformanceError("cypher_name_unsafe")
    return name


@dataclass(frozen=True, slots=True)
class NodeShape:
    """How a node is looked up: static label, key property, whether ``$<var>_label`` is checked.

    A spec node is ``NodeShape("SpecNode", "uuid", True)``: matched on the unique ``SpecNode`` key,
    its domain label checked with ``$label IN labels(n)`` (a dynamic label in MATCH would scan).
    """

    label: str
    key: str
    check_label: bool = False

    def match(self, var: str) -> str:
        """``MATCH`` of one node of the project by ``$<var>_key`` (and ``$<var>_label``); the fragment
        file's ``RETURN`` tail, which only makes it a complete statement, is left out."""

        label, key = safe_name(self.label), safe_name(self.key)
        if self.check_label:
            return render_query(load_core_query("match_node_checked.cypher"), label=label, key=key, var=var, tail="")
        return render_query(load_core_query("match_node.cypher"), label=label, key=key, var=var, tail="")


# Schema setup (unscoped: rule I3 exempts it; runs on the driver directly)

SHOW_CONSTRAINTS: Final = Query("show_constraints", load_core_query("show_constraints.cypher"), project_scoped=False)

SHOW_INDEXES: Final = Query("show_indexes", load_core_query("show_indexes.cypher"), project_scoped=False)

AWAIT_INDEXES: Final = Query("await_indexes", load_core_query("await_indexes.cypher"), project_scoped=False)


# The constraints and indexes are defined in the static DDL files under ``ddl/`` (one statement per
# object, ``CREATE ... IF NOT EXISTS``), applied by ``graph.core.schema.ensure_schema`` in this order.

CORE_DDL_FILES: Final = ("node_constraints.cypher", "node_indexes.cypher", "relationship_indexes.cypher")


def load_core_ddl(name: str) -> tuple[str, ...]:
    """The statements of the core schema DDL file ``name`` (relative to ``ddl/``)."""

    return read_ddl_file(_DDL_DIRECTORY / name)


CORE_DDL: Final = tuple(statement for name in CORE_DDL_FILES for statement in load_core_ddl(name))


_DDL_HEAD: Final = re.compile(
    r"CREATE\s+(?:CONSTRAINT|(?:RANGE\s+|VECTOR\s+|FULLTEXT\s+)?INDEX)\s+(?P<name>\w+)\s+IF\s+NOT\s+EXISTS\s+FOR\s"
)


@dataclass(frozen=True, slots=True)
class DdlStatement:
    """One idempotent DDL statement of a DDL file and the name of the object it creates.

    The statement must start ``CREATE <kind> <name> IF NOT EXISTS FOR`` with ``name`` a safe name;
    anything else raises ``ConformanceError("ddl_statement_unsafe")``.
    """

    name: str
    text: str

    def __post_init__(self) -> None:
        head = _DDL_HEAD.match(self.text)
        if head is None or head["name"] != safe_name(self.name):
            raise ConformanceError("ddl_statement_unsafe", name=safe_name(self.name))


def ddl_query(statement: DdlStatement) -> Query:
    """The unscoped query that runs one DDL statement (schema setup, rule I3 exempts it)."""

    return Query(f"create_{statement.name}", statement.text, project_scoped=False)


# Owned writes (graph.core.writer). Parameters: ``$key`` (node key or relationship ``edge_key``),
# ``$props`` (properties to set; ``SET += `` so other properties stay), ``$label`` (domain label).

UPSERT_SPEC_NODE: Final = Query("upsert_spec_node", load_core_query("upsert_spec_node.cypher"))

DELETE_SPEC_NODE: Final = Query("delete_spec_node", load_core_query("delete_spec_node.cypher"))

SPEC_NODE_RELATIONSHIP_TYPES: Final = Query(
    "spec_node_relationship_types", load_core_query("spec_node_relationship_types.cypher")
)

SPEC_NODE_EXISTS: Final = Query("spec_node_exists", load_core_query("spec_node_exists.cypher"))

MOVE_FOREIGN_RELATIONSHIPS: Final = Query(
    "move_foreign_relationships", load_core_query("move_foreign_relationships.cypher")
)


# A node without domain label is upserted on a key a uniqueness constraint holds (graph.core.schema):
# ``Document`` on ``(group_id, document_id)``, ``Fragment`` on ``(group_id, fragment_id)``, and each
# foreign label (a declared label of another owner than build) on ``(group_id, uuid)``.

DOCUMENT: Final = "Document"
FRAGMENT: Final = "Fragment"
FOREIGN_KEY: Final = "uuid"

UPSERT_DOCUMENT: Final = Query(f"upsert_node_{DOCUMENT}", load_core_query("upsert_document.cypher"))

UPSERT_FRAGMENT: Final = Query(f"upsert_node_{FRAGMENT}", load_core_query("upsert_fragment.cypher"))

_NOT_FOREIGN: Final = frozenset({DOCUMENT, FRAGMENT, SPEC_NODE})


@cache
def upsert_foreign_node(label: str) -> Query:
    """Create or update a foreign node of ``label`` by its ``uuid``; ``Document``, ``Fragment`` and
    ``SpecNode`` are not foreign (``ConformanceError("node_key_unconstrained")``)."""

    if label in _NOT_FOREIGN:
        raise ConformanceError("node_key_unconstrained", label=label, key=FOREIGN_KEY)
    return Query(
        f"upsert_node_{label}",
        render_query(load_core_query("upsert_foreign_node.cypher"), foreign_label=safe_name(label)),
    )


def foreign_node_constraint(label: str) -> str:
    """The DDL statement of the ``(group_id, uuid)`` uniqueness constraint of the foreign ``label``, named
    ``graph_foreign_uuid_<label in lower case>`` (``ddl/foreign_node_constraint.cypher``): the statement a
    submodule's DDL holds for each foreign label it declares. It is not part of ``CORE_DDL`` (the shipped
    ontology has no foreign label). ``Document``, ``Fragment`` and ``SpecNode`` are not foreign
    (``ConformanceError("node_key_unconstrained")``)."""

    if label in _NOT_FOREIGN:
        raise ConformanceError("node_key_unconstrained", label=label, key=FOREIGN_KEY)
    (template,) = load_core_ddl("foreign_node_constraint.cypher")
    name = safe_name(label)
    return render_query(template, foreign_label=name, foreign_name=name.lower())


@cache
def delete_keyed_node(label: str, key: str) -> Query:
    return Query(
        f"delete_node_{label}",
        render_query(load_core_query("delete_keyed_node.cypher"), label=safe_name(label), key=safe_name(key)),
    )


@cache
def keyed_node_relationship_types(label: str, key: str) -> Query:
    return Query(
        f"relationship_types_{label}",
        render_query(
            load_core_query("keyed_node_relationship_types.cypher"),
            label=safe_name(label),
            key=safe_name(key),
        ),
    )


@cache
def upsert_relationship(rel_type: str, source: NodeShape, target: NodeShape) -> Query:
    """MERGE one relationship by ``edge_key`` between two existing nodes.

    No row: an end is missing. ``conflict`` true: a relationship of this type and ``edge_key``
    already joins other endpoints (found by a ``(group_id, edge_key)`` index seek), nothing written.
    """

    rel = safe_name(rel_type)
    return Query(
        f"upsert_relationship_{rel_type}",
        render_query(
            load_core_query("upsert_relationship.cypher"),
            source=source.match("s"),
            target=target.match("t"),
            type=rel,
        ),
    )


@cache
def delete_relationship(rel_type: str) -> Query:
    return Query(
        f"delete_relationship_{rel_type}",
        render_query(load_core_query("delete_relationship.cypher"), type=safe_name(rel_type)),
    )


# Reads (graph.core.reader). ``n.spec_id IS NOT NULL`` lets the planner seek the
# ``(group_id, spec_id)`` unique index on its ``group_id`` prefix instead of scanning the label.

READ_NODE_BY_SPEC_ID: Final = Query("read_node_by_spec_id", load_core_query("read_node_by_spec_id.cypher"))

READ_NODE_BY_ALIAS: Final = Query("read_node_by_alias", load_core_query("read_node_by_alias.cypher"))

READ_NODES: Final = Query("read_nodes", load_core_query("read_nodes.cypher"))

COUNT_NODES: Final = Query("count_nodes", load_core_query("count_nodes.cypher"))

READ_NODES_PAGE: Final = Query("read_nodes_page", load_core_query("read_nodes_page.cypher"))

READ_NEIGHBOURS: Final = Query("read_neighbours", load_core_query("read_neighbours.cypher"))

READ_EDGES: Final = Query("read_edges", load_core_query("read_edges.cypher"))

READ_DOCUMENTS: Final = Query("read_documents", load_core_query("read_documents.cypher"))

READ_FRAGMENTS: Final = Query("read_fragments", load_core_query("read_fragments.cypher"))

# Search and traversal (graph.core.reader). The vector and fulltext indexes span the whole database:
# hits are filtered by ``group_id``, spec label and ``$where`` after the index call. ``$index`` names
# the index (procedures take it as a value). An index page returns ``fetched`` (what the index gave
# before filtering) and ``hits`` (those of the project, in the index's score order); the reader asks
# for further pages until it has enough. Searches return keys and scores only; the reader then reads
# the nodes it keeps with ``READ_NODES_BY_UUID``.

READ_NODES_BY_UUID: Final = Query("read_nodes_by_uuid", load_core_query("read_nodes_by_uuid.cypher"))

READ_NODE_UUIDS_BY_SPEC_ID: Final = Query(
    "read_node_uuids_by_spec_id", load_core_query("read_node_uuids_by_spec_id.cypher")
)

READ_NODES_BY_IDS: Final = Query("read_nodes_by_ids", load_core_query("read_nodes_by_ids.cypher"))

SEARCH_NODES_FULLTEXT: Final = Query("search_nodes_fulltext", load_core_query("search_nodes_fulltext.cypher"))

COUNT_SEARCH_VECTORS: Final = Query("count_search_vectors", load_core_query("count_search_vectors.cypher"))

SEARCH_NODES_VECTOR: Final = Query("search_nodes_vector", load_core_query("search_nodes_vector.cypher"))

SEARCH_NODES_VECTOR_EXACT: Final = Query(
    "search_nodes_vector_exact", load_core_query("search_nodes_vector_exact.cypher")
)

SEARCH_FRAGMENTS: Final = Query("search_fragments", load_core_query("search_fragments.cypher"))

READ_FRAGMENT_HITS: Final = Query("read_fragment_hits", load_core_query("read_fragment_hits.cypher"))

READ_NODES_FOR_LINES: Final = Query("read_nodes_for_lines", load_core_query("read_nodes_for_lines.cypher"))

# Scope: one query per declared spine path (or contiguous part of a ``sequence`` path). Every walk
# follows each edge in its stored direction towards the root node unless the relation is in
# ``$symmetric``, and never passes through a node with a spine-root label (``$roots``).


@cache
def scope_via(relations: tuple[str, ...], min_hops: int, max_hops: int) -> Query:
    """Nodes reaching the root by ``min_hops``..``max_hops`` edges of any of ``relations``."""

    types = "|".join(safe_name(name) for name in relations)
    return Query(
        f"read_scope_via_{'_'.join(relations)}_{min_hops}_{max_hops}",
        render_query(
            load_core_query("scope_via.cypher"),
            types=types,
            min_hops=str(int(min_hops)),
            max_hops=str(int(max_hops)),
        ),
    )


@cache
def scope_sequence(relations: tuple[str, ...], symmetric: tuple[str, ...]) -> Query:
    """Nodes reaching the root by one edge of each of ``relations``, in order; a relation in
    ``symmetric`` is walked either way."""

    either_way = {safe_name(name) for name in symmetric}
    pattern = "(n:SpecNode)"
    conditions = []
    for index, relation in enumerate(relations):
        last = index == len(relations) - 1
        end = "(root)" if last else f"(x{index}:SpecNode)"
        arrow = "-" if relation in either_way else "->"
        pattern += f"-[r{index}:{safe_name(relation)}]{arrow}{end}"
        conditions.append(f"r{index}.group_id = $project_id")
        if not last:
            conditions.append(
                f"x{index}.group_id = $project_id AND none(label IN labels(x{index}) WHERE label IN $roots)"
            )
    return Query(
        f"read_scope_sequence_{'_'.join(relations)}",
        render_query(load_core_query("scope_sequence.cypher"), pattern=pattern, conditions=" AND ".join(conditions)),
    )


READ_IMPACT_HOP: Final = Query("read_impact_hop", load_core_query("read_impact_hop.cypher"))
