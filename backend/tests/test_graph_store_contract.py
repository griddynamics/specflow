"""The store contract suite: what every graph store adapter must do, offline (design D1, D8, D10, D11).

One suite over every harness of ``graph_store_contract.HARNESSES`` (today the Neo4j/Bolt adapter over a fake
driver): the project-id guard, error classes with neutral codes, the retry policy, transactions (or one
commit per statement when ``multi_statement_tx`` is false), rows as plain data and the property payloads
(``None`` removes, lists keep their order, scalars keep their type, no map is ever written by the
library). A new adapter adds its harness and runs the same tests.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

import graph.build.queries as build_queries
import graph.core.queries as core_queries
from graph.core import GraphWriteError, NodeRef, OwnedWriter, Query, load_ontology
from graph.core.errors import ConfigurationError, ConformanceError, GraphError
from graph.core.store import RETRY_DELAYS_SECONDS, STORE_TRANSACTIONS_UNSUPPORTED, Executor
from graph_store_contract import (
    CANARY,
    CHECK_FAILURES,
    EXPECTED,
    FAILURES,
    HARNESSES,
    AdapterHarness,
)

PID = "proj-contract-1"
SCOPED_QUERIES = sorted(
    {
        value.name: value
        for module in (core_queries, build_queries)
        for value in vars(module).values()
        if isinstance(value, Query) and value.project_scoped
    }.values(),
    key=lambda q: q.name,
)
UNSCOPED = core_queries.PING


TRANSACTIONAL = sorted(name for name, make in HARNESSES.items() if make().capabilities.multi_statement_tx)
PER_STATEMENT = sorted(name for name in HARNESSES if name not in TRANSACTIONAL)


@pytest.fixture(params=sorted(HARNESSES))
def harness(request: pytest.FixtureRequest) -> AdapterHarness:
    return HARNESSES[request.param]()


@pytest.fixture(params=TRANSACTIONAL)
def transactional(request: pytest.FixtureRequest) -> AdapterHarness:
    """A harness of a store with multi-statement transactions (none: its tests are skipped)."""

    return HARNESSES[request.param]()


@pytest.fixture(params=PER_STATEMENT)
def per_statement(request: pytest.FixtureRequest) -> AdapterHarness:
    """A harness of a store that commits every statement on its own (none yet: its tests are skipped)."""

    return HARNESSES[request.param]()


def executor(harness: AdapterHarness) -> Executor:
    return harness.store().executor()


def one_query() -> Query:
    return SCOPED_QUERIES[0]


class RollbackError(Exception):
    """Raised by a test inside a transaction block."""


# Project-id guard ---------------------------------------------------------------------------------


def test_the_library_has_scoped_queries_to_run() -> None:
    assert len(SCOPED_QUERIES) > 20


@pytest.mark.parametrize("bad", ["", "Bad name", "a;b", "x) OR 1=1 (n", "a" * 500, None, 7])
async def test_an_invalid_project_id_is_refused_before_the_backend(harness: AdapterHarness, bad: Any) -> None:
    ex = executor(harness)
    for call in (ex.read, ex.write):
        with pytest.raises(ConfigurationError) as raised:
            await call(one_query(), bad)
        assert raised.value.code == "invalid_project_id"
    with pytest.raises(ConfigurationError):
        async with ex.tx(bad):
            pass
    assert harness.statements == [] and harness.events == []


async def test_an_unscoped_query_and_a_caller_project_id_are_refused(harness: AdapterHarness) -> None:
    ex = executor(harness)
    with pytest.raises(ConformanceError) as raised:
        await ex.read(UNSCOPED, PID)
    assert raised.value.code == "query_not_project_scoped"
    with pytest.raises(ConformanceError) as raised:
        await ex.write(one_query(), PID, project_id="other")
    assert raised.value.code == "project_id_parameter_reserved"
    assert harness.statements == []


@pytest.mark.parametrize("query", SCOPED_QUERIES, ids=lambda q: q.name)
async def test_every_scoped_library_query_reaches_the_backend_bound_to_the_project(
    harness: AdapterHarness, query: Query
) -> None:
    ex = executor(harness)
    await ex.read(query, PID)
    await ex.write(query, PID)
    assert [(s.mode, s.params["project_id"]) for s in harness.statements] == [("read", PID), ("write", PID)]


async def test_a_transaction_binds_every_statement_to_its_project(harness: AdapterHarness) -> None:
    async with executor(harness).tx(PID) as tx:
        await tx.run(SCOPED_QUERIES[0])
        await tx.run(SCOPED_QUERIES[1], key="k")
        with pytest.raises(ConformanceError):
            await tx.run(SCOPED_QUERIES[1], project_id="other")
    assert [s.params["project_id"] for s in harness.statements] == [PID, PID]
    assert harness.statements[1].params["key"] == "k"


# Error classes and neutral codes ---------------------------------------------------------------------


@pytest.mark.parametrize("kind", FAILURES)
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
async def test_a_backend_failure_maps_to_a_neutral_error_carrying_the_store(
    harness: AdapterHarness, kind: str, *, write: bool
) -> None:
    ex = executor(harness)
    harness.fail(kind)
    cls, code = EXPECTED[kind][1 if write else 0]
    with pytest.raises(GraphError) as raised:
        await (ex.write if write else ex.read)(one_query(), PID)
    assert type(raised.value) is cls and raised.value.code == code
    assert raised.value.fields["store"] == harness.backend
    assert CANARY not in str(raised.value) and CANARY not in json.dumps(raised.value.fields, default=str)
    assert len(harness.statements) == 1  # none of these is retried


@pytest.mark.parametrize("kind", FAILURES)
async def test_a_failure_inside_a_transaction_maps_like_a_write(harness: AdapterHarness, kind: str) -> None:
    harness.fail(kind)
    cls, code = EXPECTED[kind][1]
    with pytest.raises(GraphError) as raised:
        async with executor(harness).tx(PID) as tx:
            await tx.run(one_query())
    assert type(raised.value) is cls and raised.value.code == code
    assert raised.value.fields["store"] == harness.backend and CANARY not in str(raised.value)


@pytest.mark.parametrize("kind", FAILURES)
async def test_a_failure_inside_a_transaction_rolls_it_back(transactional: AdapterHarness, kind: str) -> None:
    transactional.fail(kind)
    with pytest.raises(GraphError):
        async with executor(transactional).tx(PID) as tx:
            await tx.run(one_query())
    assert transactional.events[-1] == "rollback" and "commit" not in transactional.events


@pytest.mark.parametrize("kind", CHECK_FAILURES)
async def test_check_maps_connection_failures_to_neutral_codes(harness: AdapterHarness, kind: str) -> None:
    harness.fail_check(kind)
    cls, code = EXPECTED[kind][0]
    with pytest.raises(GraphError) as raised:
        await harness.store().check()
    assert type(raised.value) is cls and raised.value.code == code
    assert raised.value.fields["store"] == harness.backend and CANARY not in str(raised.value)
    await harness.store().check()  # and a healthy backend passes


# Retry policy --------------------------------------------------------------------------------------


def test_the_default_retry_delays_are_the_design_policy() -> None:
    assert RETRY_DELAYS_SECONDS == (0.1, 0.3, 1.0)


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
async def test_a_transient_failure_is_retried_after_each_delay_then_succeeds(
    harness: AdapterHarness, *, write: bool
) -> None:
    harness.answer([{"ok": 1}])
    harness.fail("transient", "transient", None)
    rows = await (executor(harness).write if write else executor(harness).read)(one_query(), PID)
    assert rows == [{"ok": 1}]
    assert harness.sleeps == list(RETRY_DELAYS_SECONDS[:2])
    assert len(harness.statements) == 3


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
async def test_a_transient_failure_after_the_last_retry_is_mapped_with_the_attempt_count(
    harness: AdapterHarness, *, write: bool
) -> None:
    attempts = len(RETRY_DELAYS_SECONDS) + 1
    harness.fail(*["transient"] * (attempts + 2))
    cls, code = EXPECTED["server_error"][1 if write else 0]
    with pytest.raises(GraphError) as raised:
        await (executor(harness).write if write else executor(harness).read)(one_query(), PID)
    assert type(raised.value) is cls and raised.value.code == code
    assert raised.value.fields["attempts"] == attempts
    assert harness.sleeps == list(RETRY_DELAYS_SECONDS) and len(harness.statements) == attempts


@pytest.mark.parametrize("kind", FAILURES)
async def test_a_permanent_failure_is_never_retried(harness: AdapterHarness, kind: str) -> None:
    harness.fail(kind, None)
    with pytest.raises(GraphError):
        await executor(harness).read(one_query(), PID)
    assert harness.sleeps == [] and len(harness.statements) == 1


async def test_an_explicit_transaction_is_not_retried(transactional: AdapterHarness) -> None:
    transactional.fail("transient")
    with pytest.raises(GraphWriteError):
        async with executor(transactional).tx(PID) as tx:
            await tx.run(one_query())
    assert transactional.sleeps == [] and len(transactional.statements) == 1


# Transactions (design D10) --------------------------------------------------------------------------


async def test_a_transaction_commits_its_statements_together_when_the_block_ends(
    transactional: AdapterHarness,
) -> None:
    async with executor(transactional).tx(PID) as tx:
        await tx.run(SCOPED_QUERIES[0])
        await tx.run(SCOPED_QUERIES[1])
        assert "commit" not in transactional.events
    assert transactional.events == ["begin", "statement", "statement", "commit"]


async def run_then_raise(harness: AdapterHarness) -> None:
    async with executor(harness).tx(PID) as tx:
        await tx.run(SCOPED_QUERIES[0])
        raise RollbackError


async def test_a_transaction_rolls_back_when_the_block_raises(transactional: AdapterHarness) -> None:
    with pytest.raises(RollbackError):
        await run_then_raise(transactional)
    assert transactional.events == ["begin", "statement", "rollback"]


async def test_a_store_without_multi_statement_transactions_commits_every_statement(
    per_statement: AdapterHarness,
) -> None:
    async with executor(per_statement).tx(PID) as tx:
        await tx.run(SCOPED_QUERIES[0])
        await tx.run(SCOPED_QUERIES[1])
    assert per_statement.events == ["begin", "statement", "commit", "begin", "statement", "commit"]


async def test_the_writer_transaction_opens_on_a_transactional_store(transactional: AdapterHarness) -> None:
    store = transactional.store()
    assert store.info.backend == transactional.backend and store.info.capabilities.multi_statement_tx
    async with OwnedWriter(store, load_ontology(), "build", PID).transaction():
        pass
    assert transactional.events == ["begin", "commit"]


async def test_the_writer_transaction_is_refused_on_a_per_statement_store(per_statement: AdapterHarness) -> None:
    writer = OwnedWriter(per_statement.store(), load_ontology(), "build", PID)
    with pytest.raises(ConfigurationError) as raised:
        async with writer.transaction():
            pass
    assert raised.value.code == STORE_TRANSACTIONS_UNSUPPORTED


# Rows ---------------------------------------------------------------------------------------------


def assert_plain(value: Any) -> None:
    assert type(value) in (str, int, float, bool, type(None), list, dict), type(value)
    if isinstance(value, list):
        for item in value:
            assert_plain(item)
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            assert_plain(item)


@pytest.mark.parametrize("query", SCOPED_QUERIES, ids=lambda q: q.name)
async def test_rows_come_back_as_plain_dicts_exactly_as_the_backend_answered(
    harness: AdapterHarness, query: Query
) -> None:
    answered = [
        {"uuid": "u1", "n": 3, "score": 0.25, "ok": True, "none": None, "ids": ["b", "a"], "nested": {"k": [1]}}
    ]
    harness.answer(answered)
    ex = executor(harness)
    for rows in (await ex.read(query, PID), await ex.write(query, PID)):
        assert rows == answered and rows is not answered
        assert all(type(row) is dict for row in rows)
        assert_plain(rows)
        assert list(rows[0]) == list(answered[0])  # column order kept
    harness.answer([])
    assert await ex.read(query, PID) == []


# Property payloads (design D7) ----------------------------------------------------------------------

WRITTEN = {"written": 1, "label_mismatch": False, "spec_id_changed": False, "conflict": False}
PAYLOAD = {
    "text": "x",
    "count": 3,
    "ratio": 0.5,
    "flag": True,
    "off": False,
    "zero": 0,
    "names": ["z", "a", "m"],
    "numbers": [3, 1, 2],
    "removed": None,
    "empty": [],
}


async def test_property_payloads_reach_the_backend_unchanged(harness: AdapterHarness) -> None:
    harness.answer([WRITTEN])
    writer = OwnedWriter(harness.store(), load_ontology(), "build", PID)
    await writer.upsert_node("Module", "u-1", {"spec_id": "MOD::A", **PAYLOAD})
    sent = harness.statements[-1].params["props"]
    assert list(sent) == ["spec_id", *PAYLOAD]  # keys in the order given
    assert sent["removed"] is None and "removed" in sent  # None travels: the statement removes the property
    assert sent["names"] == ["z", "a", "m"] and sent["numbers"] == [3, 1, 2] and sent["empty"] == []
    for name, value in PAYLOAD.items():
        assert type(sent[name]) is type(value), name
    assert sent["flag"] is True and sent["off"] is False and sent["zero"] == 0 and sent["zero"] is not False
    await writer.upsert_relationship("INTEGRATES_WITH", _ref("Module", "u-1"), _ref("Module", "u-2"), "k", PAYLOAD)
    edge = harness.statements[-1].params["props"]
    assert list(edge) == list(PAYLOAD) and edge["removed"] is None and edge["names"] == ["z", "a", "m"]
    assert all(type(edge[name]) is type(value) for name, value in PAYLOAD.items())


async def test_the_library_writes_no_map_valued_property(harness: AdapterHarness) -> None:
    harness.answer([WRITTEN])
    writer = OwnedWriter(harness.store(), load_ontology(), "build", PID)
    await writer.upsert_node("Module", "u-1", {"spec_id": "MOD::A", "name": "A", "stmt": "s", "external": "no"})
    await writer.upsert_relationship(
        "INTEGRATES_WITH", _ref("Module", "u-1"), _ref("Module", "u-2"), "k", {"basis": "stated", "fact": "f"}
    )
    for executed in harness.statements:
        props = executed.params.get("props", {})
        assert not [name for name, value in props.items() if isinstance(value, dict)]


def _ref(label: str, key: str) -> NodeRef:
    return NodeRef(label, key)


def test_every_query_harness_declares_its_backend_and_capabilities() -> None:
    for name, factory in HARNESSES.items():
        built = factory()
        assert built.backend == name or built.backend
        assert isinstance(built.capabilities.multi_statement_tx, bool)
        assert built.store().info.backend == built.backend


def test_the_contract_lists_a_mapping_for_every_failure_kind() -> None:
    kinds: Sequence[str] = FAILURES
    assert set(EXPECTED) == set(kinds)
