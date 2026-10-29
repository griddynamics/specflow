"""AC-1.1: required bindings per role, one error for everything, no values leaked."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from graph.core import Neo4jConfig
from runtime_defaults_support import RUNTIME_DEFAULTS
from settings import (
    AgentDatabaseSettings,
    ConfigurationError,
    DatabaseSettings,
    GraphBuildSettings,
    load_settings,
)

# Written out literally, so a change to the code under test cannot shrink the matrix.
DATABASE = (
    "DATABASE_HOST",
    "DATABASE_NAME",
    "DATABASE_USER",
    "DATABASE_PASSWORD",
)
STORAGE = ("STORAGE_BUCKET", "STORAGE_PREFIX")
TEMPORAL = ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE")
NEO4J = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")
AUTH = ("AUTH_ALLOWED_EMAIL_DOMAINS",)
AGENT = ("AGENT_MODEL", "LLM_GATEWAY_URL", "LLM_GATEWAY_API_KEY")

REQUIRED = {
    "migrate": ("APP_ENV", *DATABASE),
    "api": ("APP_ENV", *DATABASE, *AUTH, *STORAGE, *TEMPORAL, *NEO4J, "AGENT_MODEL"),
    "worker": (
        "APP_ENV",
        *DATABASE,
        *STORAGE,
        *TEMPORAL,
        *NEO4J,
        *AGENT,
        "DATABASE_AGENT_PASSWORD",
    ),
}
# What a role must not need: removing it keeps the role loadable.
NOT_NEEDED = {
    "migrate": ("DATABASE_PORT", *AUTH, *STORAGE, *TEMPORAL, *NEO4J, *AGENT, "DATABASE_AGENT_PASSWORD"),
    "api": (
        "DATABASE_AGENT_PASSWORD",
        "LLM_GATEWAY_URL",
        "LLM_GATEWAY_API_KEY",
        # optional: a default exists, or the value is only needed in oidc mode
        "DATABASE_PORT",
        "OIDC_ISSUER",
        "GOOGLE_CLOUD_PROJECT",
    ),
    "worker": (*AUTH, "DATABASE_PORT", "GOOGLE_CLOUD_PROJECT"),
}


@pytest.mark.parametrize("role", ["api", "worker", "migrate"])
def test_each_role_loads_from_a_valid_environment(role, valid_env):
    settings = load_settings(role, valid_env)

    assert settings.role == role
    assert settings.app.app_env == "local"
    assert settings.app.api_port == int(valid_env["API_PORT"])
    assert isinstance(settings.database, DatabaseSettings)


def test_bindings_present_per_role(valid_env):
    api = load_settings("api", valid_env)
    worker = load_settings("worker", valid_env)
    migrate = load_settings("migrate", valid_env)

    assert api.auth and api.storage and api.temporal and api.neo4j and api.agent_run
    assert api.agent is None and api.agent_database is None
    assert worker.storage and worker.temporal and worker.neo4j and worker.agent
    assert worker.agent_run and worker.agent_database and worker.auth is None
    assert migrate.agent_database is not None and migrate.agent_database.password is None
    assert (migrate.auth, migrate.storage, migrate.temporal, migrate.neo4j) == (None,) * 4


@pytest.mark.parametrize(
    ("role", "variable"),
    [(role, name) for role, names in REQUIRED.items() for name in names],
)
def test_a_missing_required_variable_is_reported(role, variable, valid_env):
    del valid_env[variable]

    with pytest.raises(ConfigurationError) as raised:
        load_settings(role, valid_env)

    assert raised.value.variables == (variable,)


@pytest.mark.parametrize(
    ("role", "variable"),
    [(role, name) for role, names in NOT_NEEDED.items() for name in names],
)
def test_a_binding_the_role_does_not_need_may_be_absent(role, variable, valid_env):
    valid_env.pop(variable, None)

    load_settings(role, valid_env)


@pytest.mark.parametrize("role", ["api", "worker", "migrate"])
def test_everything_wrong_is_reported_in_one_error(role):
    with pytest.raises(ConfigurationError) as raised:
        load_settings(role, {})

    assert set(raised.value.variables) >= set(REQUIRED[role])
    assert len(raised.value.variables) == len(set(raised.value.variables))


def test_invalid_values_and_missing_ones_share_one_error(valid_env):
    valid_env["API_PORT"] = "not-a-port"
    valid_env["APP_ENV"] = "staging"
    del valid_env["NEO4J_URI"]
    valid_env["WORKER_QUEUES"] = "parse,other"

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)

    assert set(raised.value.variables) == {"API_PORT", "APP_ENV", "NEO4J_URI", "WORKER_QUEUES"}


def test_the_message_names_variables_and_never_values(valid_env):
    canary = "s3cr3t-value-XYZ"
    valid_env["API_PORT"] = canary
    valid_env.update({"DATABASE_PASSWORD": "  "})
    valid_env["AUTH_DEV_SIGNING_KEY"] = canary
    valid_env["AUTH_MODE"] = "oidc"
    valid_env["TEMPORAL_API_KEY"] = canary

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)

    text = f"{raised.value} {raised.value!r} {raised.value.variables}"
    assert canary not in text
    assert "API_PORT" in text
    assert "DATABASE_PASSWORD" in text


def test_the_environment_mapping_is_the_only_source(clean_specflow_env, valid_env):
    for name, value in valid_env.items():
        clean_specflow_env.setenv(name, value)

    with pytest.raises(ConfigurationError) as raised:
        load_settings("migrate", {"APP_ENV": "local"})

    assert set(raised.value.variables) == set(DATABASE)


def test_without_a_mapping_the_process_environment_is_used(clean_specflow_env, valid_env):
    for name, value in valid_env.items():
        clean_specflow_env.setenv(name, value)

    assert load_settings("migrate").database.host == "localhost"


def test_secrets_are_secret_strings_and_never_render(valid_env):
    settings = load_settings("worker", valid_env)

    assert isinstance(settings.database.password, SecretStr)
    assert isinstance(settings.neo4j.password, SecretStr)
    assert isinstance(settings.agent.llm_gateway_api_key, SecretStr)
    assert isinstance(settings.agent_database.password, SecretStr)
    rendered = repr(settings)
    for secret in ("db-pass-9f1c", "agent-pass-7b2d", "neo-pass-4e8a", "gateway-key-3c6e"):
        assert secret not in rendered


def test_defaults(valid_env):
    settings = load_settings("worker", valid_env)

    assert settings.temporal.queue_parse == "specflow-parse"
    assert settings.temporal.queue_agents == "specflow-agents"
    assert settings.temporal.worker_queues == ("parse", "agents")
    assert settings.neo4j.database == "neo4j"
    assert settings.agent_run.trace_sensitive_data is False
    api = load_settings("api", valid_env)
    assert api.auth.api_audience == "specflow-api"
    assert api.auth.api_client_ids == ("specflow-web",)
    assert api.auth.allowed_email_domains == ("example.com",)
    assert api.auth.jwks_url is None


def test_blank_optional_variables_fall_back_to_their_defaults(valid_env):
    valid_env.update(
        API_PORT="",
        AUTH_JWKS_URL="",
        AUTH_API_AUDIENCE="",
        AUTH_API_CLIENT_IDS="",
        NEO4J_DATABASE="",
        AGENT_TRACE_SENSITIVE_DATA="",
        TEMPORAL_API_KEY="",
        DATABASE_AGENT_USER="",
    )

    api = load_settings("api", valid_env)

    assert api.app.api_port == RUNTIME_DEFAULTS["API_PORT"]
    assert api.auth.jwks_url is None
    assert api.auth.api_audience == "specflow-api"
    assert api.auth.api_client_ids == ("specflow-web",)
    assert api.neo4j.database == "neo4j"
    assert api.agent_run.trace_sensitive_data is False
    assert api.temporal.api_key is None


def test_agent_model_has_one_source_of_truth(valid_env):
    valid_env["AGENT_MODEL"] = "  "

    with pytest.raises(ConfigurationError) as raised:
        load_settings("worker", valid_env)

    assert raised.value.variables.count("AGENT_MODEL") == 1
    valid_env["AGENT_MODEL"] = " a/model "
    worker = load_settings("worker", valid_env)
    assert worker.agent_run.model == "a/model"
    assert not hasattr(worker.agent, "model")


@pytest.mark.parametrize(("raw", "expected"), [("false", False), ("true", True), ("0", False)])
def test_trace_sensitive_data_flag(raw, expected, valid_env):
    valid_env["AGENT_TRACE_SENSITIVE_DATA"] = raw

    assert load_settings("api", valid_env).agent_run.trace_sensitive_data is expected


def test_the_worker_without_the_agents_queue_does_not_need_agent_bindings(valid_env):
    for name in (*NEO4J, *AGENT, "DATABASE_AGENT_PASSWORD"):
        del valid_env[name]
    valid_env["WORKER_QUEUES"] = "parse"

    worker = load_settings("worker", valid_env)

    assert (worker.neo4j, worker.agent, worker.agent_run) == (None, None, None)
    assert worker.agent_database.password is None


def test_queue_options_replace_worker_queues(valid_env):
    valid_env["WORKER_QUEUES"] = "agents"
    for name in (*NEO4J, *AGENT, "DATABASE_AGENT_PASSWORD"):
        del valid_env[name]

    worker = load_settings("worker", valid_env, queues=["parse"])

    assert worker.temporal.worker_queues == ("parse",)
    with pytest.raises(ConfigurationError) as raised:
        load_settings("worker", valid_env, queues=["agents"])
    assert "NEO4J_URI" in raised.value.variables
    assert "DATABASE_AGENT_PASSWORD" in raised.value.variables


def test_the_worker_polling_agents_needs_the_agent_password(valid_env):
    valid_env.update({"DATABASE_AGENT_PASSWORD": "  "})

    with pytest.raises(ConfigurationError) as raised:
        load_settings("worker", valid_env)

    assert raised.value.variables == ("DATABASE_AGENT_PASSWORD",)


def test_migrate_never_carries_the_agent_password(valid_env):
    migrate = load_settings("migrate", valid_env)

    assert migrate.agent_database.user == "agent_user"
    assert migrate.agent_database.password is None


def test_as_database_settings_swaps_user_and_password(valid_env):
    worker = load_settings("worker", valid_env)

    agent_db = worker.agent_database.as_database_settings(worker.database)

    assert agent_db.user == "agent_user"
    assert agent_db.password.get_secret_value() == "agent-pass-7b2d"
    assert (agent_db.host, agent_db.port, agent_db.name) == ("localhost", 5433, "specgraph")
    assert worker.database.user == "app_user"
    assert "agent-pass-7b2d" in agent_db.url().render_as_string(hide_password=False)


def test_as_database_settings_without_a_password_is_refused(valid_env):
    migrate = load_settings("migrate", valid_env)

    with pytest.raises(ConfigurationError) as raised:
        migrate.agent_database.as_database_settings(migrate.database)

    assert raised.value.variables == ("DATABASE_AGENT_PASSWORD",)
    assert isinstance(migrate.agent_database, AgentDatabaseSettings)


def test_neo4j_settings_map_onto_the_graph_library_config(valid_env):

    neo4j = load_settings("api", valid_env).neo4j
    config = neo4j.to_graph_config()

    assert isinstance(config, Neo4jConfig)
    assert (config.uri, config.user, config.database) == (
        "bolt://localhost:7687",
        "neo4j",
        "neo4j",
    )
    assert config.password.get_secret_value() == "neo-pass-4e8a"
    assert "neo-pass-4e8a" not in repr(config)


def test_langfuse_is_enabled_only_with_both_keys(valid_env):
    assert load_settings("api", valid_env).langfuse.enabled is False
    valid_env["LANGFUSE_PUBLIC_KEY"] = "pk"
    assert load_settings("api", valid_env).langfuse.enabled is False
    valid_env.update({"LANGFUSE_SECRET_KEY": "sk", "LANGFUSE_BASE_URL": "http://localhost:3000"})

    langfuse = load_settings("api", valid_env).langfuse

    assert langfuse.enabled is True
    assert langfuse.base_url == "http://localhost:3000"


def test_enabled_langfuse_requires_a_base_url(valid_env):
    valid_env.update({"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"})

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)

    assert raised.value.variables == ("LANGFUSE_BASE_URL",)


def test_an_unknown_role_is_a_programming_error(valid_env):
    with pytest.raises(ValueError, match="Unknown role"):
        load_settings("check", valid_env)


def test_the_code_defaults_of_the_public_contract(valid_env):
    for name in ("DATABASE_PORT", "GOOGLE_CLOUD_PROJECT", "AUTH_MODE", "OIDC_ISSUER", "AUTH_DEV_SIGNING_KEY"):
        valid_env.pop(name)

    api = load_settings("api", {**valid_env, "OIDC_ISSUER": "https://idp.example.com/realms/app"})

    assert api.database.port == 5432
    assert api.storage.google_cloud_project is None
    assert api.auth.mode == "oidc"
    assert api.auth.issuer == "https://idp.example.com/realms/app"


def test_a_blank_auth_mode_means_oidc(valid_env):
    valid_env.update(AUTH_MODE="  ", OIDC_ISSUER="https://idp.example.com/realms/app")
    del valid_env["AUTH_DEV_SIGNING_KEY"]

    assert load_settings("api", valid_env).auth.mode == "oidc"


def test_oidc_mode_needs_the_issuer_and_dev_mode_does_not(valid_env):
    del valid_env["OIDC_ISSUER"]
    assert load_settings("api", valid_env).auth.issuer is None  # dev mode

    valid_env.update(AUTH_MODE="oidc")
    del valid_env["AUTH_DEV_SIGNING_KEY"]
    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)
    assert raised.value.variables == ("OIDC_ISSUER",)


def test_the_embedding_dimension_defaults_to_1536(valid_env):
    env = {**valid_env, "GRAPH_LLM_MODEL": "a", "GRAPH_SMALL_MODEL": "b", "GRAPH_EMBEDDING_MODEL": "c"}

    assert GraphBuildSettings(**env).embedding_dim == RUNTIME_DEFAULTS["GRAPH_EMBEDDING_DIM"]
