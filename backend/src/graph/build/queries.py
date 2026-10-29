"""Build Cypher: every query text the build runs, as named ``Query`` constants (architecture.md I3).

The statements live in ``.cypher`` files under ``queries/`` next to this module and are loaded through
``load_build_query``; a query whose relationship type, labels or hop count are only known at run time is a
file with ``__NAME__`` tokens that its factory fills with validated names.

Every query binds ``$project_id``; the core ``Executor`` injects it and refuses a query that does not.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from graph.core.errors import ConformanceError
from graph.core.queries import Query, safe_name
from graph.core.query_files import read_query_file, render_query

_QUERY_DIRECTORY: Final = Path(__file__).resolve().parent / "queries"
_MAX_HOPS: Final = 16


def load_build_query(name: str) -> str:
    """The text of the build query file ``name`` (relative to ``queries/``)."""

    return read_query_file(_QUERY_DIRECTORY, name)


# Plan (build.md step 1.2): the persisted documents the snapshot is compared with. Missing
# properties are defaulted so a row always validates as a ``DocumentRec``.
READ_PERSISTED_DOCUMENTS: Final = Query(
    "build_read_persisted_documents",
    load_build_query("build_read_persisted_documents.cypher"),
)

# Plan (build.md step 1.4): the persisted fragments a revision's fragments are matched with; retired
# fragments are history and never matched. Ordered by document, then ordinal.
READ_PERSISTED_FRAGMENTS: Final = Query(
    "build_read_persisted_fragments",
    load_build_query("build_read_persisted_fragments.cypher"),
)

# Extract nodes (build.md step 2): known terms of an ``update`` build are the names of the persisted
# hub nodes. ``$labels`` are the ontology's hub labels.
READ_PERSISTED_HUBS: Final = Query(
    "build_read_persisted_hubs",
    load_build_query("build_read_persisted_hubs.cypher"),
)

# Mode selection (build.md "Modes"): whether anything of the project was persisted. An interrupted first
# persist can leave spec nodes without any ``Document`` (documents are written in the last phase).
READ_PERSISTED_PRESENCE: Final = Query(
    "build_read_persisted_presence",
    load_build_query("build_read_persisted_presence.cypher"),
)


# Diff (build.md step 6): the current graph. Spec nodes are found through the ``(group_id, uuid)`` unique
# index (``uuid IS NOT NULL`` lets the planner seek on its ``group_id`` prefix); the vectors are left out.
READ_CURRENT_NODES: Final = Query(
    "build_read_current_nodes",
    load_build_query("build_read_current_nodes.cypher"),
)

# Each node's ``REFERENCED_BY`` (with the text of its evidence lines, cut from the fragment text whose
# line ``i`` is source line ``line_start + i``) and ``MENTIONED_IN`` relationships.
READ_CURRENT_LINKS: Final = Query(
    "build_read_current_links",
    load_build_query("build_read_current_links.cypher"),
)

READ_CURRENT_FRAGMENTS: Final = Query(
    "build_read_current_fragments",
    load_build_query("build_read_current_fragments.cypher"),
)


@cache
def read_current_edges(relation: str) -> Query:
    """Every edge of one domain relation in the project, found through its ``(group_id, edge_key)``
    index, with the uuid, identity key and ``spec_id`` of both ends."""

    rel = safe_name(relation)
    return Query(
        f"build_read_current_edges_{rel}",
        render_query(load_build_query("read_current_edges.cypher"), type=rel),
    )


# Persist (build.md step 7). Each query takes ``$rows`` (one map per operation of a batch) and runs
# inside the batch's transaction. Nodes are found on their unique ``(group_id, <key>)`` keys; a domain
# label is set only when the node is created (a dynamic label in a MATCH/MERGE pattern scans), and
# relationship types are rendered from validated names (``relationship_query``). Every query returns
# ``written``, the number of rows that found what they write to.

CREATE_SPEC_NODES: Final = Query(
    "build_create_spec_nodes",
    load_build_query("build_create_spec_nodes.cypher"),
)

UPDATE_SPEC_NODES: Final = Query(
    "build_update_spec_nodes",
    load_build_query("build_update_spec_nodes.cypher"),
)

# Merge (W3): per row, every relationship of each merged node whose type build does not own
# (``$owned_types``) moves to the survivor (same pattern as the core ``MOVE_FOREIGN_RELATIONSHIPS``: same
# type, direction, other end and properties; an equal one already on the survivor is not duplicated; a
# self-loop stays a self-loop; one between the two nodes is deleted, never turned into a self-loop),
# then the merged node is deleted with its build-owned relationships. The survivor gets its final
# ``spec_id_aliases`` (its other properties and stamps come with its update). A merged node already
# gone (the batch applied again) is skipped.
MERGE_SPEC_NODES: Final = Query(
    "build_merge_spec_nodes",
    load_build_query("build_merge_spec_nodes.cypher"),
)

DELETE_SPEC_NODES: Final = Query(
    "build_delete_spec_nodes",
    load_build_query("build_delete_spec_nodes.cypher"),
)

# A missing fragment is created as a key-only stub: fragments are written in the last phase
# (documents and fragments), which then sets their properties.
REPLACE_NODE_LINKS: Final = Query(
    "build_replace_node_links",
    load_build_query("build_replace_node_links.cypher"),
)

UPSERT_DOCUMENTS: Final = Query(
    "build_upsert_documents",
    load_build_query("build_upsert_documents.cypher"),
)

SET_DOCUMENT_STATUS: Final = Query(
    "build_set_document_status",
    load_build_query("build_set_document_status.cypher"),
)

UPSERT_FRAGMENTS: Final = Query(
    "build_upsert_fragments",
    load_build_query("build_upsert_fragments.cypher"),
)

SET_FRAGMENT_STATUS: Final = Query(
    "build_set_fragment_status",
    load_build_query("build_set_fragment_status.cypher"),
)

# Domain edges of one relation: the type is rendered (a dynamic type in MATCH/MERGE scans); the caller
# checks ``Ontology.is_declared``, the factories check the safe-name pattern. Edges are found through
# the per-relation ``(group_id, edge_key)`` index.


@cache
def upsert_edges(relation: str) -> Query:
    """MERGE edges by ``row.edge_key`` between the nodes with uuids ``row.source`` and ``row.target``."""

    rel = safe_name(relation)
    return Query(
        f"build_upsert_edges_{rel}",
        render_query(load_build_query("upsert_edges.cypher"), type=rel),
    )


@cache
def update_edges(relation: str) -> Query:
    """``SET += row.props`` on the edge ``row.uuid`` stored under one of ``row.keys`` (its key before
    the update, and the key the update gives it, so a batch applied again finds it). An edge without a
    ``uuid`` is matched on its key alone."""

    rel = safe_name(relation)
    return Query(
        f"build_update_edges_{rel}",
        render_query(load_build_query("update_edges.cypher"), type=rel),
    )


@cache
def delete_edges(relation: str) -> Query:
    """Delete the edge ``row.uuid`` stored under ``row.edge_key`` (an edge without a ``uuid``: by key).

    Matching the uuid too keeps a batch applied again from deleting another edge that has since been
    given this key."""

    rel = safe_name(relation)
    return Query(
        f"build_delete_edges_{rel}",
        render_query(load_build_query("delete_edges.cypher"), type=rel),
    )


# Verification (analysis.md "Verification"; ``graph.build.verify``). The reads return only what build
# owns: spec nodes with their build-owned properties and their build-owned labels (``$labels``),
# relationships of build-owned relations (``$relations``) and fragments; foreign labels, properties
# and relationship types are never read. The repairs address what the reads returned by element id
# (a fault may have removed a node's ``uuid`` or an edge's ``edge_key``) and re-check the project and
# the owned type, so they never touch a foreign node or relationship.

VERIFY_SPEC_NODES: Final = Query(
    "build_verify_spec_nodes",
    load_build_query("build_verify_spec_nodes.cypher"),
)

# Domain edges touching a spec node of the project (either end may be something else: a fault).
VERIFY_DOMAIN_EDGES: Final = Query(
    "build_verify_domain_edges",
    load_build_query("build_verify_domain_edges.cypher"),
)

# Fragments: status and hashes (never the text), and whether any spec node is grounded in them.
VERIFY_FRAGMENTS: Final = Query(
    "build_verify_fragments",
    load_build_query("build_verify_fragments.cypher"),
)

REPAIR_DELETE_EDGES: Final = Query(
    "build_repair_delete_edges",
    load_build_query("build_repair_delete_edges.cypher"),
)

# W4: a spec node deleted by a repair goes with its relationships.
REPAIR_DELETE_NODES: Final = Query(
    "build_repair_delete_nodes",
    load_build_query("build_repair_delete_nodes.cypher"),
)

# ``row.remove``: the node's other build-owned labels; ``row.label``: the label it keeps (both checked
# against the ontology by the caller); ``row.label_disagreement``: its value with that label (null
# removes it).
REPAIR_SET_LABELS: Final = Query(
    "build_repair_set_labels",
    load_build_query("build_repair_set_labels.cypher"),
)

REPAIR_SET_SPEC_IDS: Final = Query(
    "build_repair_set_spec_ids",
    load_build_query("build_repair_set_spec_ids.cypher"),
)

# Restored evidence: one ``REFERENCED_BY`` per evidence line range, to fragments that exist.
REPAIR_NODE_EVIDENCE: Final = Query(
    "build_repair_node_evidence",
    load_build_query("build_repair_node_evidence.cypher"),
)

REPAIR_EDGE_EVIDENCE: Final = Query(
    "build_repair_edge_evidence",
    load_build_query("build_repair_edge_evidence.cypher"),
)


# Infer (analysis.md "Light inference"). Subjects and candidates are read through stated and implied
# edges only (``$bases``; an edge without ``basis`` counts as stated), so earlier inferred edges never
# feed new candidates (FR-INF-02). Labels are
# parameters (``$subject_labels``, ``$target_labels``); relation names and hop counts come from the
# validated ontology and are rendered. A symmetric relation is matched in either direction.


def _arrow(*, symmetric: bool) -> str:
    return "-" if symmetric else "->"


def _hops(count: object) -> int:
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= _MAX_HOPS:
        raise ConformanceError("cypher_hops_invalid")
    return count


@cache
def infer_endpoint_subjects(via: tuple[str, ...], symmetric: tuple[str, ...], *, mentions: bool) -> Query:
    """Template ``endpoints``: each subject node with the uuids of its target nodes: those it reaches
    over one edge of a ``via`` relation (those in ``symmetric`` in either direction), and with
    ``mentions`` those ``MENTIONED_IN`` one of its evidence fragments (``REFERENCED_BY``, status in
    ``$statuses``); subjects without any target are left out."""

    if not via:
        raise ConformanceError("inference_via_empty")
    either = {safe_name(name) for name in symmetric}
    branches = [
        render_query(
            load_build_query("infer_endpoint_via.cypher"),
            type=safe_name(name),
            arrow=_arrow(symmetric=name in either),
        )
        for name in via
    ]
    if mentions:
        branches.append(load_build_query("infer_endpoint_mentions.cypher"))
    name = "_".join(via) + ("_mentions" if mentions else "")
    return Query(
        f"build_infer_endpoint_subjects_{name}",
        render_query(load_build_query("infer_endpoint_subjects.cypher"), branches=" UNION ".join(branches)),
    )


@cache
def infer_realization_subjects(
    member_relation: str,
    container_relation: str,
    *,
    member_symmetric: bool,
    container_symmetric: bool,
    max_hops: int,
) -> Query:
    """Template ``realization``: each subject node with its members (nodes with a ``member_relation``
    edge to it) and the container nodes reached from them by 1 to ``max_hops`` ``container_relation``
    edges, each with the members it is reached from; subjects without any container are left out."""

    member, container, hops = safe_name(member_relation), safe_name(container_relation), _hops(max_hops)
    return Query(
        f"build_infer_realization_subjects_{member}_{container}_{hops}",
        render_query(
            load_build_query("infer_realization_subjects.cypher"),
            member=member,
            member_arrow=_arrow(symmetric=member_symmetric),
            container=container,
            container_arrow=_arrow(symmetric=container_symmetric),
            hops=str(hops),
        ),
    )


# Spine reachability (ontology.md "Spine and inference"; ``graph.build.spine``, used by inference and
# verification): the spec nodes with their build-owned labels (``$labels``) and the edges of the spine
# relations (``$relations``) between spec nodes of the project, with their ``basis``; the paths are
# walked in memory.
READ_SPINE_NODES: Final = Query(
    "build_read_spine_nodes",
    load_build_query("build_read_spine_nodes.cypher"),
)

READ_SPINE_EDGES: Final = Query(
    "build_read_spine_edges",
    load_build_query("build_read_spine_edges.cypher"),
)


@cache
def infer_similar_targets(writes: str, *, symmetric: bool) -> Query:
    """Template ``attach_orphan``: for each node of ``$uuids``, the ``$limit`` target nodes of the
    project most similar to it by exact ``embedding`` cosine, leaving out those it already has a
    ``writes`` edge with (stated or implied); best first, ties by ``spec_id``, then ``uuid``. The
    project's targets are read once and scored for every subject, so the result never depends on
    other projects in the database. A node without an embedding has none."""

    rel = safe_name(writes)
    return Query(
        f"build_infer_similar_targets_{rel}",
        render_query(load_build_query("infer_similar_targets.cypher"), type=rel, arrow=_arrow(symmetric=symmetric)),
    )


@cache
def infer_exclusive_holders(writes: str) -> Query:
    """The nodes of ``$uuids`` that already have a stated or implied ``writes`` edge on one of the
    exclusive pairs ``$pairs`` (``[source label, target label]``)."""

    rel = safe_name(writes)
    return Query(
        f"build_infer_exclusive_holders_{rel}",
        render_query(load_build_query("infer_exclusive_holders.cypher"), type=rel),
    )


# The nodes a judgement shows: name, domain label (``$labels``), identity key, statements and up to
# ``$per_node`` grounding fragments (``REFERENCED_BY`` to a fragment whose status is in ``$statuses``)
# in document order, by id and content hash; their texts are read only for the judged subjects.
READ_INFER_NODES: Final = Query(
    "build_read_infer_nodes",
    load_build_query("build_read_infer_nodes.cypher"),
)

READ_INFER_FRAGMENTS: Final = Query(
    "build_read_infer_fragments",
    load_build_query("build_read_infer_fragments.cypher"),
)


@cache
def infer_write_edges(relation: str) -> Query:
    """The swap's writes: MERGE each inferred edge ``row.edge_key`` from ``row.source`` to ``row.target``
    (``uuid`` and ``created_at`` set on creation only) and set ``row.props``; ``refs`` are the
    ``row.ref`` of the rows written. A row is skipped when an
    edge with its key exists that is not an inferred edge of the same rule (``row.rule``), or, with
    ``row.either_direction``, when the two nodes are already linked by a non-inferred edge of the
    relation in either direction."""

    rel = safe_name(relation)
    return Query(
        f"build_infer_write_edges_{rel}",
        render_query(load_build_query("infer_write_edges.cypher"), type=rel),
    )


@cache
def infer_delete_edges(relation: str) -> Query:
    """The swap's deletes: the inferred edge ``row.uuid`` of rule ``row.rule`` under ``row.edge_key``;
    ``refs`` are the ``row.ref`` of the rows deleted."""

    rel = safe_name(relation)
    return Query(
        f"build_infer_delete_edges_{rel}",
        render_query(load_build_query("infer_delete_edges.cypher"), type=rel),
    )
