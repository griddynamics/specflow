"""Persist step: apply a diff to Neo4j in batches (build.md step 7; tech-specs 5.4 "Persist batches").

Operations are cut into batches of at most ``BATCH_OPERATIONS`` in diff order (which is persist phase
order: removed edges, merges, updates and carry-overs, node deletes, creates, edges and structural
relationships, documents and fragments). Each batch is one transaction; inside it, each run of
consecutive operations of one kind (and one relation) is one ``UNWIND`` query. Every write MERGEs on a
key, replaces a whole set, or finds its target by uuid (edges: by key and uuid, an updated edge by its
old or new key), so a batch applied twice leaves the same graph: after an interruption, the batch after
the stored cursor is applied again. The cursor is saved by the caller through ``save`` after every batch.

A merge (W3) moves every relationship of a merged node whose type build does not own to the survivor,
then deletes the merged node; a carry-over keeps the node (the new target takes its uuid), so its
relationships stay where they are and it is written like an update.

Node operations carry their vectors as text hashes (``vectors``); before a batch runs, the vectors of
its operations are fetched in one call (``fetch_vectors``, the build's vector store) and written as
float lists under their property names, so at most one batch of vectors is in memory at a time.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from graph.build.models import (
    DiffArtifact,
    DocumentStatusChange,
    DocumentUpsert,
    EdgeCreate,
    EdgeDelete,
    EdgeUpdate,
    FragmentStatusChange,
    FragmentUpsert,
    NodeCarryOver,
    NodeCreate,
    NodeDelete,
    NodeLinks,
    NodeMerge,
    NodeUpdate,
    Operation,
    PersistCursor,
)
from graph.build.queries import (
    CREATE_SPEC_NODES,
    DELETE_SPEC_NODES,
    MERGE_SPEC_NODES,
    REPLACE_NODE_LINKS,
    SET_DOCUMENT_STATUS,
    SET_FRAGMENT_STATUS,
    UPDATE_SPEC_NODES,
    UPSERT_DOCUMENTS,
    UPSERT_FRAGMENTS,
    delete_edges,
    update_edges,
    upsert_edges,
)
from graph.core.db import Executor, ScopedTransaction
from graph.core.errors import ConformanceError
from graph.core.ontology import BUILT_IN_RELATIONS, Ontology
from graph.core.queries import Query
from logger.logger import INFO, get_logger, log_event
from logger.progress import ProgressSampler

logger = get_logger("graph.build.persist")

BATCH_OPERATIONS: Final = 500
BUILD_OWNER: Final = "build"

Row = dict[str, Any]
SaveCursor = Callable[[PersistCursor], Awaitable[None]]
OnBatch = Callable[[int], None]
FetchVectors = Callable[[Collection[str]], Awaitable[Mapping[str, np.ndarray]]]
"""Vectors by ``text_hash`` (every hash asked for, or it raises)."""
Vectors = Mapping[str, np.ndarray]


@dataclass(frozen=True)
class PersistTarget:
    """Where a persist writes: the project's transactions (``executor``) and the build stamping the writes."""

    executor: Executor
    project_id: str
    build_id: str


def batches(operations: Sequence[Operation], size: int = BATCH_OPERATIONS) -> list[tuple[Operation, ...]]:
    """Consecutive slices of at most ``size`` operations."""

    return [tuple(operations[start : start + size]) for start in range(0, len(operations), size)]


async def persist(
    diff: DiffArtifact,
    cursor: PersistCursor,
    *,
    target: PersistTarget,
    ontology: Ontology,
    save: SaveCursor,
    fetch_vectors: FetchVectors,
    on_batch: OnBatch | None = None,
    batch_size: int = BATCH_OPERATIONS,
) -> PersistCursor:
    """Apply the batches after ``cursor.batch_index``; return the final cursor (``done``).

    ``fetch_vectors`` returns the vectors of the text hashes the batch's node operations name.

    ``save`` stores the cursor after each committed batch; ``on_batch(index)`` runs after that (a
    test hook for fault injection). A cursor that is already ``done`` writes nothing. ``batch_size``
    must be the same for every run of one diff (the cursor counts batches). ``target`` gives the
    executor, project and build.
    """

    executor, project_id, build_id = target.executor, target.project_id, target.build_id
    if cursor.done:
        return cursor
    planned = batches(diff.operations, batch_size)
    owned = _owned_relationship_types(ontology)
    sampler = ProgressSampler(len(planned), every=10, fraction=0.10)
    for index in range(cursor.batch_index + 1, len(planned)):
        vectors = await _batch_vectors(planned[index], fetch_vectors)
        async with executor.tx(project_id) as transaction:
            for query, rows, strict in _groups(planned[index], ontology, vectors):
                params = {"owned_types": owned} if query is MERGE_SPEC_NODES else {}
                await _run(transaction, query, rows, strict=strict, build_id=build_id, params=params)
        cursor = PersistCursor(batch_index=index, batches=len(planned), done=False)
        await save(cursor)
        if on_batch is not None:
            on_batch(index)
        if sampler.should_log(index + 1):
            log_event(
                logger, INFO, "graph.persist.progress", build_id=build_id, batches_done=index + 1, batches=len(planned)
            )
    cursor = PersistCursor(batch_index=len(planned) - 1, batches=len(planned), done=True)
    await save(cursor)
    return cursor


def _owned_relationship_types(ontology: Ontology) -> list[str]:
    """Relationship types build owns: the built-in structural ones and its ontology relations."""

    return sorted(BUILT_IN_RELATIONS | {name for name in ontology.relations if ontology.owner_of(name) == BUILD_OWNER})


async def _run(
    transaction: ScopedTransaction, query: Query, rows: list[Row], *, strict: bool, build_id: str, params: Row
) -> None:
    records = await transaction.run(query, rows=rows, build_id=build_id, **params)
    written = int(records[0]["written"]) if records else 0
    if strict and written != len(rows):
        raise ConformanceError("persist_target_missing", query=query.name, rows=len(rows), written=written)


async def _batch_vectors(batch: Sequence[Operation], fetch: FetchVectors) -> Vectors:
    digests = {
        digest
        for operation in batch
        if isinstance(operation, NodeCreate | NodeUpdate | NodeCarryOver)
        for digest in operation.vectors.values()
    }
    if not digests:
        return {}
    found = await fetch(digests)
    if not digests <= found.keys():
        raise ConformanceError("vector_missing", missing=len(digests - found.keys()))
    return found


def _vector_properties(operation: NodeCreate | NodeUpdate | NodeCarryOver, vectors: Vectors) -> Row:
    return {name: vectors[digest].tolist() for name, digest in operation.vectors.items()}


def _groups(batch: Sequence[Operation], ontology: Ontology, vectors: Vectors) -> list[tuple[Query, list[Row], bool]]:
    """Runs of consecutive operations sharing one query, as ``(query, rows, strict)``; ``strict``: every
    row must find what it writes to (creates, upserts, node updates and merges, whose node stays; an
    edge update or a delete applied twice finds less)."""

    groups = []
    for (query, strict), members in itertools.groupby(
        ((_route(operation, ontology), operation) for operation in batch), key=lambda pair: pair[0]
    ):
        groups.append((query, [_row(operation, vectors) for _, operation in members], strict))
    return groups


def _route_nodes(operation: Operation, ontology: Ontology) -> tuple[Query, bool] | None:
    if isinstance(operation, NodeCreate):
        _require_owned(ontology, operation.label)
        return CREATE_SPEC_NODES, True
    if isinstance(operation, NodeMerge):
        return MERGE_SPEC_NODES, True
    if isinstance(operation, NodeUpdate | NodeCarryOver):
        return UPDATE_SPEC_NODES, True
    if isinstance(operation, NodeDelete):
        return DELETE_SPEC_NODES, False
    return None


def _route_edges(operation: Operation, ontology: Ontology) -> tuple[Query, bool] | None:
    if isinstance(operation, EdgeCreate):
        return upsert_edges(_require_owned(ontology, operation.relation)), True
    if isinstance(operation, EdgeUpdate):
        return update_edges(_require_owned(ontology, operation.relation)), False
    if isinstance(operation, EdgeDelete):
        return delete_edges(_require_owned(ontology, operation.relation)), False
    return None


def _route_structure(operation: Operation) -> tuple[Query, bool] | None:
    if isinstance(operation, NodeLinks):
        return REPLACE_NODE_LINKS, True
    if isinstance(operation, DocumentUpsert):
        return UPSERT_DOCUMENTS, True
    if isinstance(operation, DocumentStatusChange):
        return SET_DOCUMENT_STATUS, False
    if isinstance(operation, FragmentUpsert):
        return UPSERT_FRAGMENTS, True
    if isinstance(operation, FragmentStatusChange):
        return SET_FRAGMENT_STATUS, False
    return None


def _route(operation: Operation, ontology: Ontology) -> tuple[Query, bool]:
    routed = _route_nodes(operation, ontology) or _route_edges(operation, ontology) or _route_structure(operation)
    if routed is None:
        raise ConformanceError("persist_operation_not_implemented", op=operation.op)
    return routed


def _require_owned(ontology: Ontology, name: str) -> str:
    """``name`` when the ontology declares it as a build-owned label or relation."""

    if not ontology.is_declared(name) or ontology.owner_of(name) != BUILD_OWNER:
        raise ConformanceError("persist_name_not_owned", name=name)
    return name


def _node_row(operation: Operation, vectors: Vectors) -> Row | None:
    if isinstance(operation, NodeCreate):
        props = {**operation.properties, **_vector_properties(operation, vectors), "uuid": operation.uuid}
        return {"uuid": operation.uuid, "label": operation.label, "props": props}
    if isinstance(operation, NodeMerge):
        return {
            "survivor": operation.survivor_uuid,
            "merged": list(operation.merged_uuids),
            "aliases": list(operation.aliases),
        }
    if isinstance(operation, NodeUpdate | NodeCarryOver):
        return {"uuid": operation.uuid, "props": {**operation.properties, **_vector_properties(operation, vectors)}}
    if isinstance(operation, NodeDelete):
        return {"uuid": operation.uuid}
    if isinstance(operation, NodeLinks):
        return {
            "uuid": operation.uuid,
            "evidence": [lines.model_dump(mode="json") for lines in operation.evidence],
            "mentions": [{"fragment_id": fid, "count": count} for fid, count in operation.mentions.items()],
        }
    return None


def _edge_row(operation: Operation) -> Row | None:
    if isinstance(operation, EdgeCreate):
        return {
            "source": operation.source_uuid,
            "target": operation.target_uuid,
            "edge_key": operation.edge_key,
            "props": {**operation.properties, "uuid": operation.uuid},
        }
    if isinstance(operation, EdgeUpdate):
        rekeyed = operation.properties.get("edge_key")
        keys = [operation.edge_key, *([rekeyed] if isinstance(rekeyed, str) and rekeyed != operation.edge_key else [])]
        return {"uuid": operation.uuid, "keys": keys, "props": dict(operation.properties)}
    if isinstance(operation, EdgeDelete):
        return {"edge_key": operation.edge_key, "uuid": operation.uuid}
    return None


def _structure_row(operation: Operation) -> Row | None:
    if isinstance(operation, DocumentUpsert):
        document = operation.document
        return {
            "document_id": document.document_id,
            "props": {
                "spec_id": operation.spec_id,
                "revision_id": document.revision_id,
                "version": document.version,
                "path": document.path,
                "title": document.title,
                "status": document.status,
            },
        }
    if isinstance(operation, DocumentStatusChange):
        return {"document_id": operation.document_id, "status": operation.status}
    if isinstance(operation, FragmentUpsert):
        fragment = operation.fragment
        return {
            "fragment_id": fragment.fragment_id,
            "document_id": fragment.document_id,
            "props": {
                "document_id": fragment.document_id,
                "spec_id": operation.spec_id,
                "text": fragment.text,
                "heading_path": list(fragment.heading_path),
                "ordinal": fragment.ordinal,
                "line_start": fragment.line_start,
                "line_end": fragment.line_end,
                "content_hash": fragment.content_hash,
                "status": fragment.status,
                "extracted_hash": fragment.extracted_hash,
                "error_code": fragment.error_code,
            },
        }
    if isinstance(operation, FragmentStatusChange):
        return {"fragment_id": operation.fragment_id, "status": operation.status, "error_code": operation.error_code}
    return None


def _row(operation: Operation, vectors: Vectors) -> Row:
    row = _node_row(operation, vectors) or _edge_row(operation) or _structure_row(operation)
    if row is None:
        raise ConformanceError("persist_operation_not_implemented", op=operation.op)
    return row
