"""Offline tests of graph store routing and the Neo4j store (design D1, D3, D4, D10).

``build_store`` routes ``StoreConfig.backend`` through a table and imports only the chosen adapter;
the three backends without an adapter yet answer ``store_not_available``; the Neo4j store wraps a
driver (deprecated arguments) or owns one. No database: drivers are hand-written fakes.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import types
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import graph.stores as stores
from graph.build import BuildContext
from graph.core import (
    ConfigurationError,
    GraphReader,
    Neo4jConfig,
    OwnedWriter,
    ensure_schema,
)
from graph.core.errors import ConformanceError
from graph.core.queries import PING
from graph.core.store import (
    GraphStore,
    StoreCapabilities,
    StoreInfo,
    as_executor,
    as_store,
    check,
    multi_statement_tx,
)
from graph.stores import BACKENDS, StoreConfig, build_store
from graph.stores._bolt.cypher import render_cypher
from graph.stores._bolt.executor import BoltExecutor
from graph.stores.neo4j import Neo4jStore
from graph_support import llm_config, shipped_ontology
from test_graph_schema import DIM, SchemaDriver

SECRET = "pw-never-in-errors-91"
BACKEND_CONFIG = {"uri": "bolt://127.0.0.1:1", "user": "neo4j", "password": SECRET}
SRC = Path(__file__).resolve().parents[1] / "src"


def config(**overrides: Any) -> StoreConfig:
    return StoreConfig(**{**BACKEND_CONFIG, **overrides})


# StoreConfig


def test_a_store_config_defaults_to_neo4j_and_hides_its_secret() -> None:
    settings = config()
    assert settings.backend == "neo4j" and settings.name is None
    assert settings.admin_uri is None and settings.pool_size is None
    assert SECRET not in repr(settings) and SECRET not in str(settings.model_dump())
    assert settings.password.get_secret_value() == SECRET


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"backend": "mysql"}, "backend"),
        ({"uri": ""}, "uri"),
        ({"user": ""}, "user"),
        ({"pool_size": 0}, "pool_size"),
        ({"admin_uri": ""}, "admin_uri"),
        ({"unknown": 1}, "unknown"),
    ],
)
def test_an_invalid_store_config_names_the_field_and_never_a_value(
    overrides: dict[str, Any], field: str
) -> None:
    with pytest.raises(ConfigurationError) as raised:
        config(**overrides)
    assert raised.value.code == "invalid_settings"
    assert raised.value.fields["fields"] == field
    assert SECRET not in str(raised.value) and "mysql" not in str(raised.value)


def test_a_store_config_is_frozen() -> None:
    with pytest.raises(ValueError, match="frozen"):
        config().uri = "x"  # type: ignore[misc]


def test_a_neo4j_config_converts_to_store_settings() -> None:
    legacy = Neo4jConfig(
        uri="bolt://h:7687", user="u", password=SecretStr(SECRET), database="graph"
    )
    converted = StoreConfig.from_neo4j(legacy)
    assert (converted.backend, converted.uri, converted.user, converted.name) == (
        "neo4j",
        "bolt://h:7687",
        "u",
        "graph",
    )
    assert converted.password.get_secret_value() == SECRET


# Routing


def test_the_routing_table_names_all_four_backends() -> None:
    assert BACKENDS == ("neo4j", "arcadedb", "postgres", "tigergraph")
    assert set(stores._ADAPTERS) == set(BACKENDS)


@pytest.mark.parametrize("backend", ["arcadedb", "postgres", "tigergraph"])
def test_a_backend_without_an_adapter_is_not_available(backend: str) -> None:
    with pytest.raises(ConfigurationError) as raised:
        build_store(config(backend=backend))
    assert raised.value.code == "store_not_available"
    assert raised.value.fields == {"store": backend}


def test_neo4j_routes_to_the_neo4j_store_without_connecting() -> None:
    store = build_store(config(name="graph"))
    assert isinstance(store, Neo4jStore) and isinstance(store, GraphStore)
    assert store.info == StoreInfo(
        "neo4j", StoreCapabilities(multi_statement_tx=True), None
    )


def test_a_neo4j_config_is_still_accepted() -> None:
    store = build_store(
        Neo4jConfig(uri="bolt://127.0.0.1:1", user="u", password=SecretStr(SECRET))
    )
    assert isinstance(store, Neo4jStore)


def test_routing_imports_only_the_chosen_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []
    real = importlib.import_module

    def spy(name: str, package: str | None = None) -> Any:
        imported.append(name)
        return real(name, package)

    monkeypatch.setattr(stores.importlib, "import_module", spy)
    build_store(config())
    assert imported == ["graph.stores.neo4j"]


def test_a_missing_sdk_is_a_configuration_error_naming_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str, package: str | None = None) -> Any:
        raise ModuleNotFoundError("No module named 'pyTigerGraph'", name="pyTigerGraph")

    monkeypatch.setattr(stores.importlib, "import_module", missing)
    with pytest.raises(ConfigurationError) as raised:
        build_store(config(backend="tigergraph"))
    assert raised.value.code == "store_sdk_missing"
    assert raised.value.fields == {"store": "tigergraph", "install_extra": "tigergraph"}


def test_any_other_import_failure_is_a_bug_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(name: str, package: str | None = None) -> Any:
        raise ModuleNotFoundError(
            "No module named 'something_else'", name="something_else"
        )

    monkeypatch.setattr(stores.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        build_store(config())


def test_an_adapter_module_builds_its_store_from_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeStore:
        @classmethod
        def from_config(cls, settings: StoreConfig) -> FakeStore:
            return cls()

    module = types.ModuleType("graph.stores.fake_adapter")
    module.FakeStore = FakeStore  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "graph.stores.fake_adapter", module)
    monkeypatch.setitem(
        stores._ADAPTERS,
        "postgres",
        stores._Adapter("graph.stores.fake_adapter", "FakeStore", "", ("sqlalchemy",)),
    )
    assert isinstance(build_store(config(backend="postgres")), FakeStore)


@pytest.mark.parametrize(
    "module",
    [
        "graph.stores",
        "graph.stores.config",
        "graph.stores.neo4j",
        "graph.stores._bolt.cypher",
        "graph.stores._bolt.executor",
        "graph.stores._bolt.schema",
        "graph.core",
        "graph.core.db",
        "graph.core.schema",
    ],
)
def test_every_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    """The shims and the stores import each other: no entry point may hit a circular import."""

    env = {**os.environ, "PYTHONPATH": str(SRC)}
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-400:]


# The Neo4j store


class Driver(SchemaDriver):
    """A schema driver that also answers readiness and records its lifecycle."""

    def __init__(self, **options: Any) -> None:
        super().__init__(shipped_ontology(), **options)
        self.closed = False
        self.verified = 0

    async def verify_connectivity(self) -> None:
        self.verified += 1

    async def execute_query(
        self, text: str, params: dict[str, Any] | None = None, **kwargs: Any
    ) -> Any:
        if text == render_cypher(PING):
            self.texts.append(text)
            return types.SimpleNamespace(records=[{"ok": 1}])
        return await super().execute_query(text, params, **kwargs)

    async def close(self) -> None:
        self.closed = True


def test_the_neo4j_store_reports_a_transactional_backend() -> None:
    store = Neo4jStore(Driver(), "graph")
    assert store.info == StoreInfo(
        "neo4j", StoreCapabilities(multi_statement_tx=True), None
    )
    assert store.executor() is store.executor()
    assert isinstance(store.executor(), BoltExecutor)
    assert store.executor().capabilities.multi_statement_tx is True


async def test_check_is_one_cheap_round_trip_and_does_not_read_the_version() -> None:
    driver = Driver()
    store = Neo4jStore(driver)
    await store.check()
    assert driver.verified == 1 and driver.texts == [render_cypher(PING)]
    assert store.info.server_version is None


async def test_ensure_schema_reads_the_version_through_the_gate() -> None:
    driver = Driver()
    driver.add_all()
    store = Neo4jStore(driver)
    assert await store.ensure_schema(shipped_ontology(), DIM) == ()
    assert store.info.server_version == "5.26.0"


async def test_ensure_schema_refuses_an_unsupported_version() -> None:
    store = Neo4jStore(Driver(version="5.25.1"))
    with pytest.raises(ConfigurationError) as raised:
        await store.ensure_schema(shipped_ontology(), DIM)
    assert raised.value.code == "neo4j_version_unsupported"
    assert store.info.server_version is None


async def test_the_module_functions_accept_a_store_or_a_driver() -> None:
    first, second = Driver(), Driver()
    for driver in (first, second):
        driver.add_all()
    created_by_store = await ensure_schema(
        Neo4jStore(first), shipped_ontology(), llm_config(embedding_dim=DIM)
    )
    created_by_driver = await ensure_schema(
        second, shipped_ontology(), llm_config(embedding_dim=DIM), "neo4j"
    )
    assert created_by_store == created_by_driver == ()
    await check(Neo4jStore(first))
    await check(second, "neo4j")
    assert first.verified == second.verified == 1


async def test_a_store_wrapping_a_driver_leaves_it_open_and_one_from_config_closes_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = Driver()
    await Neo4jStore(driver).close()
    assert driver.closed is False

    made: list[Driver] = []

    def factory(*_args: Any, **_kwargs: Any) -> Driver:
        made.append(Driver())
        return made[0]

    import graph.stores._bolt.executor as bolt

    monkeypatch.setattr(bolt.AsyncGraphDatabase, "driver", factory)
    owned = Neo4jStore.from_config(config(name="graph"))
    await owned.close()
    assert made[0].closed is True


# Consumers take a store, an executor or a driver


class FakeTransactions:
    def __init__(self) -> None:
        self.writes: list[str] = []

    async def read(
        self, query: Any, project_id: str, /, **params: Any
    ) -> list[dict[str, Any]]:
        return []

    async def write(
        self, query: Any, project_id: str, /, **params: Any
    ) -> list[dict[str, Any]]:
        self.writes.append(query.name)
        return [{"written": 1, "label_mismatch": False, "spec_id_changed": False}]

    @asynccontextmanager
    async def tx(self, project_id: str) -> Iterator[Any]:  # type: ignore[misc]
        raise AssertionError("no transaction expected")
        yield


class FakeStore:
    """A store without multi-statement transactions."""

    def __init__(self, *, transactional: bool) -> None:
        self.info = StoreInfo(
            "fake", StoreCapabilities(multi_statement_tx=transactional)
        )
        self._executor = FakeTransactions()

    def executor(self) -> FakeTransactions:
        return self._executor

    async def check(self) -> None:
        return None

    async def ensure_schema(
        self, ontology: Any, embedding_dim: int, *, ddl: Any = None
    ) -> tuple[str, ...]:
        return ("created",)

    async def close(self) -> None:
        return None


def test_a_store_an_executor_and_a_driver_resolve_to_an_executor() -> None:
    store = FakeStore(transactional=True)
    assert as_executor(store) is store.executor()
    assert as_store(store) is store
    assert as_executor(store.executor()) is store.executor()
    wrapped = as_executor(Driver(), "graph")
    assert isinstance(wrapped, BoltExecutor)


def test_capabilities_come_from_the_store_or_the_executor_else_true() -> None:
    plain, limited = FakeStore(transactional=True), FakeStore(transactional=False)
    assert multi_statement_tx(plain, plain.executor()) is True
    assert multi_statement_tx(limited, limited.executor()) is False
    bare = FakeTransactions()
    assert multi_statement_tx(bare, bare) is True
    bare.capabilities = StoreCapabilities(multi_statement_tx=False)  # type: ignore[attr-defined]
    assert multi_statement_tx(bare, bare) is False


async def test_writer_transaction_is_refused_on_a_store_without_multi_statement_transactions() -> (
    None
):
    store = FakeStore(transactional=False)
    writer = OwnedWriter(store, shipped_ontology(), "build", "proj-1")
    with pytest.raises(ConfigurationError) as raised:
        async with writer.transaction():
            raise AssertionError("the block must not run")
    assert raised.value.code == "store_transactions_unsupported"
    await writer.upsert_node(
        "Document", "doc-1", {"title": "x"}
    )  # a single operation is still fine
    assert store.executor().writes == ["upsert_node_Document"]


async def test_writer_transaction_is_refused_on_an_executor_that_says_so() -> None:
    executor = FakeTransactions()
    executor.capabilities = StoreCapabilities(multi_statement_tx=False)  # type: ignore[attr-defined]
    with pytest.raises(ConfigurationError) as raised:
        async with OwnedWriter(
            executor, shipped_ontology(), "build", "proj-1"
        ).transaction():
            raise AssertionError("the block must not run")
    assert raised.value.code == "store_transactions_unsupported"


async def test_writer_transaction_on_a_transactional_store_reaches_the_executor() -> (
    None
):
    store = FakeStore(transactional=True)
    writer = OwnedWriter(store, shipped_ontology(), "build", "proj-1")
    with pytest.raises(AssertionError, match="no transaction expected"):
        async with writer.transaction():
            raise ConformanceError("not reached")


def test_reader_and_build_context_take_a_store() -> None:
    store = FakeStore(transactional=True)
    reader = GraphReader(store, shipped_ontology())
    assert reader._executor is store.executor()
    context = BuildContext(
        project_id="proj-1",
        ontology=shipped_ontology(),
        driver=store,
        gateway=object(),
        registry=object(),
        corpus=object(),
        extraction=object(),
        builds=object(),
    )
    assert context.executor is store.executor()


def test_build_context_still_takes_a_driver_and_database() -> None:
    context = BuildContext(
        project_id="proj-1",
        ontology=shipped_ontology(),
        driver=Driver(),
        database="graph",
        gateway=object(),
        registry=object(),
        corpus=object(),
        extraction=object(),
        builds=object(),
    )
    assert isinstance(context.executor, BoltExecutor)
