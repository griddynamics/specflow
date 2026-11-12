"""Core queries: every query the core runs, as named ``Query`` objects (architecture.md I3, design D2).

A ``Query`` is dialect-neutral: its ``name`` (unique; the key of ``OfflineExecutor``, logs and golden
text), a ``template`` id (``core.<file stem>``) and typed semantic ``args`` (relations, hop bounds,
node shapes, labels; frozen dataclasses below). The factories validate the names they are given and
return queries; they never build query text. A store's dialect (``graph.core.dialect``) turns a query
into an execution plan: for a Cypher store the canonical ``.cypher`` files under ``queries/`` next to
this module, a query whose label or relationship type is only known at run time being a file with
``__NAME__`` tokens that the Cypher dialect fills with validated names. The schema's constraints and
indexes are static DDL files under ``ddl/``, loaded through ``load_core_ddl``.

A project-scoped query binds the project id; the executor injects the parameter and the dialect
refuses a scoped statement that does not reference it. Unscoped queries (readiness, version gate,
schema setup) never touch project data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from typing import Any, Final

from graph.core.errors import ConformanceError
from graph.core.query_files import CORE_DDL_DIRECTORY, GRAPH_ROOT, read_ddl_file, render_query

PROJECT_ID_PARAMETER: Final = "project_id"
LITERAL_TEMPLATE: Final = "core.literal"
DDL_TEMPLATE: Final = "core.ddl"
_SUBMODULE_DDL_DIRECTORY: Final = "ddl"
_SUBMODULE_DDL_SUFFIX: Final = ".cypher"


@dataclass(frozen=True, slots=True)
class Query:
    """One named, dialect-neutral query: ``template`` says what it does, ``args`` (a frozen dataclass of
    semantic arguments, or None) what it is about; ``project_scoped`` queries bind the project id."""

    name: str
    template: str
    args: Any = None
    project_scoped: bool = True

    @classmethod
    def literal(cls, name: str, text: str, *, project_scoped: bool = True) -> Query:
        """A query that is one fixed Cypher text (tests, ad-hoc probes); only Cypher dialects run it."""

        return cls(name, LITERAL_TEMPLATE, LiteralText(text), project_scoped)

    @property
    def identity(self) -> tuple[str, Any]:
        """What makes two queries the same: name and arguments (never rendered text)."""

        return self.name, self.args


@dataclass(frozen=True, slots=True)
class LiteralText:
    """Arguments of a literal query: its whole text."""

    text: str


@dataclass(frozen=True, slots=True)
class DdlText:
    """Arguments of a DDL query: the canonical (Cypher) statement."""

    statement: str


@dataclass(frozen=True, slots=True)
class Relation:
    """One relationship type (validated safe name)."""

    type: str


@dataclass(frozen=True, slots=True)
class LabelKey:
    """A node label and the key property its nodes are found by (validated safe names)."""

    label: str
    key: str


@dataclass(frozen=True, slots=True)
class ForeignLabel:
    """A foreign node label (validated safe name)."""

    label: str


@dataclass(frozen=True, slots=True)
class RelationEnds:
    """A relationship of ``type`` between two nodes found by their shapes."""

    type: str
    source: NodeShape
    target: NodeShape


@dataclass(frozen=True, slots=True)
class ScopeVia:
    """Spine path of ``relations`` walked ``min_hops``..``max_hops`` edges towards the root."""

    relations: tuple[str, ...]
    min_hops: int
    max_hops: int


@dataclass(frozen=True, slots=True)
class ScopeSequence:
    """Spine sequence: one edge of each of ``relations`` in order; those in ``symmetric`` either way."""

    relations: tuple[str, ...]
    symmetric: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EndpointSubjects:
    """Inference ``endpoints``: targets over one edge of ``via`` (``symmetric`` ones either way) and, with
    ``mentions``, those mentioned in evidence fragments."""

    via: tuple[str, ...]
    symmetric: tuple[str, ...]
    mentions: bool


@dataclass(frozen=True, slots=True)
class RealizationSubjects:
    """Inference ``realization``: members by ``member``, containers one container path on.

    The path is ``relations`` with ``symmetric`` (per relation: walked either way). A ``sequence`` path is one
    edge of each relation in order (``min_hops == max_hops == len(relations)``); otherwise ``min_hops`` to
    ``max_hops`` edges of any of ``relations``, all in one direction."""

    member: str
    member_symmetric: bool
    relations: tuple[str, ...]
    symmetric: tuple[bool, ...]
    sequence: bool
    min_hops: int
    max_hops: int


@dataclass(frozen=True, slots=True)
class SimilarTargets:
    """Inference ``attach_orphan``: nearest targets by embedding for relation ``type``."""

    type: str
    symmetric: bool


PING: Final = Query("ping", "core.ping", project_scoped=False)

SERVER_COMPONENTS: Final = Query("server_components", "core.server_components", project_scoped=False)

# Names rendered into query text
#
# Neo4j 5.26 plans a dynamic label or type in MATCH/MERGE as a full scan (spike 1.2b), so labels and
# relationship types used for lookups are rendered into the text. Only names that passed
# ``Ontology.is_declared`` or are built-in may reach these factories (the caller checks that); every
# factory also checks the safe-name pattern. Values are always parameters. Factories are cached.

SAFE_NAME: Final = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SPEC_NODE: Final = "SpecNode"
# Spec node vector properties: written by build, never returned by reads.
EMBEDDING: Final = "embedding"
NAME_EMBEDDING: Final = "name_embedding"


def safe_name(name: object) -> str:
    """Return ``name`` when it may be rendered into Cypher, else raise ``ConformanceError``."""

    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise ConformanceError("cypher_name_unsafe")
    return name


@dataclass(frozen=True, slots=True)
class NodeShape:
    """How a node is looked up: static label, key property, whether ``$<var>_label`` is checked."""

    label: str
    key: str
    check_label: bool = False

    def checked(self) -> NodeShape:
        """This shape with label and key checked against the safe-name pattern (``ConformanceError``)."""

        return NodeShape(safe_name(self.label), safe_name(self.key), self.check_label)


# Schema setup (unscoped: rule I3 exempts it; runs on the driver directly)

SHOW_CONSTRAINTS: Final = Query("show_constraints", "core.show_constraints", project_scoped=False)

SHOW_INDEXES: Final = Query("show_indexes", "core.show_indexes", project_scoped=False)

AWAIT_INDEXES: Final = Query("await_indexes", "core.await_indexes", project_scoped=False)


# The constraints and indexes are defined in the static DDL files under ``ddl/`` (one statement per
# object, ``CREATE... IF NOT EXISTS``), applied by ``graph.core.schema.ensure_schema`` in this order.

CORE_DDL_FILES: Final = ("node_constraints.cypher", "node_indexes.cypher", "relationship_indexes.cypher")


def load_core_ddl(name: str) -> tuple[str, ...]:
    """The statements of the core schema DDL file ``name`` (relative to ``ddl/``)."""

    return read_ddl_file(CORE_DDL_DIRECTORY / name)


CORE_DDL: Final = tuple(statement for name in CORE_DDL_FILES for statement in load_core_ddl(name))


def submodule_ddl() -> tuple[str, ...]:
    """The statements of every ``graph/<sub>/ddl/*.cypher``, sorted, without importing a submodule."""

    statements: list[str] = []
    for child in sorted(GRAPH_ROOT.iterdir(), key=lambda item: item.name):
        if child.name in {"core", "stores"} or child.name.startswith(("_", ".")) or not child.is_dir():
            continue
        directory = child / _SUBMODULE_DDL_DIRECTORY
        if not directory.is_dir():
            continue
        for file in sorted(directory.iterdir(), key=lambda item: item.name):
            if file.is_file() and file.name.endswith(_SUBMODULE_DDL_SUFFIX):
                statements.extend(read_ddl_file(file))
    return tuple(statements)


_DDL_HEAD: Final = re.compile(
    r"CREATE\s+(?:CONSTRAINT|(?:RANGE\s+|VECTOR\s+|FULLTEXT\s+)?INDEX)\s+(?P<name>\w+)\s+IF\s+NOT\s+EXISTS\s+FOR\s"
)


@dataclass(frozen=True, slots=True)
class DdlStatement:
    """One idempotent DDL statement of a DDL file and the name of the object it creates."""

    name: str
    text: str

    def __post_init__(self) -> None:
        head = _DDL_HEAD.match(self.text)
        if head is None or head["name"] != safe_name(self.name):
            raise ConformanceError("ddl_statement_unsafe", name=safe_name(self.name))


def ddl_query(statement: DdlStatement) -> Query:
    """The unscoped query that runs one DDL statement (schema setup, rule I3 exempts it)."""

    return Query(f"create_{statement.name}", DDL_TEMPLATE, DdlText(statement.text), project_scoped=False)


# Owned writes (graph.core.writer). Parameters: ``$key`` (node key or relationship ``edge_key``)
# ``$props`` (properties to set; ``SET += `` so other properties stay), ``$label`` (domain label).

UPSERT_SPEC_NODE: Final = Query("upsert_spec_node", "core.upsert_spec_node")

DELETE_SPEC_NODE: Final = Query("delete_spec_node", "core.delete_spec_node")

SPEC_NODE_RELATIONSHIP_TYPES: Final = Query("spec_node_relationship_types", "core.spec_node_relationship_types")

SPEC_NODE_EXISTS: Final = Query("spec_node_exists", "core.spec_node_exists")

MOVE_FOREIGN_RELATIONSHIPS: Final = Query("move_foreign_relationships", "core.move_foreign_relationships")


# A node without domain label is upserted on a key a uniqueness constraint holds (graph.core.schema):
# ``Document`` on ``(group_id, document_id)``, ``Fragment`` on ``(group_id, fragment_id)``, and each
# foreign label (a declared label of another owner than build) on ``(group_id, uuid)``.

DOCUMENT: Final = "Document"
FRAGMENT: Final = "Fragment"
FOREIGN_KEY: Final = "uuid"

UPSERT_DOCUMENT: Final = Query(f"upsert_node_{DOCUMENT}", "core.upsert_document")

UPSERT_FRAGMENT: Final = Query(f"upsert_node_{FRAGMENT}", "core.upsert_fragment")

_NOT_FOREIGN: Final = frozenset({DOCUMENT, FRAGMENT, SPEC_NODE})


@cache
def upsert_foreign_node(label: str) -> Query:
    """Create or update a foreign node of ``label`` by its ``uuid``; ``Document``, ``Fragment`` and
    ``SpecNode`` are not foreign (``ConformanceError("node_key_unconstrained")``)."""

    if label in _NOT_FOREIGN:
        raise ConformanceError("node_key_unconstrained", label=label, key=FOREIGN_KEY)
    return Query(f"upsert_node_{label}", "core.upsert_foreign_node", ForeignLabel(safe_name(label)))


def foreign_node_constraint(label: str) -> str:
    """The DDL statement of the ``(group_id, uuid)`` uniqueness constraint of the foreign ``label``, named
    ``graph_foreign_uuid_<label in lower case>`` (``ddl/foreign_node_constraint.cypher``): the statement a submodule's
    DDL holds for each foreign label it declares.
    """

    if label in _NOT_FOREIGN:
        raise ConformanceError("node_key_unconstrained", label=label, key=FOREIGN_KEY)
    (template,) = load_core_ddl("foreign_node_constraint.cypher")
    name = safe_name(label)
    return render_query(template, foreign_label=name, foreign_name=name.lower())


@cache
def delete_keyed_node(label: str, key: str) -> Query:
    return Query(f"delete_node_{label}", "core.delete_keyed_node", LabelKey(safe_name(label), safe_name(key)))


@cache
def keyed_node_relationship_types(label: str, key: str) -> Query:
    return Query(
        f"relationship_types_{label}",
        "core.keyed_node_relationship_types",
        LabelKey(safe_name(label), safe_name(key)),
    )


@cache
def upsert_relationship(rel_type: str, source: NodeShape, target: NodeShape) -> Query:
    """MERGE one relationship by ``edge_key`` between two existing nodes."""

    rel = safe_name(rel_type)
    return Query(
        f"upsert_relationship_{rel_type}",
        "core.upsert_relationship",
        RelationEnds(rel, source.checked(), target.checked()),
    )


@cache
def delete_relationship(rel_type: str) -> Query:
    return Query(f"delete_relationship_{rel_type}", "core.delete_relationship", Relation(safe_name(rel_type)))


# Reads (graph.core.reader). ``n.spec_id IS NOT NULL`` lets the planner seek the
# ``(group_id, spec_id)`` unique index on its ``group_id`` prefix instead of scanning the label.

READ_NODE_BY_SPEC_ID: Final = Query("read_node_by_spec_id", "core.read_node_by_spec_id")

READ_NODE_BY_ALIAS: Final = Query("read_node_by_alias", "core.read_node_by_alias")

READ_NODES: Final = Query("read_nodes", "core.read_nodes")

READ_NEIGHBOURS: Final = Query("read_neighbours", "core.read_neighbours")

COUNT_NODES: Final = Query("count_nodes", "core.count_nodes")

READ_NODES_PAGE: Final = Query("read_nodes_page", "core.read_nodes_page")

READ_EDGES: Final = Query("read_edges", "core.read_edges")

# Read-time ``inherit``: the nodes one ``via`` hop from each node whose
# inheriting attribute is null, with the stored values the reader may fill in from the owner.
READ_OWNER_VALUES: Final = Query("read_owner_values", "core.read_owner_values")

READ_DOCUMENTS: Final = Query("read_documents", "core.read_documents")

READ_FRAGMENTS: Final = Query("read_fragments", "core.read_fragments")

# Search and traversal (graph.core.reader). The vector and fulltext indexes span the whole database:
# hits are filtered by ``group_id``, spec label and ``$where`` after the index call (``$defaulted``: by
# attribute, the labels whose declared default is the filter value, or for a null filter the labels with
# any default: an absent property reads as the default; see ``GraphReader._filters``). ``$index`` names
# the index (procedures take it as a value). An index page returns ``fetched`` (what the index gave
# before filtering) and ``hits`` (those of the project, in the index's score order); the reader asks
# for further pages until it has enough. Searches return keys and scores only; the reader then reads
# the nodes it keeps with ``READ_NODES_BY_UUID``.

READ_NODES_BY_UUID: Final = Query("read_nodes_by_uuid", "core.read_nodes_by_uuid")

READ_NODE_UUIDS_BY_SPEC_ID: Final = Query("read_node_uuids_by_spec_id", "core.read_node_uuids_by_spec_id")

READ_NODES_BY_IDS: Final = Query("read_nodes_by_ids", "core.read_nodes_by_ids")

SEARCH_NODES_FULLTEXT: Final = Query("search_nodes_fulltext", "core.search_nodes_fulltext")

COUNT_SEARCH_VECTORS: Final = Query("count_search_vectors", "core.count_search_vectors")

SEARCH_NODES_VECTOR: Final = Query("search_nodes_vector", "core.search_nodes_vector")

SEARCH_NODES_VECTOR_EXACT: Final = Query("search_nodes_vector_exact", "core.search_nodes_vector_exact")

SEARCH_FRAGMENTS: Final = Query("search_fragments", "core.search_fragments")

READ_FRAGMENT_HITS: Final = Query("read_fragment_hits", "core.read_fragment_hits")

READ_NODES_FOR_LINES: Final = Query("read_nodes_for_lines", "core.read_nodes_for_lines")

# The stored search vectors of given nodes (``similar``, ``expand``): compared without an embedding call.
READ_NODE_VECTORS: Final = Query("read_node_vectors", "core.read_node_vectors")

# Scope: one query per declared spine path (or contiguous part of a ``sequence`` path). Every walk
# follows each edge in its stored direction towards the root node unless the relation is in
# ``$symmetric``, and never passes through a node with a spine-root label (``$roots``).


@cache
def scope_via(relations: tuple[str, ...], min_hops: int, max_hops: int) -> Query:
    """Nodes reaching the root by ``min_hops``..``max_hops`` edges of any of ``relations``."""

    return Query(
        f"read_scope_via_{'_'.join(relations)}_{min_hops}_{max_hops}",
        "core.scope_via",
        ScopeVia(tuple(safe_name(name) for name in relations), int(min_hops), int(max_hops)),
    )


@cache
def scope_sequence(relations: tuple[str, ...], symmetric: tuple[str, ...]) -> Query:
    """Nodes reaching the root by one edge of each of ``relations``, in order; a relation in
    ``symmetric`` is walked either way."""

    either_way = tuple(safe_name(name) for name in symmetric)
    return Query(
        f"read_scope_sequence_{'_'.join(relations)}",
        "core.scope_sequence",
        ScopeSequence(tuple(safe_name(name) for name in relations), either_way),
    )


READ_IMPACT_HOP: Final = Query("read_impact_hop", "core.read_impact_hop")

# Trace (trace.md "Library"): the incident domain edges of a frontier, roles applied by the reader.
READ_TRACE_HOP: Final = Query("read_trace_hop", "core.read_trace_hop")
