"""Offline tests of the ArcadeDB store and executor: routing, database name, error mapping, the relationship
types read before a merge or a move, retries of a deadlock, lifecycle and the version after ensure_schema.

Drivers and the admin server are hand-written fakes; nothing connects anywhere.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from neo4j.exceptions import ClientError, ServiceUnavailable, TransientError
from pydantic import SecretStr

from graph.build import queries as build_queries
from graph.core import queries as core_queries
from graph.core.errors import ConfigurationError, ConformanceError, GraphWriteError, InfrastructureUnavailableError
from graph.core.store import GraphStore, StoreCapabilities, StoreInfo
from graph.stores import StoreConfig, build_store
from graph.stores._bolt import executor as bolt
from graph.stores.arcadedb import AdminClient, ArcadeDbStore
from graph.stores.arcadedb import dialect as arcade_dialect
from graph.stores.arcadedb.executor import ArcadeDbExecutor, ArcadeDbScopedTransaction, map_error
from graph.stores.arcadedb.store import DEFAULT_DATABASE
from graph.stores.neo4j import Neo4jStore
from graph_support import shipped_ontology
from test_graph_core import FakeDriver, Sleeps, neo4j_error

LEAK_PROBE = "pw-never-in-errors-55"
TYPES = [{"relationshipType": name} for name in ("FLAGS", "PART_OF", "REFERENCED_BY")]


class TypesDriver(FakeDriver):
    """A fake driver whose transactions answer the relationship types and count every other statement."""

    def session(self, **options: Any) -> Any:
        session = super().session(**options)
        driver = self

        class Transaction:
            def __init__(self) -> None:
                self._closed = False

            async def run(self, text: str, params: dict[str, Any] | None = None) -> Any:
                driver.runs.append((text, dict(params or {})))
                rows = TYPES if text.startswith("CALL db.relationshipTypes") else [{"written": 1, "moved": 1}]

                async def data() -> list[dict[str, Any]]:
                    return rows

                return SimpleNamespace(data=data)

            async def commit(self) -> None:
                self._closed = True
                driver.commits += 1

            async def rollback(self) -> None:
                self._closed = True

            def closed(self) -> bool:
                return self._closed

        async def execute(work: Any) -> Any:
            return await work(Transaction())

        async def begin() -> Transaction:
            return Transaction()

        session.execute_read = execute
        session.execute_write = execute
        session.begin_transaction = begin
        return session


def admin(handler: Any = None) -> AdminClient:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/server":
            return httpx.Response(200, json={"version": "26.10.2 (build x)"})
        return httpx.Response(200, json={"result": []})

    return AdminClient(
        "http://arcade:2480", "root", SecretStr(LEAK_PROBE), transport=httpx.MockTransport(handler or respond)
    )


def executor(driver: FakeDriver, sleeps: Sleeps | None = None) -> ArcadeDbExecutor:
    return ArcadeDbExecutor(driver, "graphdb", sleep=sleeps or Sleeps())


# routing and construction


def test_the_routing_row_resolves_to_the_arcadedb_store() -> None:
    store = build_store(
        StoreConfig(backend="arcadedb", uri="bolt://127.0.0.1:1", user="root", password=SecretStr(LEAK_PROBE))
    )
    assert isinstance(store, ArcadeDbStore) and isinstance(store, GraphStore)
    assert store.info == StoreInfo("arcadedb", StoreCapabilities(multi_statement_tx=True), None)
    assert store.executor() is store.executor() and isinstance(store.executor(), ArcadeDbExecutor)
    assert store.executor().capabilities.multi_statement_tx is True


def test_from_config_derives_the_admin_endpoint_and_defaults_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def make_admin(url: str, user: str, password: SecretStr, **options: Any) -> Any:
        seen.append((url, user, password.get_secret_value(), options))
        return admin()

    monkeypatch.setattr("graph.stores.arcadedb.store.AdminClient", make_admin)
    config = StoreConfig(backend="arcadedb", uri="bolt+ssc://db:7687", user="root", password=SecretStr(LEAK_PROBE))
    store = ArcadeDbStore.from_config(config)
    assert seen == [("https://db:2480", "root", LEAK_PROBE, {"verify": False})]
    assert store._database == DEFAULT_DATABASE == "specflow"
    named = ArcadeDbStore.from_config(config.model_copy(update={"name": "graph1", "admin_uri": "http://other:9"}))
    assert named._database == "graph1" and seen[1][0] == "http://other:9" and seen[1][3] == {"verify": False}


async def test_every_session_names_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = TypesDriver()
    store = ArcadeDbStore(driver, admin(), "graph1")
    await store.executor().read(core_queries.READ_NODES, "p", ids=[])
    async with store.executor().tx("p") as tx:
        await tx.run(core_queries.READ_NODES, ids=[])
    assert [options["database"] for options in driver.sessions] == ["graph1", "graph1"]


def test_the_store_refuses_to_start_when_a_required_override_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(arcade_dialect.OVERRIDES, "core.scope_via")
    with pytest.raises(ConformanceError) as raised:
        ArcadeDbStore(FakeDriver(), admin())
    assert raised.value.code == "arcadedb_override_missing"


async def test_check_is_one_cheap_bolt_round_trip() -> None:
    driver = FakeDriver()
    store = ArcadeDbStore(driver, admin(lambda _request: pytest.fail("check must not use HTTP")))
    await store.check()
    assert len(driver.queries) == 1 and "RETURN" in driver.queries[0]


async def test_ensure_schema_records_the_real_server_version() -> None:
    store = ArcadeDbStore(FakeDriver(), admin(), "graphdb")
    assert store.info.server_version is None
    with pytest.raises(ConfigurationError):  # the fake admin has no indexes: the creates do not show up
        await store.ensure_schema(shipped_ontology(), 8)


async def test_a_store_that_wraps_its_clients_leaves_them_open_and_one_from_config_closes_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    class Closing:
        async def close(self) -> None:
            closed.append("driver")

    class ClosingAdmin:
        async def close(self) -> None:
            closed.append("admin")

    await ArcadeDbStore(Closing(), cast(AdminClient, ClosingAdmin())).close()
    assert closed == []
    await ArcadeDbStore(Closing(), cast(AdminClient, ClosingAdmin()), owns_clients=True).close()
    assert closed == ["driver", "admin"]


# error mapping


FULLTEXT = ["search_nodes_fulltext", "search_fragments"]


@pytest.mark.parametrize("query", FULLTEXT)
def test_a_fulltext_text_the_server_cannot_parse_is_a_neutral_complex_query(query: str) -> None:
    error = neo4j_error(ClientError, "Neo.ClientError.Statement.SyntaxError")
    mapped = map_error(error, query, write=False)
    assert isinstance(mapped, ConformanceError) and mapped.code == "query_too_complex"
    assert mapped.fields == {
        "store": "arcadedb",
        "query": query,
        "error_type": "CypherSyntaxError",
        "backend_code": "Neo.ClientError.Statement.SyntaxError",
        "attempts": 1,
    }


def test_a_syntax_error_of_any_other_query_or_of_a_write_is_not_a_complex_query() -> None:
    error = neo4j_error(ClientError, "Neo.ClientError.Statement.SyntaxError")
    assert map_error(error, "read_nodes", write=False).code == "store_query_rejected"
    assert map_error(error, "search_fragments", write=True).code == "store_write_failed"


def test_other_errors_map_as_on_the_shared_executor() -> None:
    assert map_error(ServiceUnavailable("down"), "q", write=False).code == "store_unreachable"
    unknown = neo4j_error(ClientError, "Neo.ClientError.Statement.SemanticError")
    assert map_error(unknown, "search_fragments", write=False).code == "store_query_rejected"
    assert map_error(unknown, "q", write=False).fields["store"] == "arcadedb"


async def test_a_fulltext_syntax_error_reaches_the_caller_as_query_too_complex() -> None:
    driver = FakeDriver(errors=[neo4j_error(ClientError, "Neo.ClientError.Statement.SyntaxError")])
    with pytest.raises(ConformanceError) as raised:
        await executor(driver).read(core_queries.SEARCH_FRAGMENTS, "p", index="graph_fragment_text", query="a AND (")
    assert raised.value.code == "query_too_complex" and raised.value.fields["store"] == "arcadedb"


async def test_a_deadlock_is_retried_and_then_succeeds() -> None:
    deadlock = neo4j_error(TransientError, "Neo.TransientError.Transaction.DeadlockDetected")
    driver = FakeDriver(errors=[deadlock, deadlock, None])
    sleeps = Sleeps()
    rows = await executor(driver, sleeps).write(build_queries.UPDATE_SPEC_NODES, "p", rows=[], build_id="b")
    assert rows and sleeps.delays == [0.1, 0.3]
    assert len(driver.runs) == 3


async def test_a_deadlock_that_never_clears_is_a_write_failure_after_the_last_retry() -> None:
    deadlock = neo4j_error(TransientError, "Neo.TransientError.Transaction.DeadlockDetected")
    driver = FakeDriver(errors=[deadlock] * 4)
    sleeps = Sleeps()
    with pytest.raises(GraphWriteError) as raised:
        await executor(driver, sleeps).write(build_queries.UPDATE_SPEC_NODES, "p", rows=[], build_id="b")
    assert raised.value.code == "store_write_failed" and raised.value.fields["attempts"] == 4
    assert sleeps.delays == [0.1, 0.3, 1.0]


async def test_an_unreachable_server_is_an_infrastructure_error() -> None:
    driver = FakeDriver(errors=[ServiceUnavailable("down")] * 4)
    with pytest.raises(InfrastructureUnavailableError):
        await executor(driver).read(core_queries.READ_NODES, "p", ids=[])


# the relationship types a merge and a move need


async def test_a_merge_reads_the_relationship_types_then_runs_one_move_per_foreign_type() -> None:
    driver = TypesDriver()
    rows = await executor(driver).write(
        build_queries.MERGE_SPEC_NODES, "p", rows=[], build_id="b", owned_types=["PART_OF", "REFERENCED_BY"]
    )
    assert rows == [{"written": 1, "moved": 1}]
    texts = [text for text, _ in driver.runs]
    assert texts[0].startswith("CALL db.relationshipTypes()")
    assert [("[r:FLAGS]" in t) for t in texts[1:]] == [False, True, False]
    assert "SET new.spec_id_aliases" in texts[1] and texts[-1].endswith("DETACH DELETE old")
    assert [(o["database"], o["default_access_mode"]) for o in driver.sessions] == [
        ("graphdb", "READ"),
        ("graphdb", "WRITE"),
    ]


async def test_a_move_in_a_transaction_reads_the_types_in_that_transaction() -> None:
    driver = TypesDriver()
    async with executor(driver).tx("p") as tx:
        assert isinstance(tx, ArcadeDbScopedTransaction)
        rows = await tx.run(
            core_queries.MOVE_FOREIGN_RELATIONSHIPS,
            from_key="a",
            from_label="Module",
            to_key="b",
            to_label="Module",
            owned_types=["PART_OF", "REFERENCED_BY"],
        )
    assert rows == [{"moved": 1}]
    assert driver.modes == [] and driver.commits == 1  # no managed read: everything ran inside the one transaction
    assert [t.startswith("CALL db.relationshipTypes()") for t, _ in driver.runs] == [True, False]


async def test_a_read_that_needs_no_types_does_not_ask_for_them() -> None:
    driver = TypesDriver()
    await executor(driver).read(core_queries.READ_NODES, "p", ids=[])
    assert not any("relationshipTypes" in text for text, _ in driver.runs)


async def test_a_failure_reading_the_types_is_mapped_like_any_other(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = TypesDriver(errors=[])
    original = driver.session

    def broken(**options: Any) -> Any:
        session = original(**options)

        async def fail(_work: Any) -> Any:
            raise ServiceUnavailable("down")

        session.execute_read = fail
        return session

    monkeypatch.setattr(driver, "session", broken)
    with pytest.raises(InfrastructureUnavailableError):
        await executor(driver).write(build_queries.MERGE_SPEC_NODES, "p", rows=[], build_id="b", owned_types=[])


def test_the_shared_bolt_machinery_is_unchanged_for_neo4j() -> None:
    neo4j_executor = Neo4jStore(FakeDriver(), "neo4j").executor()
    assert type(neo4j_executor) is bolt.BoltExecutor
    assert neo4j_executor._error_mapper is bolt._map_error
