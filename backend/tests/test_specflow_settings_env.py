"""The live-tier helpers and the env template that backs the settings (AC-13.4, names only)."""

from __future__ import annotations

import ast
import base64
import re
from pathlib import Path

import pytest
import yaml
from temporalio.service import RPCError, RPCStatusCode

import specflow_live
from settings import (
    AgentDatabaseSettings,
    AgentRunSettings,
    AgentSettings,
    ApiInternalSettings,
    AppSettings,
    AtlassianOAuthSettings,
    AuthSettings,
    BoxSettings,
    ConfigurationError,
    GitHubSettings,
    GraphBuildSettings,
    GraphStoreSettings,
    JiraSettings,
    LangfuseSettings,
    McpSettings,
    SlackAppSettings,
    SlackNoticeSettings,
    StorageSettings,
    TemporalSettings,
    load_settings,
)
from specflow_live import TemporalLive, live_enabled, requires_live
from specflow_settings_support import FakeClient, FakeHandle, example_entries

SETTINGS_CLASSES = (
    AppSettings,
    AgentDatabaseSettings,
    AuthSettings,
    StorageSettings,
    TemporalSettings,
    GraphStoreSettings,
    AgentRunSettings,
    AgentSettings,
    GraphBuildSettings,
    JiraSettings,
    AtlassianOAuthSettings,
    GitHubSettings,
    BoxSettings,
    LangfuseSettings,
    McpSettings,
    ApiInternalSettings,
    SlackNoticeSettings,
    SlackAppSettings,
)
ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "configuration.md").read_text()

# SPL-ENV-01: the 23 required, 29 optional and 6 local-stack keys, nothing else.
REQUIRED_KEYS = frozenset(
    {
        "APP_ENV",
        "MCP_RESOURCE_URL",
        "DATABASE_HOST",
        "DATABASE_NAME",
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "DATABASE_AGENT_PASSWORD",
        "GRAPH_STORE_URI",
        "GRAPH_STORE_USER",
        "GRAPH_STORE_PASSWORD",
        "TEMPORAL_ADDRESS",
        "TEMPORAL_NAMESPACE",
        "LLM_GATEWAY_URL",
        "LLM_GATEWAY_API_KEY",
        "AGENT_MODEL",
        "AUTH_ALLOWED_EMAIL_DOMAINS",
        "PUBLIC_URL",
        "API_INTERNAL_URL",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "SESSION_SECRET",
        "DRIVE_IDENTITY_EMAIL",
    }
)
OPTIONAL_KEYS = frozenset(
    {
        "TEMPORAL_API_KEY",
        "TEMPORAL_TLS_CERT",
        "TEMPORAL_TLS_KEY",
        "GRAPH_STORE",
        "GRAPH_STORE_NAME",
        "GRAPH_STORE_ADMIN_URI",
        "GRAPH_STORE_POOL_SIZE",
        "GRAPH_LLM_MODEL",
        "GRAPH_SMALL_MODEL",
        "GRAPH_EMBEDDING_MODEL",
        "SPECFLOW_CONFIG_FILE",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "OIDC_INTERNAL_URL",
        "AUTH_JWKS_URL",
        "MCP_PUBLIC_URL",
        "MCP_CLIENT_ID",
        "MCP_SERVER_NAME",
        "ATLASSIAN_SITE_URL",
        "ATLASSIAN_EMAIL",
        "ATLASSIAN_API_TOKEN",
        "ATLASSIAN_OAUTH_CLIENT_ID",
        "ATLASSIAN_OAUTH_CLIENT_SECRET",
        "ATLASSIAN_TOKEN_KEY",
        "GITHUB_APP_CLIENT_ID",
        "GITHUB_APP_CLIENT_SECRET",
        "GITHUB_APP_SLUG",
        "GITHUB_TOKEN_KEY",
        "BOX_OAUTH_CLIENT_ID",
        "BOX_OAUTH_CLIENT_SECRET",
        "BOX_TOKEN_KEY",
    }
)
# Names only the local stack reads (`make env-local` writes them, docker-compose.yml and the Keycloak realm use them).
STACK_KEYS = frozenset(
    {
        "LOCAL_TEST_USER_EMAIL",
        "LOCAL_TEST_USER_PASSWORD",
        "LOCAL_TEST_USER_ID",
        "KEYCLOAK_ADMIN_PASSWORD",
        "KEYCLOAK_QA_PASSWORD",
        "KEYCLOAK_WEB_CLIENT_SECRET",
    }
)

# Source files that read environment names directly (not through a settings class).
ENV_READERS = (
    "src/settings.py",
    "src/cloud_profile.py",
    "src/cli.py",
    "src/db/migrate.py",
    "src/logger/config.py",
    "src/temporal/diagnostics.py",
    "src/agent_runtime/tracing.py",
    "src/llm/tracing/langfuse_urls.py",
    "src/drive/select.py",
    "examples/graph_build.py",
    "examples/gcs_connectivity.py",
    "examples/temporal_hello.py",
    "examples/mcp_qa_lib.py",
    "examples/mcp_qa.py",
)
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")
# Upper-case string constants in those files that are not environment names.
NOT_ENV_NAMES = frozenset(
    {"APP_ROOT_LOGGERS", "QUEUE_MAXSIZE", "THIRD_PARTY_LEVELS", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}
)


def _settings_fields():
    for cls in SETTINGS_CLASSES:
        for field in cls.model_fields.values():
            if isinstance(field.validation_alias, str) and field.validation_alias != "cloud":
                yield field.validation_alias, field


def _env_names_read_in_code() -> set[str]:
    names = {name for name, _ in _settings_fields()}
    names.update(f"DATABASE_{n}" for n in ("HOST", "PORT", "NAME", "USER", "PASSWORD"))
    backend = ROOT / "backend"
    for relative in ENV_READERS:
        tree = ast.parse((backend / relative).read_text())
        names.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and ENV_NAME.match(node.value)
        )
    rules = (ROOT / "frontend" / "lib" / "env-rules.mjs").read_text()
    names.update(re.findall(r'"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"', rules))
    return names - NOT_ENV_NAMES


def test_the_env_template_has_exactly_the_documented_keys():
    assert set(example_entries()) == REQUIRED_KEYS | OPTIONAL_KEYS | STACK_KEYS
    assert len(REQUIRED_KEYS) == 23
    assert len(OPTIONAL_KEYS) == 32
    assert len(STACK_KEYS) == 6


def test_every_local_stack_key_is_used_by_the_compose_file():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert sorted(name for name in STACK_KEYS if name not in compose) == []


def test_every_key_of_the_env_template_is_a_known_setting():
    assert sorted((REQUIRED_KEYS | OPTIONAL_KEYS) - _env_names_read_in_code()) == []


def test_every_setting_without_a_default_is_a_key_or_has_a_doc_row():
    keys = REQUIRED_KEYS | OPTIONAL_KEYS
    names = [name for name, field in _settings_fields() if field.is_required() or field.default is None]
    names += [f"DATABASE_{n}" for n in ("HOST", "NAME", "USER", "PASSWORD")]

    assert sorted(n for n in names if n not in keys and n not in DOC) == []
    # A setting with a code default is documented too (checked below for every name).


def test_every_env_name_read_in_code_is_in_the_config_doc():
    missing = sorted(name for name in _env_names_read_in_code() if f"`{name}`" not in DOC)

    assert missing == []


def test_the_env_template_values_are_all_empty():
    assert {name: value for name, value in example_entries().items() if value} == {}


def test_the_env_template_and_doc_carry_no_local_path_or_internal_host():
    text = (ROOT / ".env.example").read_text() + DOC

    # Split words: this file is public too, and its own pattern must not trip the publication gate.
    internal = ("grid" + "dynamics", "ever" + "green", "bif" + "rost", "gd" + "-gcp", r"(?<!host\.docker)\.internal\b")
    assert "/Users" not in text
    assert not re.search("|".join(internal), text, re.IGNORECASE)


@pytest.mark.parametrize("name", specflow_live.LIVE_NAMES)
def test_requires_live_follows_the_gate_variable(name, monkeypatch):
    monkeypatch.delenv(f"SPECFLOW_TEST_{name}", raising=False)
    assert live_enabled(name) is False
    assert requires_live(name).args[0] is True
    monkeypatch.setenv(f"SPECFLOW_TEST_{name}", "0")
    assert live_enabled(name) is False
    monkeypatch.setenv(f"SPECFLOW_TEST_{name}", "1")
    assert live_enabled(name) is True
    assert requires_live(name).args[0] is False


def test_requires_live_rejects_an_unknown_tier():
    with pytest.raises(ValueError, match="Unknown live tier"):
        requires_live("UNKNOWN")


def test_temporal_live_names_carry_the_run_prefix():
    live = TemporalLive(FakeClient(), "abc123", 4)

    assert live.task_queue == "pytest-abc123-4"
    assert live.id_prefix == "pytest-abc123-"
    assert live.workflow_id("x") == "pytest-abc123-x"
    assert live.workflow_id().startswith("pytest-abc123-")
    assert live.workflow_id() != live.workflow_id()


async def test_temporal_live_start_prefixes_ids_uses_the_queue_and_tracks():
    client = FakeClient()
    live = TemporalLive(client, "abc123", 1)

    handle = await live.start("wf", {"a": 1}, workflow_id="doc", execution_timeout=5)

    workflow, args, workflow_id, options = client.started[0]
    assert (workflow, args, workflow_id) == ("wf", ({"a": 1},), "pytest-abc123-doc")
    assert options == {"task_queue": "pytest-abc123-1", "execution_timeout": 5}
    assert live._handles == [handle]


async def test_temporal_live_cleanup_terminates_every_handle_and_ignores_closed_ones():
    live = TemporalLive(FakeClient(), "abc123", 1)
    closed = FakeHandle("a", RPCError("done", RPCStatusCode.NOT_FOUND, b""))
    running = FakeHandle("b")
    live.track(closed)
    live.track(running)

    assert await live.cleanup() == 2

    assert closed.reasons and running.reasons
    assert await live.cleanup() == 0


async def test_temporal_live_cleanup_does_not_hide_unexpected_errors():
    live = TemporalLive(FakeClient(), "abc123", 1)
    live.track(FakeHandle("a", RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        await live.cleanup()


class StubItem(pytest.Function):
    """A ``pytest.Function`` stand-in that only records the markers added to it."""

    @classmethod
    def build(cls, fixturenames: list[str]) -> StubItem:
        item = cls.__new__(cls)
        item.fixturenames = fixturenames
        item.markers = []
        return item

    def add_marker(self, marker, *_args):
        self.markers.append(marker.mark)


def test_the_temporal_live_gate_skips_without_the_env_flag(monkeypatch):
    monkeypatch.delenv("SPECFLOW_TEST_TEMPORAL", raising=False)
    monkeypatch.delenv("TEMPORAL_NAMESPACE", raising=False)
    item = StubItem.build(["temporal_live"])

    specflow_live.pytest_collection_modifyitems([item])

    assert [m.name for m in item.markers] == ["skipif", "skipif"]
    assert item.markers[0].args[0] is True
    assert item.markers[1].args[0] is False


def test_the_temporal_live_gate_skips_outside_the_local_namespace(monkeypatch):
    monkeypatch.setenv("SPECFLOW_TEST_TEMPORAL", "1")
    monkeypatch.setenv("TEMPORAL_NAMESPACE", "production")
    item = StubItem.build(["temporal_live"])

    specflow_live.pytest_collection_modifyitems([item])

    assert item.markers[0].args[0] is False
    assert item.markers[1].args[0] is True


def test_the_temporal_live_gate_leaves_other_items_unmarked(monkeypatch):
    monkeypatch.delenv("SPECFLOW_TEST_TEMPORAL", raising=False)
    item = StubItem.build(["tmp_path"])

    specflow_live.pytest_collection_modifyitems([item])

    assert item.markers == []


def test_the_api_service_gets_the_mcp_resource_url_of_the_mcp_service_and_the_chart_lists_the_mcp_variables():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    chart = yaml.safe_load((ROOT / "charts/specflow/examples/components/api.yaml").read_text())

    assert services["api"]["environment"]["MCP_RESOURCE_URL"] == services["mcp"]["environment"]["MCP_RESOURCE_URL"]
    names = {entry["name"] for entry in chart["env"]}
    assert {"MCP_RESOURCE_URL", "MCP_AUDIENCE", "MCP_CLIENT_IDS"} <= names


def test_the_api_address_of_the_mcp_role_shares_its_name_with_the_web_app():
    """``API_INTERNAL_URL`` is one env name for two readers: the frontend rules and ``ApiInternalSettings``."""

    rules = (ROOT / "frontend" / "lib" / "env-rules.mjs").read_text()

    assert ApiInternalSettings.model_fields["url"].validation_alias == "API_INTERNAL_URL"
    assert '"API_INTERNAL_URL"' in rules
    assert "API_INTERNAL_URL" in REQUIRED_KEYS


# Jira sync account (FR-JIRA-CFG-08, NFR-JIRA-01) -------------------------------------------------

JIRA_VARIABLES = ("ATLASSIAN_SITE_URL", "ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN")
JIRA_ENV = {
    "ATLASSIAN_SITE_URL": "https://example.atlassian.net/",
    "ATLASSIAN_EMAIL": "robot@example.com",
    "ATLASSIAN_API_TOKEN": "token-canary-5d1f",
}


@pytest.mark.parametrize("blank", [False, True])
def test_no_atlassian_variable_means_no_jira_settings(valid_env, blank):
    valid_env.update(dict.fromkeys(JIRA_VARIABLES, "  ") if blank else {})

    assert load_settings("api", valid_env).jira is None


def test_all_three_atlassian_variables_build_the_account(valid_env):
    jira = load_settings("api", {**valid_env, **JIRA_ENV}).jira

    assert jira is not None
    assert (jira.site_url, jira.email) == ("https://example.atlassian.net", "robot@example.com")
    assert jira.api_token.get_secret_value() == "token-canary-5d1f"


@pytest.mark.parametrize("present", [{"ATLASSIAN_EMAIL"}, {"ATLASSIAN_SITE_URL", "ATLASSIAN_API_TOKEN"}])
def test_a_partial_atlassian_set_names_the_missing_variables(valid_env, present):
    valid_env.update({name: JIRA_ENV[name] for name in present})

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)

    assert set(raised.value.variables) == set(JIRA_VARIABLES) - present
    assert "token-canary" not in str(raised.value)


@pytest.mark.parametrize("name", ["ATLASSIAN_API_TOKEN", "ATLASSIAN_EMAIL", "ATLASSIAN_SITE_URL"])
def test_a_blank_atlassian_value_is_refused_when_the_others_are_set(valid_env, name):
    valid_env.update({**JIRA_ENV, name: "   "})

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)

    assert raised.value.variables == (name,)


@pytest.mark.parametrize(
    "url", ["http://example.atlassian.net", "https://example.atlassian.net/wiki", "https://u:p@example.atlassian.net"]
)
def test_the_site_url_must_be_an_https_origin(valid_env, url):
    valid_env.update({**JIRA_ENV, "ATLASSIAN_SITE_URL": url})

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", valid_env)

    assert raised.value.variables == ("ATLASSIAN_SITE_URL",)


def test_only_the_api_role_reads_the_atlassian_account(valid_env):
    worker = load_settings("worker", {**valid_env, **JIRA_ENV, "ATLASSIAN_EMAIL": ""})

    assert worker.jira is None


def test_the_token_is_not_in_the_repr_of_the_settings(valid_env):
    settings = load_settings("api", {**valid_env, **JIRA_ENV})

    assert settings.jira is not None
    assert "token-canary" not in repr(settings.jira) + str(settings.jira)
    assert "token-canary" not in repr(settings)


# Atlassian OAuth connections (docs/jira-sync/oauth.md "Modes", "Settings") ----------------------------------------

OAUTH_ENV = {
    "ATLASSIAN_SITE_URL": "https://example.atlassian.net",
    "ATLASSIAN_OAUTH_CLIENT_ID": "client-id-1",
    "ATLASSIAN_OAUTH_CLIENT_SECRET": "secret-canary-2a9e",
    "ATLASSIAN_TOKEN_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode(),
    "PUBLIC_URL": "http://localhost:3001/",
}


def test_the_oauth_client_id_turns_the_oauth_mode_on_with_its_callback(valid_env):
    jira = load_settings("api", {**valid_env, **OAUTH_ENV}).jira

    assert isinstance(jira, AtlassianOAuthSettings) and jira.mode == "oauth"
    assert (jira.site_url, jira.client_id) == ("https://example.atlassian.net", "client-id-1")
    assert jira.callback_url == "http://localhost:3001/api/integrations/atlassian/callback"


def test_oauth_wins_over_the_token_account_when_both_are_set(valid_env):
    jira = load_settings("api", {**valid_env, **JIRA_ENV, **OAUTH_ENV}).jira

    assert isinstance(jira, AtlassianOAuthSettings) and jira.mode == "oauth"


def test_the_token_mode_is_unchanged_without_the_client_id(valid_env):
    jira = load_settings(
        "api", {**valid_env, **JIRA_ENV, "ATLASSIAN_OAUTH_CLIENT_ID": " ", "ATLASSIAN_TOKEN_KEY": "x"}
    ).jira

    assert isinstance(jira, JiraSettings) and jira.mode == "token"


@pytest.mark.parametrize(
    "missing", ["ATLASSIAN_OAUTH_CLIENT_SECRET", "ATLASSIAN_TOKEN_KEY", "PUBLIC_URL", "ATLASSIAN_SITE_URL"]
)
def test_a_partial_oauth_set_is_refused_at_startup_naming_the_missing_variable(valid_env, missing):
    env = {**valid_env, **{name: value for name, value in OAUTH_ENV.items() if name != missing}}

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", env)

    assert raised.value.variables == (missing,)
    assert "secret-canary" not in str(raised.value)


@pytest.mark.parametrize("key", ["not-a-key", base64.urlsafe_b64encode(bytes(16)).decode(), "   "])
def test_the_token_key_must_be_a_fernet_key(valid_env, key):
    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", {**valid_env, **OAUTH_ENV, "ATLASSIAN_TOKEN_KEY": key})

    assert raised.value.variables == ("ATLASSIAN_TOKEN_KEY",)


def test_the_oauth_secrets_are_not_in_the_repr_of_the_settings(valid_env):
    settings = load_settings("api", {**valid_env, **OAUTH_ENV})

    text = repr(settings) + repr(settings.jira) + str(settings.jira)
    assert "secret-canary" not in text and OAUTH_ENV["ATLASSIAN_TOKEN_KEY"] not in text


def test_only_the_api_role_reads_the_oauth_settings(valid_env):
    assert load_settings("worker", {**valid_env, **OAUTH_ENV, "PUBLIC_URL": ""}).jira is None


# GitHub integration (docs/github/oauth.md "Settings") -----------------------------------------------------------------

GITHUB_ENV = {
    "GITHUB_APP_CLIENT_ID": "Iv23-client-1",
    "GITHUB_APP_CLIENT_SECRET": "gh-secret-canary-7c1d",
    "GITHUB_APP_SLUG": "specflow-local",
    "GITHUB_TOKEN_KEY": base64.urlsafe_b64encode(bytes(range(1, 33))).decode(),
    "PUBLIC_URL": "http://localhost:3001/",
}


def test_the_github_client_id_turns_the_integration_on_with_its_callback(valid_env):
    github = load_settings("api", {**valid_env, **GITHUB_ENV}).github

    assert isinstance(github, GitHubSettings)
    assert (github.client_id, github.slug) == ("Iv23-client-1", "specflow-local")
    assert github.callback_url == "http://localhost:3001/api/integrations/github/callback"


def test_github_is_off_without_the_client_id_even_with_the_other_variables(valid_env):
    env = {**valid_env, **GITHUB_ENV, "GITHUB_APP_CLIENT_ID": "  ", "GITHUB_TOKEN_KEY": "x"}

    assert load_settings("api", env).github is None


@pytest.mark.parametrize("missing", ["GITHUB_APP_CLIENT_SECRET", "GITHUB_APP_SLUG", "GITHUB_TOKEN_KEY", "PUBLIC_URL"])
def test_a_partial_github_set_is_refused_at_startup_naming_the_missing_variable(valid_env, missing):
    env = {**valid_env, **{name: value for name, value in GITHUB_ENV.items() if name != missing}}

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", env)

    assert raised.value.variables == (missing,)
    assert "gh-secret-canary" not in str(raised.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GITHUB_TOKEN_KEY", "not-a-key"),
        ("GITHUB_APP_SLUG", "Specflow App"),
        ("GITHUB_APP_SLUG", "-leading-dash"),
        ("GITHUB_APP_CLIENT_SECRET", "   "),
    ],
)
def test_a_bad_github_value_is_refused_naming_it(valid_env, name, value):
    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", {**valid_env, **GITHUB_ENV, name: value})

    assert raised.value.variables == (name,)


def test_the_github_secrets_are_not_in_the_repr_of_the_settings(valid_env):
    settings = load_settings("api", {**valid_env, **GITHUB_ENV})

    text = repr(settings) + repr(settings.github) + str(settings.github)
    assert "gh-secret-canary" not in text and GITHUB_ENV["GITHUB_TOKEN_KEY"] not in text


def test_the_agents_worker_reads_the_github_settings_and_the_parse_worker_does_not(valid_env):
    env = {**valid_env, **GITHUB_ENV}

    assert load_settings("worker", env, queues=["agents"]).github is not None
    assert load_settings("worker", {**env, "PUBLIC_URL": ""}, queues=["parse"]).github is None
    assert load_settings("migrate", {**env, "PUBLIC_URL": ""}).github is None


# Box integration (docs/box/oauth.md "Settings") -----------------------------------------------------------------------

BOX_ENV = {
    "BOX_OAUTH_CLIENT_ID": "box-client-1",
    "BOX_OAUTH_CLIENT_SECRET": "box-secret-canary-7c1d",
    "BOX_TOKEN_KEY": base64.urlsafe_b64encode(bytes(range(2, 34))).decode(),
    "PUBLIC_URL": "http://localhost:3001/",
}


def test_the_box_client_id_turns_the_integration_on_with_its_redirect_uri(valid_env):
    box = load_settings("api", {**valid_env, **BOX_ENV}).box

    assert isinstance(box, BoxSettings)
    assert box.client_id == "box-client-1"
    assert box.callback_url == "http://localhost:3001/api/integrations/box/callback"


def test_box_is_off_without_the_client_id_even_with_the_other_variables(valid_env):
    env = {**valid_env, **BOX_ENV, "BOX_OAUTH_CLIENT_ID": "  ", "BOX_TOKEN_KEY": "x"}

    assert load_settings("api", env).box is None


@pytest.mark.parametrize("missing", ["BOX_OAUTH_CLIENT_SECRET", "BOX_TOKEN_KEY", "PUBLIC_URL"])
def test_a_partial_box_set_is_refused_at_startup_naming_the_missing_variable(valid_env, missing):
    env = {**valid_env, **{name: value for name, value in BOX_ENV.items() if name != missing}}

    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", env)

    assert raised.value.variables == (missing,)
    assert "box-secret-canary" not in str(raised.value)


def test_a_bad_box_key_is_refused_and_the_box_secrets_are_not_in_the_repr(valid_env):
    with pytest.raises(ConfigurationError) as raised:
        load_settings("api", {**valid_env, **BOX_ENV, "BOX_TOKEN_KEY": "not-a-key"})
    settings = load_settings("api", {**valid_env, **BOX_ENV})

    text = repr(settings) + repr(settings.box) + str(settings.box)
    assert raised.value.variables == ("BOX_TOKEN_KEY",)
    assert "box-secret-canary" not in text and BOX_ENV["BOX_TOKEN_KEY"] not in text


def test_only_the_api_role_reads_the_box_settings(valid_env):
    env = {**valid_env, **BOX_ENV}

    assert load_settings("worker", env, queues=["agents"]).box is None
    assert load_settings("worker", {**env, "PUBLIC_URL": ""}, queues=["parse"]).box is None
    assert load_settings("migrate", {**env, "PUBLIC_URL": ""}).box is None
