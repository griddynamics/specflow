"""Persist batching offline (build.md step 7; tech-specs 5.4 "Persist batches"; FR-BLD-13 groundwork).

``persist.batches`` cuts the diff into batches of at most ``BATCH_OPERATIONS`` in diff (phase) order;
``persist.persist`` applies each batch in one transaction, saves the cursor after it and resumes after
the stored cursor. A fake executor records every transaction, so no Neo4j is needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from graph.build import persist as writing
from graph.build.models import (
    PERSIST_PHASES,
    DiffArtifact,
    DocumentRec,
    DocumentUpsert,
    EdgeCreate,
    EdgeDelete,
    EvidenceLines,
    FragmentRec,
    FragmentUpsert,
    NodeCreate,
    NodeDelete,
    NodeLinks,
    NodeUpdate,
    Operation,
    PersistCursor,
)
from graph.build.queries import CREATE_SPEC_NODES, UPSERT_FRAGMENTS
from graph.core.errors import ConformanceError
from graph_support import shipped_ontology

ONT = shipped_ontology()
PROJECT = "p-persist"
LIMIT = writing.BATCH_OPERATIONS


def fragment(index: int) -> FragmentRec:
    return FragmentRec(
        fragment_id=f"doc#s{index}",
        document_id="doc",
        heading_path=("Doc",),
        ordinal=index,
        line_start=index + 1,
        line_end=index + 1,
        text=f"line {index}",
        content_hash=f"h{index}",
        tokens=2,
    )


def operations() -> list[Operation]:
    """A diff of every M2 operation kind, in persist phase order, over three batches."""

    document = DocumentRec(document_id="doc", revision_id="r1", version=1, path="doc.md", title="Doc")
    ops: list[Operation] = []
    ops += [EdgeDelete(relation="PART_OF", edge_key=f"old-{i}", uuid=f"oe{i}") for i in range(10)]
    ops += [
        NodeUpdate(uuid=f"u{i}", spec_id=f"S{i}", label="FunctionalRequirement", properties={"name": f"n{i}"})
        for i in range(5)
    ]
    ops += [NodeDelete(uuid=f"d{i}", spec_id=f"D{i}") for i in range(5)]
    ops += [
        NodeCreate(
            uuid=f"n{i}",
            spec_id=f"REQ::{i}",
            label="FunctionalRequirement",
            properties={"name": f"node {i}"},
            vectors={"name_embedding": f"vec{i % 7}"},
        )
        for i in range(700)
    ]
    ops += [
        EdgeCreate(
            uuid=f"e{i}",
            relation="PART_OF",
            edge_key=f"edge-{i}",
            source_uuid=f"n{i}",
            target_uuid=f"n{i + 1}",
            properties={"basis": "stated"},
        )
        for i in range(300)
    ]
    ops += [
        NodeLinks(uuid=f"n{i}", evidence=(EvidenceLines(fragment_id="doc#s0", line_start=1, line_end=1),))
        for i in range(200)
    ]
    ops += [DocumentUpsert(document=document, spec_id="DOC::doc")]
    ops += [FragmentUpsert(fragment=fragment(i), spec_id=f"FRAG::{i}") for i in range(31)]
    return ops


def identity(row: Mapping[str, Any]) -> str:
    for key in ("edge_key", "uuid", "fragment_id", "document_id"):
        if key in row:
            return str(row[key])
    raise AssertionError(row)


def op_identity(operation: Operation) -> str:
    for key in ("edge_key", "uuid", "fragment", "document"):
        value = getattr(operation, key, None)
        if value is not None:
            return str(getattr(value, f"{key}_id", value))
    raise AssertionError(operation)


@dataclass
class FakeExecutor:
    """Records each transaction as a list of ``(query name, rows)``; ``short`` makes one query write less."""

    transactions: list[list[tuple[str, list[dict[str, Any]]]]] = field(default_factory=list)
    short: str | None = None

    @asynccontextmanager
    async def tx(self, project_id: str) -> AsyncIterator[Any]:
        assert project_id == PROJECT
        runs: list[tuple[str, list[dict[str, Any]]]] = []
        executor = self

        class Transaction:
            async def run(self, query: Any, *, rows: list[dict[str, Any]], build_id: str) -> list[dict[str, Any]]:
                assert build_id == "b1"
                runs.append((query.name, rows))
                written = len(rows) - 1 if query.name == executor.short else len(rows)
                return [{"written": written}]

        yield Transaction()
        self.transactions.append(runs)


@dataclass
class Recorder:
    saved: list[PersistCursor] = field(default_factory=list)
    applied: list[int] = field(default_factory=list)
    fetched: list[set[str]] = field(default_factory=list)

    async def save(self, cursor: PersistCursor) -> None:
        self.saved.append(cursor)

    def on_batch(self, index: int) -> None:
        self.applied.append(index)

    async def fetch(self, digests: Collection[str]) -> Mapping[str, np.ndarray]:
        self.fetched.append(set(digests))
        return {digest: np.ones(3) for digest in digests}


async def run_persist(
    diff: DiffArtifact, cursor: PersistCursor, executor: FakeExecutor, recorder: Recorder
) -> PersistCursor:
    return await writing.persist(
        diff,
        cursor,
        target=writing.PersistTarget(executor=executor, project_id=PROJECT, build_id="b1"),
        ontology=ONT,
        save=recorder.save,
        fetch_vectors=recorder.fetch,
        on_batch=recorder.on_batch,
    )


def test_batches_hold_at_most_500_operations_and_keep_phase_order() -> None:
    ops = operations()
    planned = writing.batches(ops)
    assert [len(batch) for batch in planned] == [LIMIT, LIMIT, len(ops) - 2 * LIMIT]
    assert [operation for batch in planned for operation in batch] == ops
    ranks = [PERSIST_PHASES.index(operation.phase) for batch in planned for operation in batch]
    assert ranks == sorted(ranks)
    assert writing.batches([]) == []
    assert [len(batch) for batch in writing.batches(ops[:LIMIT])] == [LIMIT]


async def test_persist_applies_each_batch_in_one_transaction_in_diff_order() -> None:
    diff = DiffArtifact(operations=tuple(operations()))
    executor, recorder = FakeExecutor(), Recorder()
    final = await run_persist(diff, PersistCursor(), executor, recorder)

    assert final == PersistCursor(batch_index=2, batches=3, done=True)
    assert [sum(len(rows) for _, rows in runs) for runs in executor.transactions] == [LIMIT, LIMIT, 252]
    applied = [identity(row) for runs in executor.transactions for _, rows in runs for row in rows]
    assert applied == [op_identity(operation) for operation in diff.operations]
    # consecutive operations of one kind share one query per transaction
    first = [name for name, _ in executor.transactions[0]]
    assert len(first) == len(set(first))
    # the cursor is saved after every committed batch, then once more as done
    assert [(cursor.batch_index, cursor.done) for cursor in recorder.saved] == [
        (0, False),
        (1, False),
        (2, False),
        (2, True),
    ]
    assert recorder.applied == [0, 1, 2]
    # vectors are fetched per batch, only for that batch's node operations
    assert len(recorder.fetched) == 2  # the third batch has no node create
    assert all(digests <= {f"vec{i}" for i in range(7)} for digests in recorder.fetched)
    node_rows = [
        row for runs in executor.transactions for name, rows in runs if name == CREATE_SPEC_NODES.name for row in rows
    ]
    assert node_rows and all(row["props"]["name_embedding"] == [1.0, 1.0, 1.0] for row in node_rows)


async def test_persist_resumes_after_the_cursor_and_a_done_cursor_writes_nothing() -> None:
    diff = DiffArtifact(operations=tuple(operations()))
    executor, recorder = FakeExecutor(), Recorder()
    final = await run_persist(diff, PersistCursor(batch_index=1, batches=3), executor, recorder)
    assert final.done and recorder.applied == [2]
    assert [sum(len(rows) for _, rows in runs) for runs in executor.transactions] == [252]

    executor, recorder = FakeExecutor(), Recorder()
    assert await run_persist(diff, final, executor, recorder) == final
    assert (executor.transactions, recorder.saved, recorder.applied, recorder.fetched) == ([], [], [], [])


async def test_a_create_that_finds_nothing_to_write_to_fails_the_batch() -> None:
    diff = DiffArtifact(operations=tuple(operations()[-31:]))
    executor, recorder = FakeExecutor(short=UPSERT_FRAGMENTS.name), Recorder()
    with pytest.raises(ConformanceError) as raised:
        await run_persist(diff, PersistCursor(), executor, recorder)
    assert raised.value.code == "persist_target_missing"
    assert recorder.saved == [] and recorder.applied == []
