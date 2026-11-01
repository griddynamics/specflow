"""Offline tests of the ArcadeDB row chunking (D1) and of the plan of ``infer_similar_targets`` (D2).

A fake driver answers each statement from its ``rows`` or ``uuids`` parameter, so the tests see which statements
ran, in which transaction, with how many rows, and what the combined result is.
"""

from __future__ import annotations

import array
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from neo4j.exceptions import ClientError, TransientError

from graph.build import queries as build_queries
from graph.core.dialect import ExecutionPlan, PlannedStatement
from graph.core.errors import ConformanceError, GraphWriteError
from graph.stores.arcadedb import chunking
from graph.stores.arcadedb.chunking import MAX_MESSAGE_BYTES, chunked, combine, estimate_bytes, split_rows
from graph.stores.arcadedb.dialect import ARCADEDB_DIALECT, SIMILAR_SUBJECTS_PER_STATEMENT
from graph.stores.arcadedb.executor import ArcadeDbExecutor
from test_graph_core import FakeDriver, Sleeps, neo4j_error

VECTOR = [0.5] * 3072  # 27,648 bytes estimated
VECTOR_ROW_BYTES = 2 * estimate_bytes(VECTOR)


def vector_row(index: int) -> dict[str, Any]:
    return {
        "uuid": f"u-{index}",
        "label": "Feature",
        "props": {"uuid": f"u-{index}", "embedding": VECTOR, "name_embedding": VECTOR},
    }


def plain_row(index: int) -> dict[str, Any]:
    return {"uuid": f"u-{index}", "label": "Feature", "props": {"uuid": f"u-{index}", "name": f"n{index}"}}


# the estimate and the split


def test_the_estimate_counts_nine_bytes_per_float_and_the_text_of_strings() -> None:
    assert estimate_bytes(VECTOR) == 5 + 3072 * 9
    assert estimate_bytes("abc") == 5 + 3
    assert estimate_bytes("é") == 5 + 2  # UTF-8 bytes, not characters
    assert estimate_bytes({"k": [1.0, 2.0]}) == 5 + estimate_bytes("k") + 5 + 18
    flag: Any = True
    assert estimate_bytes(None) == estimate_bytes(flag) == 9  # never below the real size


def test_a_batch_of_500_vector_rows_is_cut_below_the_message_budget() -> None:
    rows = [vector_row(index) for index in range(500)]
    chunks = split_rows(rows)
    assert len(chunks) > 1
    assert all(sum(estimate_bytes(row) for row in chunk) <= MAX_MESSAGE_BYTES for chunk in chunks)
    assert max(len(chunk) for chunk in chunks) == MAX_MESSAGE_BYTES // estimate_bytes(rows[0])  # as full as allowed
    assert [row for chunk in chunks for row in chunk] == rows  # order kept, nothing lost or repeated


def test_rows_without_vectors_stay_in_one_chunk_up_to_the_row_cap() -> None:
    rows = [plain_row(index) for index in range(chunking.MAX_ROWS_PER_MESSAGE)]
    assert split_rows(rows) == [rows]
    assert [len(chunk) for chunk in split_rows([*rows, plain_row(999)])] == [chunking.MAX_ROWS_PER_MESSAGE, 1]


def test_a_single_row_over_the_budget_is_a_chunk_of_its_own() -> None:
    huge = {"uuid": "big", "props": {"embedding": [0.1] * (MAX_MESSAGE_BYTES // 9 + 10)}}
    chunks = split_rows([plain_row(1), huge, plain_row(2)])
    assert chunks == [[plain_row(1)], [huge], [plain_row(2)]]


def test_no_rows_is_no_chunk() -> None:
    assert split_rows([]) == []


def test_the_limits_can_be_given() -> None:
    assert [len(c) for c in split_rows([plain_row(i) for i in range(5)], max_rows=2)] == [2, 2, 1]
    assert [
        len(c) for c in split_rows([vector_row(i) for i in range(5)], max_bytes=estimate_bytes(vector_row(0)) * 2)
    ] == [2, 2, 1]


# combining the chunk results


def test_counts_add_up_and_lists_concatenate_in_chunk_order() -> None:
    results = [
        [{"written": 2, "refs": ["a", "b"]}],
        [{"written": 3, "refs": ["c"]}],
        [{"written": 0, "refs": []}],
    ]
    assert combine(results) == [{"written": 5, "refs": ["a", "b", "c"]}]
    assert results[0] == [{"written": 2, "refs": ["a", "b"]}]  # the inputs are not changed


def test_a_chunk_without_a_row_makes_the_rows_concatenate() -> None:
    assert combine([[], []]) == []
    assert combine([[{"x": 1}, {"x": 2}], [], [{"x": 3}]]) == [{"x": 1}, {"x": 2}, {"x": 3}]


def test_a_flag_is_not_added_like_a_count() -> None:
    assert combine([[{"ok": True, "n": 1}], [{"ok": False, "n": 1}]]) == [{"ok": True, "n": 2}]


# the plan


def statements(*row_lists: Sequence[Any]) -> ExecutionPlan:
    return ExecutionPlan(
        tuple(PlannedStatement(f"S{index}", {"rows": list(rows), "k": index}) for index, rows in enumerate(row_lists))
    )


def test_a_plan_with_nothing_to_cut_is_returned_unchanged() -> None:
    plan = statements([plain_row(1)], [])
    assert chunked(plan) is plan
    single = ExecutionPlan.single("MATCH (n) RETURN n", {"x": 1})  # no rows parameter at all
    assert chunked(single) is single


def test_each_statement_is_cut_in_place_and_keeps_its_other_parameters() -> None:
    plan = chunked(
        statements([plain_row(i) for i in range(5)], [plain_row(9)], [plain_row(i) for i in range(3)]), max_rows=2
    )
    assert [(s.text, [r["uuid"] for r in s.params["rows"]], s.params["k"]) for s in plan.statements] == [
        ("S0", ["u-0", "u-1"], 0),
        ("S0", ["u-2", "u-3"], 0),
        ("S0", ["u-4"], 0),
        ("S1", ["u-9"], 1),
        ("S2", ["u-0", "u-1"], 2),
        ("S2", ["u-2"], 2),
    ]


def test_the_plan_reducer_sees_one_combined_result_per_original_statement() -> None:
    seen: list[Sequence[list[dict[str, Any]]]] = []

    def reduce(results: Sequence[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        seen.append(results)
        return [{"written": sum(rows[0]["written"] for rows in results if rows)}]

    base = statements([plain_row(i) for i in range(5)], [plain_row(9)])
    plan = chunked(ExecutionPlan(base.statements, reduce), max_rows=2)
    results = [[{"written": 2}], [{"written": 2}], [{"written": 1}], [{"written": 1}]]
    assert plan.reduce(results) == [{"written": 6}]
    assert seen == [[[{"written": 5}], [{"written": 1}]]]


# through the dialect


def create_plan(rows: list[dict[str, Any]]) -> ExecutionPlan:
    return ARCADEDB_DIALECT.plan(build_queries.CREATE_SPEC_NODES, {"project_id": "p", "build_id": "b", "rows": rows})


def test_the_dialect_cuts_a_create_batch_per_label_and_per_budget_in_order() -> None:
    rows = [{**vector_row(i), "label": "Module" if i % 5 == 0 else "Feature"} for i in range(200)]
    plan = create_plan(rows)
    labels = [("Module" if "SpecNode:Module" in s.text else "Feature") for s in plan.statements]
    assert labels == sorted(labels)  # the per-label statements keep their order (Feature, then Module)
    assert all(sum(estimate_bytes(row) for row in s.params["rows"]) <= MAX_MESSAGE_BYTES for s in plan.statements)
    by_label = {
        label: [
            r["uuid"]
            for s, name in zip(plan.statements, labels, strict=True)
            if name == label
            for r in s.params["rows"]
        ]
        for label in ("Feature", "Module")
    }
    assert by_label["Feature"] == [r["uuid"] for r in rows if r["label"] == "Feature"]
    assert by_label["Module"] == [r["uuid"] for r in rows if r["label"] == "Module"]
    assert plan.reduce([[{"written": len(s.params["rows"])}] for s in plan.statements]) == [{"written": 200}]


@pytest.mark.parametrize(
    "query",
    [
        build_queries.UPDATE_SPEC_NODES,
        build_queries.upsert_edges("PART_OF"),
        build_queries.infer_write_edges("PART_OF"),
        build_queries.infer_delete_edges("PART_OF"),
    ],
)
def test_every_template_with_rows_is_cut_the_same_way(query: Any) -> None:
    rows = [
        {"uuid": f"u-{i}", "props": {"embedding": VECTOR, "name_embedding": VECTOR}, "ref": f"r{i}"} for i in range(200)
    ]
    params: dict[str, Any] = {"project_id": "p", "build_id": "b", "rows": rows, "inferred": "inferred"}
    plan = ARCADEDB_DIALECT.plan(query, params)
    assert len(plan.statements) > 1
    assert [r["uuid"] for s in plan.statements for r in s.params["rows"]] == [r["uuid"] for r in rows]
    assert len({s.text for s in plan.statements}) == 1  # the same statement, only the rows differ


def test_refs_of_the_chunks_concatenate_in_row_order_through_the_plan() -> None:
    rows = [
        {"uuid": f"u-{i}", "ref": f"r{i}", "props": {"embedding": VECTOR, "name_embedding": VECTOR}} for i in range(200)
    ]
    plan = ARCADEDB_DIALECT.plan(
        build_queries.infer_delete_edges("PART_OF"), {"project_id": "p", "inferred": "inferred", "rows": rows}
    )
    answers = [
        [{"written": len(s.params["rows"]), "refs": [r["ref"] for r in s.params["rows"]]}] for s in plan.statements
    ]
    assert plan.reduce(answers) == [{"written": 200, "refs": [f"r{i}" for i in range(200)]}]


# through the executor: the same transaction


class RowsDriver(FakeDriver):
    """Transactions answer ``written`` = rows of the statement; the driver notes each statement's transaction."""

    def __init__(self) -> None:
        super().__init__()
        self.transactions: list[int] = []
        self.opened = 0
        self.managed = 0
        self.calls = 0
        self.fail_at: dict[int, Exception] = {}  # the n-th statement run (1-based, all transactions) raises

    def session(self, **options: Any) -> Any:
        session = super().session(**options)
        driver = self

        class Transaction:
            def __init__(self) -> None:
                driver.opened += 1
                self.number = driver.opened
                self._closed = False

            async def run(self, text: str, params: Mapping[str, Any] | None = None) -> Any:
                driver.runs.append((text, dict(params or {})))
                driver.transactions.append(self.number)
                driver.calls += 1
                if driver.calls in driver.fail_at:
                    raise driver.fail_at[driver.calls]
                given = params or {}
                rows = (
                    [{"uuid": uuid, "candidates": []} for uuid in given["uuids"]]
                    if "uuids" in given
                    else [{"written": len(given.get("rows", ()))}]
                )

                async def data() -> list[dict[str, Any]]:
                    return rows

                return SimpleNamespace(data=data)

            async def commit(self) -> None:
                self._closed = True
                driver.commits += 1

            async def rollback(self) -> None:
                self._closed = True
                driver.rollbacks += 1

            def closed(self) -> bool:
                return self._closed

        async def execute(work: Any) -> Any:
            driver.managed += 1
            return await work(Transaction())

        async def begin() -> Transaction:
            return Transaction()

        session.execute_read = execute
        session.execute_write = execute
        session.begin_transaction = begin
        return session


def batch() -> list[dict[str, Any]]:
    return [vector_row(index) for index in range(500)]


async def test_a_managed_write_runs_every_chunk_in_one_managed_transaction() -> None:
    driver = RowsDriver()
    executor = ArcadeDbExecutor(driver, "db", sleep=Sleeps())
    written = await executor.write(build_queries.CREATE_SPEC_NODES, "p", rows=batch(), build_id="b")
    assert written == [{"written": 500}]
    assert len(driver.runs) > 1 and driver.managed == 1 and set(driver.transactions) == {1}
    assert [row["uuid"] for _, params in driver.runs for row in params["rows"]] == [f"u-{i}" for i in range(500)]


async def test_an_explicit_transaction_runs_every_chunk_in_it_and_commits_once() -> None:
    driver = RowsDriver()
    executor = ArcadeDbExecutor(driver, "db", sleep=Sleeps())
    async with executor.tx("p") as tx:
        first = await tx.run(build_queries.CREATE_SPEC_NODES, rows=batch(), build_id="b")
        second = await tx.run(build_queries.UPDATE_SPEC_NODES, rows=batch())
    assert first == second == [{"written": 500}]
    assert driver.opened == 1 and set(driver.transactions) == {1} and driver.commits == 1 and driver.managed == 0
    assert len(driver.runs) > 2


def first_uuids(driver: RowsDriver) -> list[str]:
    """The first row uuid of each statement the driver ran, in order: which chunk each run was."""

    return [params["rows"][0]["uuid"] for _, params in driver.runs]


async def test_a_transient_failure_after_the_first_chunk_reruns_every_chunk_from_the_start() -> None:
    driver = RowsDriver()
    driver.fail_at = {2: neo4j_error(TransientError, "Neo.TransientError.Transaction.DeadlockDetected")}
    sleeps = Sleeps()
    executor = ArcadeDbExecutor(driver, "db", sleep=sleeps)
    written = await executor.write(build_queries.CREATE_SPEC_NODES, "p", rows=batch(), build_id="b")
    chunks = split_rows(batch())
    starts = [chunk[0]["uuid"] for chunk in chunks]
    assert len(chunks) > 2 and written == [{"written": 500}]
    # attempt 1: chunk 1 ran, chunk 2 raised; attempt 2: all chunks again, in order, in a new managed transaction
    assert first_uuids(driver) == [starts[0], starts[1], *starts]
    assert driver.managed == 2 and driver.transactions[:2] == [1, 1] and set(driver.transactions[2:]) == {2}
    assert len(sleeps.delays) == 1


async def test_a_chunk_that_fails_in_an_explicit_transaction_rolls_it_back_without_a_commit() -> None:
    driver = RowsDriver()
    driver.fail_at = {2: neo4j_error(ClientError, "Neo.ClientError.Statement.SyntaxError")}
    executor = ArcadeDbExecutor(driver, "db", sleep=Sleeps())
    with pytest.raises(GraphWriteError) as raised:
        async with executor.tx("p") as tx:
            await tx.run(build_queries.CREATE_SPEC_NODES, rows=batch(), build_id="b")
    assert raised.value.code == "store_write_failed"
    starts = [chunk[0]["uuid"] for chunk in split_rows(batch())]
    assert first_uuids(driver) == starts[:2]  # chunk 3 never ran
    assert driver.rollbacks == 1 and driver.commits == 0 and set(driver.transactions) == {1}


async def test_an_earlier_statement_of_the_transaction_is_rolled_back_with_the_failed_chunk() -> None:
    driver = RowsDriver()
    driver.fail_at = {3: neo4j_error(ClientError, "Neo.ClientError.Statement.SyntaxError")}
    executor = ArcadeDbExecutor(driver, "db", sleep=Sleeps())
    with pytest.raises(GraphWriteError):
        async with executor.tx("p") as tx:
            await tx.run(build_queries.UPDATE_SPEC_NODES, rows=[plain_row(1)])  # statement 1, a single message
            await tx.run(build_queries.CREATE_SPEC_NODES, rows=batch(), build_id="b")  # statements 2 and 3 (fails)
    assert driver.rollbacks == 1 and driver.commits == 0 and len(driver.runs) == 3


# the multi-step merge plan: every step is chunked, the guard still refuses


BIG = "x" * 1_500_000  # two rows per chunk


def merge_rows() -> list[dict[str, Any]]:
    return [{"survivor": f"s-{i}", "merged": [f"m-{i}"], "aliases": [BIG]} for i in range(6)]


def merge_plan() -> ExecutionPlan:
    params = {"project_id": "p", "build_id": "b", "owned_types": ["PART_OF", "REFERENCED_BY"], "rows": merge_rows()}
    return ARCADEDB_DIALECT.plan(build_queries.MERGE_SPEC_NODES, params, ["FLAGS", "PART_OF", "REFERENCED_BY"])


def merge_answers(plan: ExecutionPlan, left: Mapping[int, int]) -> list[list[dict[str, Any]]]:
    """Fake results by step: the aliases step counts its rows, the guard answers ``left`` for its n-th chunk."""

    answers: list[list[dict[str, Any]]] = []
    guard_chunks = 0
    for statement in plan.statements:
        if "spec_id_aliases" in statement.text:
            answers.append([{"written": len(statement.params["rows"])}])
        elif "AS left" in statement.text:
            guard_chunks += 1
            answers.append([{"left": left.get(guard_chunks, 0)}])
        else:
            answers.append([])  # a move or the delete returns nothing
    return answers


def test_the_merge_plan_chunks_every_step_in_step_order() -> None:
    plan = merge_plan()
    kinds = [
        "aliases"
        if "spec_id_aliases" in s.text
        else "guard"
        if "AS left" in s.text
        else "delete"
        if "DETACH DELETE" in s.text
        else "move"
        for s in plan.statements
    ]
    assert kinds == ["aliases"] * 3 + ["move"] * 3 + ["guard"] * 3 + ["delete"] * 3
    for step in ("aliases", "move", "guard", "delete"):
        rows = [
            r["survivor"]
            for s, kind in zip(plan.statements, kinds, strict=True)
            if kind == step
            for r in s.params["rows"]
        ]
        assert rows == [f"s-{i}" for i in range(6)]  # each step sees all rows once, in order


def test_the_merge_reducer_returns_the_summed_written_of_the_aliases_step() -> None:
    plan = merge_plan()
    assert plan.reduce(merge_answers(plan, {})) == [{"written": 6}]


def test_a_guard_hit_in_the_second_chunk_refuses_the_merge() -> None:
    plan = merge_plan()
    with pytest.raises(ConformanceError) as raised:
        plan.reduce(merge_answers(plan, {2: 1}))
    assert raised.value.code == "arcadedb_unrenderable_edge_type"


# the size estimate of other sequences


def test_the_estimate_handles_tuples_arrays_and_numpy_by_length() -> None:
    expected = estimate_bytes(VECTOR)
    assert estimate_bytes(tuple(VECTOR)) == expected
    assert estimate_bytes(array.array("f", VECTOR)) == expected
    assert estimate_bytes(np.array(VECTOR, dtype=np.float32)) == expected
    assert estimate_bytes(range(3072)) == expected
    assert estimate_bytes(b"abc") == 5 + 3
    assert estimate_bytes({"props": {"embedding": np.zeros(3072, dtype=np.float32)}}) == estimate_bytes(
        {"props": {"embedding": VECTOR}}
    )


# the plan of infer_similar_targets (D2)


def similar_plan(uuids: list[str], writes: str = "PART_OF", *, symmetric: bool = False) -> ExecutionPlan:
    params = {"project_id": "p", "uuids": uuids, "target_labels": ["Feature"], "bases": ["stated"], "limit": 5}
    return ARCADEDB_DIALECT.plan(build_queries.infer_similar_targets(writes, symmetric=symmetric), params)


def test_similar_targets_leave_out_the_final_sort_and_use_the_shared_score_scale() -> None:
    (statement,) = similar_plan(["u-1"]).statements
    assert "ORDER BY uuid" not in statement.text  # the sort that held every subject's target list in heap
    assert "(1 + vector.similarity.cosine(n.embedding, c.embedding)) / 2 AS score" in statement.text
    assert "ORDER BY score DESC, c.spec_id, c.uuid LIMIT $limit" in statement.text  # top-k per subject unchanged
    assert statement.text.count("UNWIND $uuids") == 1 and "WHERE n.embedding IS NOT NULL" in statement.text


def test_similar_targets_run_the_subjects_in_chunks_with_the_same_other_parameters() -> None:
    uuids = [f"u-{index:04d}" for index in range(2 * SIMILAR_SUBJECTS_PER_STATEMENT + 7)]
    plan = similar_plan(uuids)
    assert [len(s.params["uuids"]) for s in plan.statements] == [
        SIMILAR_SUBJECTS_PER_STATEMENT,
        SIMILAR_SUBJECTS_PER_STATEMENT,
        7,
    ]
    assert [u for s in plan.statements for u in s.params["uuids"]] == uuids
    assert all(s.params["limit"] == 5 and s.params["target_labels"] == ["Feature"] for s in plan.statements)
    assert len({s.text for s in plan.statements}) == 1


def test_similar_targets_without_subjects_still_plan_one_statement() -> None:
    (statement,) = similar_plan([]).statements
    assert statement.params["uuids"] == []


def test_similar_targets_come_back_ordered_by_uuid_whatever_the_chunk_order() -> None:
    plan = similar_plan(["u-3", "u-1", "u-2"])
    rows = [{"uuid": "u-3", "candidates": []}, {"uuid": "u-1", "candidates": [{"uuid": "x", "score": 0.9}]}]
    assert [row["uuid"] for row in plan.reduce([rows, [{"uuid": "u-2", "candidates": []}]])] == ["u-1", "u-2", "u-3"]
    assert plan.reduce([[]]) == []


def test_symmetric_similar_targets_keep_the_undirected_pattern() -> None:
    (statement,) = similar_plan(["u-1"], symmetric=True).statements
    assert "(n)-[x:PART_OF]-(c)" in statement.text


async def test_similar_targets_run_all_chunks_in_one_read_transaction() -> None:
    driver = RowsDriver()
    executor = ArcadeDbExecutor(driver, "db", sleep=Sleeps())
    uuids = [f"u-{index:04d}" for index in range(120)]
    found = await executor.read(
        build_queries.infer_similar_targets("PART_OF", symmetric=False),
        "p",
        uuids=uuids,
        target_labels=["Feature"],
        bases=["stated"],
        limit=5,
    )
    assert len(driver.runs) == 3 and driver.managed == 1 and set(driver.transactions) == {1}
    assert [row["uuid"] for row in found] == uuids
