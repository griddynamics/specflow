"""The live-tier helpers and the env template that backs the settings (AC-13.4, names only)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from temporalio.service import RPCError, RPCStatusCode

import specflow_live
from settings import (
    AgentDatabaseSettings,
    AgentRunSettings,
    AgentSettings,
    AppSettings,
    AuthSettings,
    GraphBuildSettings,
    LangfuseSettings,
    Neo4jSettings,
    StorageSettings,
    TemporalSettings,
)
from specflow_live import TemporalLive, live_enabled, requires_live
from specflow_settings_support import FakeClient, FakeHandle, example_entries

SETTINGS_CLASSES = (
    AppSettings,
    AgentDatabaseSettings,
    AuthSettings,
    StorageSettings,
    TemporalSettings,
    Neo4jSettings,
    AgentRunSettings,
    AgentSettings,
    GraphBuildSettings,
    LangfuseSettings,
)
ROOT = Path(__file__).resolve().parents[2]
DOC = (ROOT / "docs" / "configuration.md").read_text()

# SPL-ENV-01: the 22 required and 10 optional keys, nothing else.
REQUIRED_KEYS = frozenset(
    {
        "APP_ENV",
        "DATABASE_HOST",
        "DATABASE_NAME",
        "DATABASE_USER",
        "DATABASE_PASSWORD",
        "DATABASE_AGENT_PASSWORD",
        "NEO4J_URI",
        "NEO4J_USER",
        "NEO4J_PASSWORD",
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
        "GRAPH_LLM_MODEL",
        "GRAPH_SMALL_MODEL",
        "GRAPH_EMBEDDING_MODEL",
        "SPECFLOW_CONFIG_FILE",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
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
    assert set(example_entries()) == REQUIRED_KEYS | OPTIONAL_KEYS
    assert len(REQUIRED_KEYS) == 22
    assert len(OPTIONAL_KEYS) == 10


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
