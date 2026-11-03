"""Offline tests of the PostgreSQL graph store: dialect and template coverage, parameter conversion, the
``:project_id`` guard, SQLSTATE mapping, retry, schema coverage, URL building. No database."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, cast

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

import graph.build.queries as build_queries
import graph.core.queries as core_queries
from graph.core.errors import (
    ConfigurationError,
    ConformanceError,
    GraphWriteError,
    InfrastructureUnavailableError,
)
from graph.core.queries import NodeShape, Query
from graph.core.store import Executor, GraphStore, StoreCapabilities, StoreInfo, as_executor, is_store
from graph.stores import StoreConfig, build_store
from graph.stores._bolt import cypher
from graph.stores.postgres import PostgresDialect, PostgresStore
from graph.stores.postgres import dialect as pg_dialect
from graph.stores.postgres import executor as pg_executor
from graph.stores.postgres.dialect import (
    BUILD_FRAGMENT_FILES,
    FRAGMENT_FILES,
    json_text,
    parameter_value,
    referenced_parameters,
    render_schema,
    templates,
    vector_text,
)
from graph.stores.postgres.executor import (
    PostgresExecutor,
    connection_url,
    is_transient,
    map_error,
    sqlstate,
)
from graph.stores.postgres.schema import parse_ddl_statement, schema_objects
from graph.stores.postgres.sqlfiles import BUILD_SQL_DIRECTORY, CORE_SQL_DIRECTORY, load_ddl_sql, split_statements
from graph_support import load_gap_ontology, shipped_ontology

CANARY: Final = "canary-password-value"
CORE_FILES: Final = sorted(path.name for path in CORE_SQL_DIRECTORY.glob("*.sql"))
BUILD_FILES: Final = sorted(path.name for path in BUILD_SQL_DIRECTORY.glob("*.sql"))
# What the Cypher dialect renders from a literal or DDL text: PostgreSQL has no Cypher.
CYPHER_ONLY: Final = frozenset({"core.ddl", "core.literal"})


def config(**overrides: Any) -> StoreConfig:
    values: dict[str, Any] = {
        "backend": "postgres",
        "uri": "postgresql://127.0.0.1:1/db",
        "user": "u",
        "password": SecretStr(CANARY),
        "name": "graph",
    }
    return StoreConfig(**{**values, **overrides})


# Template coverage


def cypher_templates(catalog: str) -> set[str]:
    found = {name for name in (*cypher._STATIC, *cypher._RENDERERS) if name.startswith(f"{catalog}.")}
    return found - CYPHER_ONLY


def test_every_template_of_the_catalog_has_a_sql_file() -> None:
    """The coverage test of design D2: no core or build template the catalog declares is missing for PostgreSQL."""

    declared = cypher_templates("core") | cypher_templates("build")
    assert declared, "the catalog declares no template?"
    assert declared - templates() == set()
    assert templates() - declared == set()
    for template in declared:
        catalog, _, stem = template.partition(".")
        folder = CORE_SQL_DIRECTORY if catalog == "core" else BUILD_SQL_DIRECTORY
        assert (folder / f"{stem}.sql").is_file(), template


def test_the_sql_folders_hold_exactly_the_templates_fragments_and_settings() -> None:
    core = {f"core.{name.removesuffix('.sql')}" for name in CORE_FILES if name not in FRAGMENT_FILES}
    build = {f"build.{name.removesuffix('.sql')}" for name in BUILD_FILES if name not in BUILD_FRAGMENT_FILES}
    assert core | build == templates()
    assert set(CORE_FILES) >= FRAGMENT_FILES and set(BUILD_FILES) >= BUILD_FRAGMENT_FILES


def test_the_catalog_has_core_and_build_templates_of_which_postgres_runs_all_but_the_cypher_only_ones() -> None:
    all_core = {name for name in (*cypher._STATIC, *cypher._RENDERERS) if name.startswith("core.")}
    all_build = {name for name in (*cypher._STATIC, *cypher._RENDERERS) if name.startswith("build.")}
    assert (len(all_core), len(all_build)) == (42, 42)
    assert len(templates()) == 40 + 42
    assert (all_core | all_build) - templates() == CYPHER_ONLY


def catalog_queries() -> list[Query]:
    queries = [value for value in vars(core_queries).values() if isinstance(value, Query)]
    spec = NodeShape("SpecNode", "uuid", check_label=True)
    queries += [
        core_queries.scope_via(("PART_OF",), 1, 3),
        core_queries.scope_sequence(("GROUPED_BY", "REALIZED_BY"), ("REALIZED_BY",)),
        core_queries.upsert_relationship("PART_OF", spec, spec),
        core_queries.upsert_relationship("MENTIONED_IN", spec, NodeShape("Fragment", "fragment_id")),
        core_queries.upsert_relationship(
            "CONTAINS", NodeShape("Document", "document_id"), NodeShape("Fragment", "fragment_id")
        ),
        core_queries.upsert_relationship("ABOUT", NodeShape("Finding", "uuid"), spec),
        core_queries.delete_relationship("PART_OF"),
        core_queries.delete_keyed_node("Document", "document_id"),
        core_queries.delete_keyed_node("Fragment", "fragment_id"),
        core_queries.delete_keyed_node("Finding", "uuid"),
        core_queries.keyed_node_relationship_types("Document", "document_id"),
        core_queries.upsert_foreign_node("Finding"),
    ]
    queries += [value for value in vars(build_queries).values() if isinstance(value, Query)]
    queries += [
        build_queries.read_current_edges("PART_OF"),
        build_queries.upsert_edges("PART_OF"),
        build_queries.update_edges("PART_OF"),
        build_queries.delete_edges("PART_OF"),
        build_queries.infer_exclusive_holders("REALIZED_BY"),
        build_queries.infer_write_edges("REALIZED_BY"),
        build_queries.infer_delete_edges("REALIZED_BY"),
        build_queries.infer_endpoint_subjects(("PART_OF", "RELATES_TO"), ("RELATES_TO",), mentions=True),
        build_queries.infer_endpoint_subjects(("PART_OF",), (), mentions=False),
        build_queries.infer_realization_subjects("GROUPED_BY", "PART_OF*1..2", ("PART_OF",), member_symmetric=False),
        build_queries.infer_realization_subjects("GROUPED_BY", "PART_OF>PROVIDED_BY", (), member_symmetric=True),
        build_queries.infer_similar_targets("REALIZED_BY", symmetric=True),
    ]
    return [query for query in queries if query.template not in CYPHER_ONLY]


def params_for(text: str) -> dict[str, Any]:
    """A value for every parameter a statement binds (the library's own names)."""

    values: dict[str, Any] = dict.fromkeys(referenced_parameters(text), "x")
    values.update(
        project_id="p",
        props={"a": 1},
        where={},
        labels=["L"],
        vector=[0.5, 0.25],
        limit=3,
        skip=0,
        page=5,
        k=10,
        terms=["a"],
        max_hops=3,
        timeout_s=1,
        include_inferred=True,
    )
    return values


def test_every_catalog_query_plans_one_statement_binding_the_project_id() -> None:
    dialect = PostgresDialect()
    for query in catalog_queries():
        text = dialect.render(query, {"vector": [0.5, 0.25], "k": 10, "query": "a b", "terms": ["a"]})
        params = dict.fromkeys(referenced_parameters(text), "x")
        params.update({"vector": [0.5, 0.25], "k": 10, "query": "a b", "terms": ["a"], "project_id": "p"})
        plan = dialect.plan(query, params)
        main = plan.statements[-1]
        assert "project_id" in main.params or not query.project_scoped, query.name
        assert re.search(r"__[A-Z]+__", main.text) is None, query.name  # no token or slot left unfilled
        assert "/*__" not in main.text, query.name


# The guard and the plans


def test_a_scoped_statement_binds_the_project_id_and_unscoped_ones_do_not_need_it() -> None:
    dialect = PostgresDialect()
    plan = dialect.plan(
        core_queries.READ_NODES,
        {"project_id": "p", "labels": ["L"], "where": {"a": "b"}, "defaulted": {"a": ["L"]}, "retired": "r"},
    )
    assert plan.statements[0].params == {
        "project_id": "p",
        "labels": ["L"],
        "where": '{"a":"b"}',
        "defaulted": '{"a":["L"]}',
        "retired": "r",
    }
    ping = dialect.plan(core_queries.PING, {})
    assert ping.statements[0].params == {}


def test_a_scoped_template_that_does_not_reference_the_project_id_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(pg_dialect._STATIC, "core.ping", lambda: "SELECT 1 AS ok")
    with pytest.raises(ConformanceError) as raised:
        PostgresDialect().plan(Query("ping_scoped", "core.ping"), {"project_id": "p"})
    assert raised.value.code == "query_missing_project_id"
    assert raised.value.fields["query"] == "ping_scoped"


def test_a_missing_parameter_is_named_and_extra_parameters_are_dropped() -> None:
    dialect = PostgresDialect()
    with pytest.raises(ConformanceError) as raised:
        dialect.plan(core_queries.SPEC_NODE_EXISTS, {"project_id": "p", "n_key": "k"})
    assert raised.value.code == "query_parameter_missing"
    assert raised.value.fields["parameter"] == "n_label"
    plan = dialect.plan(core_queries.SPEC_NODE_EXISTS, {"project_id": "p", "n_key": "k", "n_label": "L", "extra": 1})
    assert plan.statements[0].params == {"project_id": "p", "n_key": "k", "n_label": "L"}


@pytest.mark.parametrize("template", ["core.nothing_like_this", "build.nothing_like_this", "core.ddl"])
def test_an_unknown_or_cypher_only_template_is_refused(template: str) -> None:
    with pytest.raises(ConformanceError) as raised:
        PostgresDialect().plan(Query("x", template, project_scoped=False), {})
    assert raised.value.code == "query_template_unknown"
    assert raised.value.fields["store"] == "postgres"


def test_a_literal_query_is_one_sql_statement_that_still_has_to_bind_the_project_id() -> None:
    dialect = PostgresDialect()
    bound = Query.literal("probe", "SELECT cast(:project_id AS text) AS p, cast(:rows AS jsonb) AS r")
    plan = dialect.plan(bound, {"project_id": "p", "rows": [{"a": 1}]})
    assert plan.statements[0].params == {"project_id": "p", "rows": '[{"a":1}]'}
    with pytest.raises(ConformanceError) as raised:
        dialect.plan(Query.literal("probe", "SELECT 1 AS one"), {})
    assert raised.value.code == "query_missing_project_id"
    assert dialect.plan(Query.literal("open", "SELECT 1 AS one", project_scoped=False), {}).statements


def test_a_hand_built_query_cannot_smuggle_names_into_the_text() -> None:
    dialect = PostgresDialect()
    for query in (
        Query("x", "core.delete_relationship", core_queries.Relation("X'; DROP TABLE graph.node; --")),
        Query("x", "core.upsert_foreign_node", core_queries.ForeignLabel("A B")),
    ):
        with pytest.raises(ConformanceError) as raised:
            dialect.plan(query, {"project_id": "p"})
        assert raised.value.code == "cypher_name_unsafe"
    with pytest.raises(ConformanceError) as raised:
        dialect.plan(Query("x", "core.delete_relationship", core_queries.LabelKey("A", "b")), {"project_id": "p"})
    assert raised.value.code == "query_arguments_invalid"


def test_relationship_types_and_labels_are_bind_values_not_text() -> None:
    dialect = PostgresDialect()
    spec = NodeShape("SpecNode", "uuid", check_label=True)
    query = core_queries.upsert_relationship("PART_OF", spec, NodeShape("Finding", "uuid"))
    text = dialect.render(query, {})
    assert "PART_OF" not in text and "Finding" not in text
    plan = dialect.plan(query, {"project_id": "p", "s_key": "a", "s_label": "L", "t_key": "b", "key": "k", "props": {}})
    params = plan.statements[0].params
    assert params["rel_type"] == "PART_OF" and params["t_key_shape"] == "Finding"


def test_node_shapes_pick_their_match_fragment() -> None:
    dialect = PostgresDialect()
    document = NodeShape("Document", "document_id")
    fragment = NodeShape("Fragment", "fragment_id")
    text = dialect.render(core_queries.upsert_relationship("CONTAINS", document, fragment), {})
    assert "n.kind = 'document'" in text and "n.kind = 'fragment'" in text and "n.kind = 'spec'" not in text
    foreign = dialect.render(core_queries.delete_keyed_node("Finding", "uuid"), {})
    assert "n.kind = 'foreign'" in foreign and ":key_shape" in foreign
    with pytest.raises(ConformanceError):
        dialect.plan(
            Query("x", "core.delete_keyed_node", core_queries.LabelKey("Document", "uuid")), {"project_id": "p"}
        )


def test_scope_walks_render_a_literal_depth_cap_and_bind_the_relations() -> None:
    dialect = PostgresDialect()
    query = core_queries.scope_via(("PART_OF", "RELATES_TO"), 1, 2)
    text = dialect.render(query, {})
    assert "w.depth < 2" in text and "/*__" not in text
    plan = dialect.plan(
        query,
        {"project_id": "p", "spec_id": "s", "labels": ["L"], "symmetric": [], "roots": [], "max_hops": 2},
    )
    assert plan.statements[0].params["types"] == ["PART_OF", "RELATES_TO"]
    for hops in ((0, 2), (3, 2), (1, 99)):
        with pytest.raises(ConformanceError) as raised:
            dialect.plan(
                Query("x", "core.scope_via", core_queries.ScopeVia(("PART_OF",), *hops)),
                {"project_id": "p", "spec_id": "s", "labels": [], "symmetric": [], "roots": [], "max_hops": 1},
            )
        assert raised.value.code == "query_arguments_invalid"
    sequence = core_queries.scope_sequence(("GROUPED_BY", "REALIZED_BY"), ("REALIZED_BY",))
    plan = dialect.plan(sequence, {"project_id": "p", "spec_id": "s", "labels": [], "roots": []})
    assert plan.statements[0].params["types"] == ["GROUPED_BY", "REALIZED_BY"]
    assert plan.statements[0].params["either_way"] == [False, True]


def test_the_fulltext_term_list_is_derived_from_the_lucene_string() -> None:
    dialect = PostgresDialect()
    params = {
        "project_id": "p",
        "index": "i",
        "query": "pay  card checkout",
        "labels": [],
        "where": {},
        "defaulted": {},
        "skip": 0,
        "page": 5,
    }
    plan = dialect.plan(core_queries.SEARCH_NODES_FULLTEXT, params)
    assert plan.statements[0].params["terms"] == ["pay", "card", "checkout"]
    text = plan.statements[0].text
    assert "1.2" in text and "0.75" in text and "__BM25" not in text
    explicit = dialect.plan(core_queries.SEARCH_NODES_FULLTEXT, {**params, "terms": ["only"]})
    assert explicit.statements[0].params["terms"] == ["only"]


def test_a_vector_search_sets_the_plan_cache_mode_and_the_hnsw_settings_before_it() -> None:
    dialect = PostgresDialect()
    params = {
        "project_id": "p",
        "index": "i",
        "model": "m",
        "vector": [0.1] * 12,
        "labels": ["L"],
        "where": {},
        "defaulted": {},
        "limit": 5,
        "k": 7,
    }
    plan = dialect.plan(core_queries.SEARCH_NODES_VECTOR, params)
    setup, main = plan.statements
    assert "set_config('plan_cache_mode'" in setup.text
    assert setup.params == {
        "plan_cache_mode": "force_custom_plan",
        "iterative_scan": "relaxed_order",
        "ef_search": "40",  # never below 40
        "max_scan_tuples": "40000",
    }
    assert "halfvec(12)" in main.text and "3072" not in main.text
    assert main.params["vector"].startswith("[0.1,0.1")
    big = dialect.plan(core_queries.SEARCH_NODES_VECTOR, {**params, "k": 5000})
    assert big.statements[0].params["ef_search"] == "1000"  # the pgvector maximum
    rows = plan.reduce([[{"plan_cache_mode": "x"}], [{"fetched": 1, "hits": []}]])
    assert rows == [{"fetched": 1, "hits": []}]
    exact = dialect.plan(core_queries.SEARCH_NODES_VECTOR_EXACT, params)
    assert exact.statements[0].params == {"plan_cache_mode": "force_custom_plan"}
    for bad in ({**params, "vector": []}, {**params, "k": 0}, {**params, "vector": "text"}):
        with pytest.raises(ConformanceError) as raised:
            dialect.plan(core_queries.SEARCH_NODES_VECTOR, bad)
        assert raised.value.code == "query_parameter_invalid"


def test_parameters_are_converted_for_the_statements() -> None:
    assert parameter_value("props", {"a": [1, 2], "b": None, "é": "ü"}) == '{"a":[1,2],"b":null,"é":"ü"}'
    assert parameter_value("vector", [1, 0.5]) == "[1.0,0.5]"
    assert vector_text((0.25, 1.0)) == "[0.25,1.0]"
    assert parameter_value("labels", ("a", "b")) == ["a", "b"]
    assert parameter_value("limit", 3) == 3
    assert json_text({"k": "v"}) == '{"k":"v"}'


def test_the_schema_name_is_rendered_into_the_text_and_validated() -> None:
    sql = "SELECT 1 FROM graph.node AS n JOIN graph.edge AS e ON TRUE WHERE x = 'specflow.graph.ensure_schema'"
    assert render_schema(sql, "graph") == sql
    rendered = render_schema(sql, "Spec_Graph")
    assert '"Spec_Graph".node' in rendered and '"Spec_Graph".edge' in rendered
    assert "'specflow.graph.ensure_schema'" in rendered
    assert render_schema("CREATE SCHEMA IF NOT EXISTS graph", "alt") == "CREATE SCHEMA IF NOT EXISTS alt"
    for bad in ("", "a b", "a;b", "1abc", "x" * 64, 'a"b'):
        with pytest.raises(ConformanceError):
            PostgresDialect(bad)
    text = PostgresDialect("alt").render(core_queries.READ_DOCUMENTS, {})
    assert "alt.node" in text and "graph." not in text


def test_text_ordering_always_uses_the_code_point_collation() -> None:
    """Neo4j orders strings by code point; every ORDER BY on text says COLLATE "C" (plan rules)."""

    for name in CORE_FILES:
        text = (CORE_SQL_DIRECTORY / name).read_text(encoding="utf-8")
        for clause in re.findall(r"ORDER BY\s+([^\n]*(?:\n\s+[^\n]*)*?)(?:\n\n|\nLIMIT|\)|$)", text):
            for item in clause.split(","):
                column = item.strip().split()[0] if item.strip() else ""
                if re.search(r"(spec_id|fragment_id|document_id|uuid|path|relation|source|target|edge_key)$", column):
                    assert 'COLLATE "C"' in item, (name, item)


# Error mapping and retry


class BoomError(Exception):
    """A statement error that carries a SQLSTATE, as asyncpg's do."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.sqlstate = code


def wrapped(error: BaseException) -> DBAPIError:
    return DBAPIError("statement", {}, error)


@pytest.mark.parametrize(
    ("error", "expected", "code"),
    [
        (asyncpg.InvalidPasswordError(), ConfigurationError, "store_auth_rejected"),
        (asyncpg.InvalidAuthorizationSpecificationError(), ConfigurationError, "store_auth_rejected"),
        (asyncpg.InsufficientPrivilegeError(), ConfigurationError, "store_access_denied"),
        (asyncpg.InvalidCatalogNameError(), ConfigurationError, "store_database_not_found"),
        (asyncpg.InvalidSchemaNameError(), ConfigurationError, "store_database_not_found"),
        (asyncpg.ConnectionDoesNotExistError(), InfrastructureUnavailableError, "store_unreachable"),
        (asyncpg.CannotConnectNowError(), InfrastructureUnavailableError, "store_unreachable"),
        (ConnectionRefusedError(), InfrastructureUnavailableError, "store_unreachable"),
        (TimeoutError(), InfrastructureUnavailableError, "store_unreachable"),
        (asyncpg.AdminShutdownError(), InfrastructureUnavailableError, "store_unreachable"),
    ],
)
def test_sqlstates_map_to_neutral_codes_with_the_store(error: BaseException, expected: type, code: str) -> None:
    for candidate in (error, wrapped(error)):
        for write in (False, True):
            mapped = map_error(candidate, "q", write=write)
            assert isinstance(mapped, expected) and mapped.code == code
            assert mapped.fields["store"] == "postgres"
    assert CANARY not in str(map_error(error, "q", write=False))


def test_other_failures_are_write_or_read_failures_and_malformed_statements_are_rejected() -> None:
    unique = asyncpg.UniqueViolationError()
    write = map_error(wrapped(unique), "upsert_spec_node", write=True, attempts=2)
    assert isinstance(write, GraphWriteError) and write.code == "store_write_failed"
    assert write.fields["backend_code"] == "23505" and write.fields["attempts"] == 2
    rejected = map_error(asyncpg.UndefinedColumnError(), "q", write=False)
    assert isinstance(rejected, ConformanceError) and rejected.code == "store_query_rejected"
    other = map_error(asyncpg.DiskFullError(), "q", write=False)
    assert isinstance(other, InfrastructureUnavailableError) and other.code == "store_read_failed"
    assert sqlstate(BoomError("40001")) == "40001" and sqlstate(wrapped(BoomError("40P01"))) == "40P01"
    assert sqlstate(ValueError("x")) is None


def test_serialization_failures_deadlocks_lost_connections_and_a_starting_server_are_transient() -> None:
    for error in (
        asyncpg.SerializationError(),
        asyncpg.DeadlockDetectedError(),
        asyncpg.CannotConnectNowError(),
        asyncpg.ConnectionFailureError(),
        asyncpg.ConnectionDoesNotExistError(),
        ConnectionResetError(),
        wrapped(asyncpg.SerializationError()),
    ):
        assert is_transient(error), error
    for error in (asyncpg.UniqueViolationError(), asyncpg.InsufficientPrivilegeError(), asyncpg.SyntaxOrAccessError()):
        assert not is_transient(error), error


def test_a_pool_timeout_and_a_cancelled_statement_are_not_retried() -> None:
    pool = PoolTimeoutError("QueuePool limit reached")
    pool.__cause__ = TimeoutError()  # the wait that expired must not make it look like a socket timeout
    assert not is_transient(pool)
    assert not is_transient(wrapped(asyncpg.QueryCanceledError()))  # statement_timeout (57014)
    assert not is_transient(wrapped(asyncpg.LockNotAvailableError()))  # lock_timeout (55P03)
    mapped = map_error(pool, "read_nodes", write=False)
    assert isinstance(mapped, InfrastructureUnavailableError) and mapped.code == "store_read_failed"
    assert is_transient(TimeoutError())  # a connect timeout still is


def test_every_connection_gets_a_statement_and_a_lock_timeout_that_the_uri_can_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def capture(url: Any, **kwargs: Any) -> None:
        seen.append(kwargs["connect_args"])

    monkeypatch.setattr(pg_executor, "create_async_engine", capture)
    pg_executor.build_engine(config())
    server = seen[0]["server_settings"]
    assert server == {"application_name": "specflow-graph", "statement_timeout": "300000", "lock_timeout": "60000"}
    _, args = connection_url(config(uri="postgresql://h/d?statement_timeout=12.5&lock_timeout=0&sslmode=require"))
    assert args["server_settings"] == {"statement_timeout": "12500", "lock_timeout": "0"} and args["ssl"] == "require"
    for bad in ("statement_timeout=soon", "lock_timeout=-1", "statement_timeout=999999"):
        with pytest.raises(ConfigurationError) as raised:
            connection_url(config(uri=f"postgresql://h/d?{bad}"))
        assert raised.value.code == "store_uri_invalid"


class FakeResult:
    returns_rows = True

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeTransaction:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self.is_active = True

    def __await__(self) -> Any:
        async def done() -> FakeTransaction:
            return self

        return done().__await__()

    async def __aenter__(self) -> FakeTransaction:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.is_active = False
        self._connection.log.append("commit" if exc[0] is None else "rollback")

    async def commit(self) -> None:
        self.is_active = False
        self._connection.log.append("commit")

    async def rollback(self) -> None:
        self.is_active = False
        self._connection.log.append("rollback")


class FakeConnection:
    """Answers each statement with the next scripted outcome: an exception to raise or rows to return."""

    def __init__(self, script: list[Any], log: list[str]) -> None:
        self.script = script
        self.log = log

    async def __aenter__(self) -> FakeConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    async def execution_options(self, **options: Any) -> FakeConnection:
        self.log.append(f"options {sorted(options)}")
        return self

    async def execute(self, _statement: Any, params: dict[str, Any]) -> FakeResult:
        self.log.append(f"execute {sorted(params)}")
        outcome = self.script.pop(0) if self.script else []
        if isinstance(outcome, asyncpg.PostgresError):
            raise wrapped(outcome)  # SQLAlchemy wraps every driver error
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResult(outcome)


class FakeEngine:
    def __init__(self, script: list[Any]) -> None:
        self.script = script
        self.log: list[str] = []
        self.connections = 0

    def connect(self) -> FakeConnection:
        self.connections += 1
        return FakeConnection(self.script, self.log)


def fake_executor(
    script: list[Any], *, delays: Sequence[float] = (0.1, 0.3, 1.0)
) -> tuple[PostgresExecutor, FakeEngine, list[float]]:
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)

    engine = FakeEngine(script)
    return PostgresExecutor(cast(AsyncEngine, engine), retry_delays=delays, sleep=sleep), engine, waits


async def test_a_transient_read_failure_is_retried_with_the_shared_delays() -> None:
    executor, engine, waits = fake_executor(
        [asyncpg.DeadlockDetectedError(), asyncpg.CannotConnectNowError(), [{"ok": 1}]]
    )
    rows = await executor.read(core_queries.PING.__class__("ping_scoped", "core.read_documents"), "p")
    assert rows == [{"ok": 1}]
    assert waits == [0.1, 0.3] and engine.connections == 3


async def test_a_write_still_failing_after_the_last_retry_is_a_write_error_with_its_attempts() -> None:
    executor, engine, waits = fake_executor([asyncpg.SerializationError()] * 4)
    with pytest.raises(GraphWriteError) as raised:
        await executor.write(core_queries.UPSERT_DOCUMENT, "p", key="d", props={})
    assert raised.value.code == "store_write_failed" and raised.value.fields["attempts"] == 4
    assert raised.value.fields["backend_code"] == "40001"
    assert waits == [0.1, 0.3, 1.0] and engine.connections == 4


async def test_a_permanent_failure_is_not_retried() -> None:
    executor, engine, waits = fake_executor([asyncpg.InsufficientPrivilegeError()])
    with pytest.raises(ConfigurationError) as raised:
        await executor.read(core_queries.READ_DOCUMENTS, "p")
    assert raised.value.code == "store_access_denied" and waits == [] and engine.connections == 1


async def test_a_read_validates_the_project_id_and_refuses_an_unscoped_query() -> None:
    executor, _, _ = fake_executor([])
    with pytest.raises(ConfigurationError) as raised:
        await executor.read(core_queries.READ_DOCUMENTS, "bad id with spaces")
    assert raised.value.code == "invalid_project_id"
    with pytest.raises(ConformanceError) as unscoped:
        await executor.read(core_queries.PING, "p")
    assert unscoped.value.code == "query_not_project_scoped"
    with pytest.raises(ConformanceError) as reserved:
        await executor.read(core_queries.READ_DOCUMENTS, "p", project_id="other")
    assert reserved.value.code == "project_id_parameter_reserved"


async def test_a_missing_project_binding_is_refused_before_the_project_id_is_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(pg_dialect._STATIC, "core.ping", lambda: "SELECT 1 AS ok")
    unbound = Query("ping_scoped", "core.ping")
    executor, engine, _ = fake_executor([])
    with pytest.raises(ConformanceError) as read:
        await executor.read(unbound, "bad id with spaces")
    assert read.value.code == "query_missing_project_id"
    with pytest.raises(ConformanceError) as write:
        await executor.write(unbound, "bad id with spaces")
    assert write.value.code == "query_missing_project_id"
    with pytest.raises(ConformanceError) as inside:
        async with executor.tx("p") as transaction:  # the transaction's project id is valid here
            await transaction.run(unbound)
    assert inside.value.code == "query_missing_project_id"
    assert engine.connections <= 1  # nothing reached the server for the two refused calls


def test_the_dialect_checks_the_binding_without_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    dialect = PostgresDialect()
    dialect.require_project_binding(core_queries.SEARCH_NODES_FULLTEXT)  # needs no query text or terms
    dialect.require_project_binding(core_queries.SEARCH_NODES_VECTOR)  # nor a vector
    monkeypatch.setitem(pg_dialect._STATIC, "core.ping", lambda: "SELECT 1 AS ok")
    with pytest.raises(ConformanceError) as raised:
        dialect.require_project_binding(Query("ping_scoped", "core.ping"))
    assert raised.value.code == "query_missing_project_id"


@pytest.mark.parametrize("query", [core_queries.SEARCH_NODES_FULLTEXT.name, core_queries.SEARCH_FRAGMENTS.name])
def test_only_a_fulltext_search_is_too_complex(query: str) -> None:
    mapped = map_error(wrapped(asyncpg.StatementTooComplexError()), query, write=False, attempts=1)
    assert isinstance(mapped, ConformanceError) and mapped.code == "query_too_complex"
    assert mapped.fields["store"] == "postgres" and mapped.fields["query"] == query
    assert mapped.fields["backend_code"] == "54001" and mapped.fields["attempts"] == 1
    assert mapped.fields["error_type"] == "DBAPIError"


@pytest.mark.parametrize("query", ["read_nodes", core_queries.SEARCH_NODES_VECTOR.name, "ping"])
def test_the_same_failure_of_any_other_query_keeps_its_normal_code(query: str) -> None:
    mapped = map_error(wrapped(asyncpg.StatementTooComplexError()), query, write=False, attempts=2)
    assert isinstance(mapped, InfrastructureUnavailableError) and mapped.code == "store_read_failed"
    assert mapped.fields["backend_code"] == "54001" and mapped.fields["attempts"] == 2
    written = map_error(
        wrapped(asyncpg.StatementTooComplexError()), core_queries.SEARCH_NODES_FULLTEXT.name, write=True
    )
    assert isinstance(written, GraphWriteError) and written.code == "store_write_failed"


async def test_an_explicit_transaction_commits_and_rolls_back_without_retrying() -> None:
    executor, engine, waits = fake_executor([[{"written": 1}], asyncpg.DeadlockDetectedError()])
    async with executor.tx("p") as tx:
        assert await tx.run(core_queries.UPSERT_DOCUMENT, key="d", props={}) == [{"written": 1}]
    assert engine.log[-1] == "commit"
    with pytest.raises(GraphWriteError) as raised:
        async with executor.tx("p") as tx:
            await tx.run(core_queries.UPSERT_DOCUMENT, key="d", props={})
    assert raised.value.fields["backend_code"] == "40P01" and waits == []
    assert engine.log[-1] == "rollback"
    with pytest.raises(RuntimeError):
        async with executor.tx("p") as tx:
            raise RuntimeError("caller bug")
    assert engine.log[-1] == "rollback"
    assert executor.capabilities.multi_statement_tx is True


# Schema coverage and files


def test_the_shipped_ddl_covers_the_ontology() -> None:
    objects = schema_objects(shipped_ontology())
    names = {item.name for item in objects}
    assert {"node", "edge", "meta", "fts_norm", "tsv_len", "props_merge", "edge_key_uq", "node_spec_uuid_uq"} <= names
    assert next(item.kind for item in objects) == "schema"


def test_a_ddl_missing_a_key_fails_with_schema_ddl_incomplete() -> None:
    statements = split_statements(load_ddl_sql("schema.sql"))
    without = [item for item in statements if "node_document_uq" not in item]
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(shipped_ontology(), without)
    assert raised.value.code == "schema_ddl_incomplete"
    assert "document_id" in str(raised.value.fields["properties"])
    no_vector = [item.replace("embedding vector,", "embedding text,") for item in statements]
    with pytest.raises(ConfigurationError) as column:
        schema_objects(shipped_ontology(), no_vector)
    assert column.value.fields["kind"] == "column"


def test_a_ddl_with_an_unknown_statement_or_a_duplicate_name_is_refused() -> None:
    with pytest.raises(ConfigurationError) as raised:
        parse_ddl_statement("DROP TABLE graph.node", position=4)
    assert raised.value.code == "schema_ddl_unrecognised" and raised.value.fields["position"] == 4
    statements = split_statements(load_ddl_sql("schema.sql"))
    with pytest.raises(ConfigurationError) as duplicate:
        schema_objects(shipped_ontology(), [*statements, statements[-1]])
    assert duplicate.value.code == "schema_ddl_duplicate_name"


def test_a_foreign_label_needs_its_key_index(tmp_path: Path) -> None:
    ontology = load_gap_ontology(tmp_path)
    statements = split_statements(load_ddl_sql("schema.sql"))
    assert schema_objects(ontology, statements)
    without = [item for item in statements if "node_foreign_uq" not in item]
    with pytest.raises(ConfigurationError) as raised:
        schema_objects(ontology, without)
    assert raised.value.code == "schema_ddl_incomplete" and "labels[1]" in str(raised.value.fields["properties"])
    assert schema_objects(shipped_ontology(), without)  # the shipped ontology has no foreign label


def test_statements_split_at_semicolons_outside_dollar_quotes() -> None:
    text = "CREATE A; CREATE FUNCTION f() AS $$ SELECT 1; SELECT 2 $$;\nCREATE B;\n"
    assert split_statements(text) == ("CREATE A", "CREATE FUNCTION f() AS $$ SELECT 1; SELECT 2 $$", "CREATE B")
    assert split_statements("") == ()


def test_the_schema_file_is_split_into_statements_that_each_create_one_object() -> None:
    statements = split_statements(load_ddl_sql("schema.sql"))
    assert len(statements) == 21
    assert {parse_ddl_statement(item).kind for item in statements} == {"schema", "function", "table", "index"}


# Settings, URL, store


def test_the_connection_url_takes_host_database_login_and_ssl_mode() -> None:
    url, args = connection_url(config(uri="postgresql://db.example.org:6543/specflow?sslmode=require"))
    assert url.drivername == "postgresql+asyncpg" and url.host == "db.example.org" and url.port == 6543
    assert url.database == "specflow" and url.username == "u" and url.password == CANARY
    assert args == {"ssl": "require"} and "sslmode" not in url.query
    plain, none = connection_url(config(uri="postgresql+asyncpg://h/d"))
    assert plain.drivername == "postgresql+asyncpg" and none == {}
    for bad in ("bolt://h:7687", "not a url", "postgresql://h/d?sslmode=maybe"):
        with pytest.raises(ConfigurationError) as raised:
            connection_url(config(uri=bad))
        assert raised.value.code == "store_uri_invalid" and CANARY not in str(raised.value)


def test_routing_builds_the_postgres_store_without_connecting() -> None:
    store = build_store(config())
    assert isinstance(store, PostgresStore) and isinstance(store, GraphStore) and is_store(store)
    assert store.info == StoreInfo("postgres", StoreCapabilities(multi_statement_tx=True), None)
    assert store.schema == "graph"
    assert isinstance(store.executor(), Executor) and store.executor() is store.executor()
    assert as_executor(store) is store.executor()


def postgres_store(settings: StoreConfig) -> PostgresStore:
    built = build_store(settings)
    assert isinstance(built, PostgresStore)
    return built


def test_the_store_name_is_the_schema_and_must_be_a_plain_name() -> None:
    assert postgres_store(config(name=None)).schema == "graph"
    assert postgres_store(config(name="graph_v2")).schema == "graph_v2"
    for bad in ("a b", "x;y", "1x", "a" * 64):
        with pytest.raises(ConfigurationError) as raised:
            build_store(config(name=bad))
        assert raised.value.code == "store_name_invalid"


async def test_a_store_with_its_own_engine_disposes_it_and_a_borrowed_one_stays_open() -> None:
    disposed: list[bool] = []

    class Engine:
        async def dispose(self) -> None:
            disposed.append(True)

    await PostgresStore(cast(AsyncEngine, Engine()), "graph").close()
    assert disposed == []
    await PostgresStore(cast(AsyncEngine, Engine()), "graph", owns_engine=True).close()
    assert disposed == [True]


def test_the_engine_pool_is_small_and_never_the_hosts() -> None:
    engine = pg_executor.build_engine(config(pool_size=3))
    assert engine.pool.size() == 3
    assert pg_executor.build_engine(config()).pool.size() == pg_executor.DEFAULT_POOL_SIZE
    assert CANARY not in repr(engine.url) and "***" in repr(engine.url)


def test_the_sql_files_ship_without_sql_text_in_python() -> None:
    """The adapter holds its SQL in files; Python only loads and fills them (the sql track checks the files)."""

    root = Path(pg_dialect.__file__).parent
    for module in root.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert not re.search(r"\b(SELECT|INSERT INTO|DELETE FROM|CREATE (TABLE|INDEX|SCHEMA))\b.{20,}", text), (
            module.name
        )
