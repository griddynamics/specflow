"""The graph store binding: GRAPH_STORE_* names, NEO4J_* aliases, the PostgreSQL DATABASE_* reuse, guards."""

from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr

from graph.stores.config import StoreConfig
from settings import GRAPH_BUILD_SWITCHES, ConfigurationError, GraphStoreSettings, load_settings
from test_specflow_settings_guards import deployed, refused


def _aliased(env: dict[str, str]) -> dict[str, str]:
    """``env`` as a deployment that only sets the NEO4J_* names has it."""

    for name in ("URI", "USER", "PASSWORD"):
        env[f"NEO4J_{name}"] = env.pop(f"GRAPH_STORE_{name}")
    return env


def _primary(env: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
    """``env`` with the store given by other GRAPH_STORE_* values."""

    env.update(
        {
            "GRAPH_STORE_URI": "bolt://graph.example.test:7687",
            "GRAPH_STORE_USER": "graph-user",
            "GRAPH_STORE_PASSWORD": "graph-pass-1d2e",
        }
    )
    env.update(extra or {})
    return env


def test_no_graph_store_name_is_a_graph_build_switch():
    assert [name for name in GRAPH_BUILD_SWITCHES if name.startswith("GRAPH_STORE")] == []
    assert GraphStoreSettings.variables() and not set(GraphStoreSettings.variables()) & set(GRAPH_BUILD_SWITCHES)


def test_the_store_binding_reads_its_own_names_and_defaults_to_neo4j(valid_env):
    store = load_settings("api", _primary(valid_env)).graph_store

    assert (store.backend, store.uri, store.user, store.name) == (
        "neo4j",
        "bolt://graph.example.test:7687",
        "graph-user",
        "neo4j",
    )
    assert isinstance(store.password, SecretStr)
    assert "graph-pass-1d2e" not in repr(store)


@pytest.mark.parametrize("role_env", [{}, {"WORKER_QUEUES": "agents"}])
def test_a_deployment_with_only_neo4j_names_starts_unchanged_for_api_and_worker_agents(valid_env, role_env):
    valid_env.update(role_env)
    _aliased(valid_env)
    valid_env["NEO4J_DATABASE"] = "graphs"
    for role in ("api", "worker"):
        config = load_settings(role, valid_env).graph_store.to_store_config()

        assert isinstance(config, StoreConfig)
        assert (config.backend, config.uri, config.user, config.name) == (
            "neo4j",
            "bolt://localhost:7687",
            "neo4j",
            "graphs",
        )
        assert config.password.get_secret_value() == "neo-pass-4e8a"


def test_a_graph_store_name_wins_over_its_alias(valid_env):
    _aliased(valid_env)
    valid_env["GRAPH_STORE_URI"] = "bolt://other.example.test:7687"

    store = load_settings("api", valid_env).graph_store

    assert store.uri == "bolt://other.example.test:7687"
    assert store.user == "neo4j"  # alias for the unset one


def test_the_alias_use_is_logged_once_with_names_only(valid_env, caplog):
    with caplog.at_level(logging.WARNING):
        load_settings("api", _aliased(valid_env))

    records = [r for r in caplog.records if getattr(r, "event", r.getMessage()) == "graph.settings.alias_used"]
    assert len(records) == 1
    rendered = "".join(f"{r.getMessage()} {r.__dict__}" for r in records)
    assert "NEO4J_PASSWORD" in rendered and "neo-pass-4e8a" not in rendered


def test_nothing_is_logged_when_no_alias_is_used(valid_env, caplog):
    with caplog.at_level(logging.WARNING):
        load_settings("api", _primary(valid_env))

    assert "graph.settings.alias_used" not in caplog.text


@pytest.mark.parametrize("backend", ["arcadedb", "tigergraph"])
def test_the_aliases_apply_to_neo4j_only(valid_env, backend):
    _aliased(valid_env)
    valid_env["GRAPH_STORE"] = backend

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)

    assert set(raised.value.variables) == {"GRAPH_STORE_URI", "GRAPH_STORE_USER", "GRAPH_STORE_PASSWORD"}


@pytest.mark.parametrize("backend", ["arcadedb", "tigergraph"])
def test_the_other_backends_take_their_own_names(valid_env, backend):
    env = _primary(valid_env, {"GRAPH_STORE": backend, "GRAPH_STORE_ADMIN_URI": "http://admin.example.test:2480"})
    env.update(GRAPH_STORE_POOL_SIZE="7", GRAPH_STORE_NAME="shop")

    config = load_settings("api", env).graph_store.to_store_config()

    assert (config.backend, config.name, config.admin_uri, config.pool_size) == (
        backend,
        "shop",
        "http://admin.example.test:2480",
        7,
    )


def test_an_unknown_backend_and_a_bad_pool_size_are_refused_by_name(valid_env):
    env = _primary(valid_env, {"GRAPH_STORE": "mysql", "GRAPH_STORE_POOL_SIZE": "0"})

    assert refused("api", env) == ("GRAPH_STORE",)
    env["GRAPH_STORE"] = "arcadedb"
    assert refused("api", env) == ("GRAPH_STORE_POOL_SIZE",)


def test_blank_optional_names_fall_back_to_the_defaults(valid_env):
    env = _primary(
        valid_env,
        {"GRAPH_STORE": "", "GRAPH_STORE_NAME": "", "GRAPH_STORE_ADMIN_URI": "", "GRAPH_STORE_POOL_SIZE": ""},
    )

    config = load_settings("api", env).graph_store.to_store_config()

    assert (config.backend, config.name, config.admin_uri, config.pool_size) == ("neo4j", "neo4j", None, None)


def test_postgres_without_a_uri_reuses_the_database_connection(valid_env):
    for name in ("URI", "USER", "PASSWORD"):
        del valid_env[f"GRAPH_STORE_{name}"]
    valid_env["GRAPH_STORE"] = "postgres"

    config = load_settings("api", valid_env).graph_store.to_store_config()

    assert (config.backend, config.uri, config.user, config.name) == (
        "postgres",
        "postgresql://localhost:5433/specgraph",
        "app_user",
        "graph",
    )
    assert config.password.get_secret_value() == "db-pass-9f1c"
    assert "db-pass-9f1c" not in config.uri


def test_postgres_reuse_takes_the_schema_name_and_an_explicit_role(valid_env):
    valid_env.update(
        {
            "GRAPH_STORE": "postgres",
            "GRAPH_STORE_NAME": "graph2",
            "GRAPH_STORE_USER": "graph_role",
            "GRAPH_STORE_PASSWORD": "graph-pass-1d2e",
        }
    )

    config = load_settings("worker", valid_env).graph_store.to_store_config()

    assert (config.user, config.name) == ("graph_role", "graph2")
    assert config.password.get_secret_value() == "graph-pass-1d2e"


def test_postgres_with_a_uri_needs_its_own_credentials(valid_env):
    valid_env.update(GRAPH_STORE="postgres", GRAPH_STORE_URI="postgresql://graph.example.test:5432/graphs")
    del valid_env["GRAPH_STORE_USER"], valid_env["GRAPH_STORE_PASSWORD"]

    assert set(refused("api", valid_env)) == {"GRAPH_STORE_USER", "GRAPH_STORE_PASSWORD"}


def test_postgres_reuse_does_not_hide_the_database_refusal(valid_env):
    valid_env.update(GRAPH_STORE="postgres")
    del valid_env["DATABASE_HOST"], valid_env["GRAPH_STORE_URI"]

    assert refused("api", valid_env) == ("DATABASE_HOST",)


@pytest.mark.parametrize("role", ["api", "worker"])
@pytest.mark.parametrize("variable", ["GRAPH_STORE_PASSWORD", "NEO4J_PASSWORD"])
def test_a_blank_store_password_is_refused_outside_local_by_the_name_in_use(role, variable, valid_env):
    env = deployed(valid_env if variable.startswith("GRAPH") else _aliased(valid_env))
    env[variable] = ""

    assert refused(role, env) == (variable,)


def test_a_blank_store_password_is_accepted_locally(valid_env):
    load_settings("worker", _primary(valid_env, {"GRAPH_STORE_PASSWORD": ""}))
    load_settings("worker", {**_aliased(valid_env), "NEO4J_PASSWORD": ""})


def test_the_store_is_required_exactly_where_neo4j_was(valid_env):
    for name in ("URI", "USER", "PASSWORD"):
        del valid_env[f"GRAPH_STORE_{name}"]
    assert set(refused("api", valid_env)) == {"GRAPH_STORE_URI", "GRAPH_STORE_USER", "GRAPH_STORE_PASSWORD"}
    assert set(refused("worker", {**valid_env, "WORKER_QUEUES": "agents"})) >= {"GRAPH_STORE_URI"}
    assert load_settings("worker", {**valid_env, "WORKER_QUEUES": "parse"}).graph_store is None
    assert load_settings("migrate", valid_env).graph_store is None
