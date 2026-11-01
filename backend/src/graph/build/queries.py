"""Build queries: every query the build runs, as named ``Query`` objects (architecture.md I3, design D2).

Like the core catalog (``graph.core.queries``) these are dialect-neutral: a name, a template id
(``build.<file stem>``) and typed semantic arguments. The canonical statements live in ``.cypher`` files
under ``queries/`` next to this module and are loaded through ``load_build_query`` by a Cypher store's
dialect; a query whose relationship type, labels or hop count are only known at run time is a file with
``__NAME__`` tokens that the dialect fills with the validated names its factory gives.

Every query binds the project id; the executor injects it and the dialect refuses one that does not.
"""

from __future__ import annotations

from functools import cache
from typing import Final

from graph.core.errors import ConformanceError
from graph.core.queries import (
    EndpointSubjects,
    Query,
    RealizationSubjects,
    Relation,
    SimilarTargets,
    safe_name,
)
from graph.core.query_files import load_build_query as load_build_query  # noqa: PLC0414 (re-export)

_MAX_HOPS: Final = 16


# Plan (build.md step 1.2): the persisted documents the snapshot is compared with. Missing
# properties are defaulted so a row always validates as a ``DocumentRec``.
READ_PERSISTED_DOCUMENTS: Final = Query("build_read_persisted_documents", "build.build_read_persisted_documents")

# Plan (build.md step 1.4): the persisted fragments a revision's fragments are matched with; retired
# fragments are history and never matched. Ordered by document, then ordinal.
READ_PERSISTED_FRAGMENTS: Final = Query("build_read_persisted_fragments", "build.build_read_persisted_fragments")

# Extract nodes (build.md step 2): known terms of an ``update`` build are the names of the persisted
# hub nodes. ``$labels`` are the ontology's hub labels.
READ_PERSISTED_HUBS: Final = Query("build_read_persisted_hubs", "build.build_read_persisted_hubs")

# Mode selection (build.md "Modes"): whether anything of the project was persisted. An interrupted first
# persist can leave spec nodes without any ``Document`` (documents are written in the last phase).
READ_PERSISTED_PRESENCE: Final = Query("build_read_persisted_presence", "build.build_read_persisted_presence")


# Diff (build.md step 6): the current graph. Spec nodes are found through the ``(group_id, uuid)`` unique
# index (``uuid IS NOT NULL`` lets the planner seek on its ``group_id`` prefix); the vectors are left out.
READ_CURRENT_NODES: Final = Query("build_read_current_nodes", "build.build_read_current_nodes")

# Each node's ``REFERENCED_BY`` (with the text of its evidence lines, cut from the fragment text whose
# line ``i`` is source line ``line_start + i``) and ``MENTIONED_IN`` relationships.
READ_CURRENT_LINKS: Final = Query("build_read_current_links", "build.build_read_current_links")

READ_CURRENT_FRAGMENTS: Final = Query("build_read_current_fragments", "build.build_read_current_fragments")


@cache
def read_current_edges(relation: str) -> Query:
    """Every edge of one domain relation in the project, found through its ``(group_id, edge_key)``
    index, with the uuid, identity key and ``spec_id`` of both ends."""

    rel = safe_name(relation)
    return Query(f"build_read_current_edges_{rel}", "build.read_current_edges", Relation(rel))


# Persist (build.md step 7). Each query takes ``$rows`` (one map per operation of a batch) and runs
# inside the batch's transaction. Nodes are found on their unique ``(group_id, <key>)`` keys; a domain
# label is set only when the node is created (a dynamic label in a MATCH/MERGE pattern scans), and
# relationship types are rendered from validated names (``relationship_query``). Every query returns
# ``written``, the number of rows that found what they write to.

CREATE_SPEC_NODES: Final = Query("build_create_spec_nodes", "build.build_create_spec_nodes")

UPDATE_SPEC_NODES: Final = Query("build_update_spec_nodes", "build.build_update_spec_nodes")

# Merge (W3): per row, every relationship of each merged node whose type build does not own
# (``$owned_types``) moves to the survivor (same pattern as the core ``MOVE_FOREIGN_RELATIONSHIPS``: same
# type, direction, other end and properties; an equal one already on the survivor is not duplicated; a
# self-loop stays a self-loop; one between the two nodes is deleted, never turned into a self-loop),
# then the merged node is deleted with its build-owned relationships. The survivor gets its final
# ``spec_id_aliases`` (its other properties and stamps come with its update). A merged node already
# gone (the batch applied again) is skipped.
MERGE_SPEC_NODES: Final = Query("build_merge_spec_nodes", "build.build_merge_spec_nodes")

DELETE_SPEC_NODES: Final = Query("build_delete_spec_nodes", "build.build_delete_spec_nodes")

# A missing fragment is created as a key-only stub: fragments are written in the last phase
# (documents and fragments), which then sets their properties.
REPLACE_NODE_LINKS: Final = Query("build_replace_node_links", "build.build_replace_node_links")

UPSERT_DOCUMENTS: Final = Query("build_upsert_documents", "build.build_upsert_documents")

SET_DOCUMENT_STATUS: Final = Query("build_set_document_status", "build.build_set_document_status")

UPSERT_FRAGMENTS: Final = Query("build_upsert_fragments", "build.build_upsert_fragments")

SET_FRAGMENT_STATUS: Final = Query("build_set_fragment_status", "build.build_set_fragment_status")

# Domain edges of one relation: the type is rendered (a dynamic type in MATCH/MERGE scans); the caller
# checks ``Ontology.is_declared``, the factories check the safe-name pattern. Edges are found through
# the per-relation ``(group_id, edge_key)`` index.


@cache
def upsert_edges(relation: str) -> Query:
    """MERGE edges by ``row.edge_key`` between the nodes with uuids ``row.source`` and ``row.target``."""

    rel = safe_name(relation)
    return Query(f"build_upsert_edges_{rel}", "build.upsert_edges", Relation(rel))


@cache
def update_edges(relation: str) -> Query:
    """``SET += row.props`` on the edge ``row.uuid`` stored under one of ``row.keys`` (its key before
    the update, and the key the update gives it, so a batch applied again finds it). An edge without a
    ``uuid`` is matched on its key alone."""

    rel = safe_name(relation)
    return Query(f"build_update_edges_{rel}", "build.update_edges", Relation(rel))


@cache
def delete_edges(relation: str) -> Query:
    """Delete the edge ``row.uuid`` stored under ``row.edge_key`` (an edge without a ``uuid``: by key).

    Matching the uuid too keeps a batch applied again from deleting another edge that has since been
    given this key."""

    rel = safe_name(relation)
    return Query(f"build_delete_edges_{rel}", "build.delete_edges", Relation(rel))


# Verification (analysis.md "Verification"; ``graph.build.verify``). The reads return only what build
# owns: spec nodes with their build-owned properties and their build-owned labels (``$labels``),
# relationships of build-owned relations (``$relations``) and fragments; foreign labels, properties
# and relationship types are never read. The repairs address what the reads returned by element id
# (a fault may have removed a node's ``uuid`` or an edge's ``edge_key``) and re-check the project and
# the owned type, so they never touch a foreign node or relationship.

VERIFY_SPEC_NODES: Final = Query("build_verify_spec_nodes", "build.build_verify_spec_nodes")

# Domain edges touching a spec node of the project (either end may be something else: a fault).
VERIFY_DOMAIN_EDGES: Final = Query("build_verify_domain_edges", "build.build_verify_domain_edges")

# Fragments: status and hashes (never the text), and whether any spec node is grounded in them.
VERIFY_FRAGMENTS: Final = Query("build_verify_fragments", "build.build_verify_fragments")

REPAIR_DELETE_EDGES: Final = Query("build_repair_delete_edges", "build.build_repair_delete_edges")

# W4: a spec node deleted by a repair goes with its relationships.
REPAIR_DELETE_NODES: Final = Query("build_repair_delete_nodes", "build.build_repair_delete_nodes")

# ``row.remove``: the node's other build-owned labels; ``row.label``: the label it keeps (both checked
# against the ontology by the caller); ``row.label_disagreement``: its value with that label (null
# removes it).
REPAIR_SET_LABELS: Final = Query("build_repair_set_labels", "build.build_repair_set_labels")

REPAIR_SET_SPEC_IDS: Final = Query("build_repair_set_spec_ids", "build.build_repair_set_spec_ids")

# Restored evidence: one ``REFERENCED_BY`` per evidence line range, to fragments that exist.
REPAIR_NODE_EVIDENCE: Final = Query("build_repair_node_evidence", "build.build_repair_node_evidence")

REPAIR_EDGE_EVIDENCE: Final = Query("build_repair_edge_evidence", "build.build_repair_edge_evidence")


# Infer (analysis.md "Light inference"). Subjects and candidates are read through stated and implied
# edges only (``$bases``; an edge without ``basis`` counts as stated), so earlier inferred edges never
# feed new candidates (FR-INF-02). Labels are
# parameters (``$subject_labels``, ``$target_labels``); relation names and hop counts come from the
# validated ontology and are rendered. A symmetric relation is matched in either direction.


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
    either = tuple(safe_name(name) for name in symmetric)
    checked = tuple(safe_name(name) for name in via)
    name = "_".join(via) + ("_mentions" if mentions else "")
    return Query(
        f"build_infer_endpoint_subjects_{name}",
        "build.infer_endpoint_subjects",
        EndpointSubjects(checked, either, mentions),
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
        "build.infer_realization_subjects",
        RealizationSubjects(member, container, member_symmetric, container_symmetric, hops),
    )


# Spine reachability (ontology.md "Spine and inference"; ``graph.build.spine``, used by inference and
# verification): the spec nodes with their build-owned labels (``$labels``) and the edges of the spine
# relations (``$relations``) between spec nodes of the project, with their ``basis``; the paths are
# walked in memory.
READ_SPINE_NODES: Final = Query("build_read_spine_nodes", "build.build_read_spine_nodes")

READ_SPINE_EDGES: Final = Query("build_read_spine_edges", "build.build_read_spine_edges")


@cache
def infer_similar_targets(writes: str, *, symmetric: bool) -> Query:
    """Template ``attach_orphan``: for each node of ``$uuids``, the ``$limit`` target nodes of the
    project most similar to it by exact ``embedding`` cosine, leaving out those it already has a
    ``writes`` edge with (stated or implied); best first, ties by ``spec_id``, then ``uuid``. The
    project's targets are read once and scored for every subject, so the result never depends on
    other projects in the database. A node without an embedding has none."""

    rel = safe_name(writes)
    return Query(f"build_infer_similar_targets_{rel}", "build.infer_similar_targets", SimilarTargets(rel, symmetric))


@cache
def infer_exclusive_holders(writes: str) -> Query:
    """The nodes of ``$uuids`` that already have a stated or implied ``writes`` edge on one of the
    exclusive pairs ``$pairs`` (``[source label, target label]``)."""

    rel = safe_name(writes)
    return Query(f"build_infer_exclusive_holders_{rel}", "build.infer_exclusive_holders", Relation(rel))


# The nodes a judgement shows: name, domain label (``$labels``), identity key, statements and up to
# ``$per_node`` grounding fragments (``REFERENCED_BY`` to a fragment whose status is in ``$statuses``)
# in document order, by id and content hash; their texts are read only for the judged subjects.
READ_INFER_NODES: Final = Query("build_read_infer_nodes", "build.build_read_infer_nodes")

READ_INFER_FRAGMENTS: Final = Query("build_read_infer_fragments", "build.build_read_infer_fragments")


@cache
def infer_write_edges(relation: str) -> Query:
    """The swap's writes: MERGE each inferred edge ``row.edge_key`` from ``row.source`` to ``row.target``
    (``uuid`` and ``created_at`` set on creation only) and set ``row.props``; ``refs`` are the
    ``row.ref`` of the rows written. A row is skipped when an
    edge with its key exists that is not an inferred edge of the same rule (``row.rule``), or, with
    ``row.either_direction``, when the two nodes are already linked by a non-inferred edge of the
    relation in either direction."""

    rel = safe_name(relation)
    return Query(f"build_infer_write_edges_{rel}", "build.infer_write_edges", Relation(rel))


@cache
def infer_delete_edges(relation: str) -> Query:
    """The swap's deletes: the inferred edge ``row.uuid`` of rule ``row.rule`` under ``row.edge_key``;
    ``refs`` are the ``row.ref`` of the rows deleted."""

    rel = safe_name(relation)
    return Query(f"build_infer_delete_edges_{rel}", "build.infer_delete_edges", Relation(rel))
