"""Tier 1 tests of ``agent_runtime.tracing`` (AC-11.1) and the plugin order.

The OpenTelemetry global provider can be set once per process, so the enabled
branch runs against recorders. The live proof that tracing off emits nothing
is the tier-2 test in ``test_specflow_temporal_core_live.py`` (AC-11.2).
"""

from __future__ import annotations

import ast
import base64
import os
from pathlib import Path

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http import trace_exporter
from opentelemetry.sdk.trace import export
from temporalio.contrib import opentelemetry as temporal_otel

from agent_runtime import plugin as plugin_module
from agent_runtime import tracing
from settings import load_settings
from specflow_service_support import BACKEND_SRC, service_python_files

PUBLIC, SECRET = "pk-lf-test-1111", "sk-lf-test-2222"


@pytest.fixture
def lf_env(valid_env):
    return {
        **valid_env,
        "LANGFUSE_BASE_URL": "http://localhost:3000/",
        "LANGFUSE_PUBLIC_KEY": PUBLIC,
        "LANGFUSE_SECRET_KEY": SECRET,
    }


@pytest.fixture
def recorder(monkeypatch):
    """Record SDK and OpenTelemetry calls in order; restore the disable flag env."""

    monkeypatch.setenv(tracing.DISABLE_ENV, "")
    monkeypatch.delenv(tracing.DISABLE_ENV)
    events: list[tuple] = []
    monkeypatch.setattr(tracing, "set_trace_processors", lambda p: events.append(("processors", p)))
    monkeypatch.setattr(tracing, "set_tracing_disabled", lambda d: events.append(("disabled", d)))
    return events


def _install_otel_fakes(monkeypatch, events):
    class Provider:
        def add_span_processor(self, processor):
            events.append(("add_span_processor", processor))

        def force_flush(self):
            events.append(("force_flush",))

        def shutdown(self):
            events.append(("shutdown",))

    provider = Provider()

    def create_tracer_provider(**kwargs):
        events.append(("create_tracer_provider", kwargs))
        return provider

    class Exporter:
        def __init__(self, **kwargs):
            events.append(("exporter", kwargs))

    monkeypatch.setattr(temporal_otel, "create_tracer_provider", create_tracer_provider)
    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", Exporter)
    monkeypatch.setattr(export, "BatchSpanProcessor", lambda exporter: ("batch", exporter))
    monkeypatch.setattr(otel_trace, "set_tracer_provider", lambda p: events.append(("set_tracer_provider", p)))
    return provider


# ------------------------------------------------------------------ disabled
def test_disabled_sets_the_env_flag_before_clearing_the_processors(valid_env, recorder):
    handle = tracing.configure(load_settings("worker", valid_env))

    assert handle.enabled is False
    assert os.environ[tracing.DISABLE_ENV] == "1"
    assert recorder == [("processors", []), ("disabled", True)]


@pytest.mark.parametrize("keys", [{}, {"LANGFUSE_PUBLIC_KEY": PUBLIC}])
def test_one_key_alone_keeps_tracing_off(valid_env, recorder, keys):
    handle = tracing.configure(load_settings("worker", {**valid_env, **keys}))

    assert handle.enabled is False


def test_disabled_shutdown_is_a_noop(valid_env, recorder):
    handle = tracing.configure(load_settings("api", valid_env))

    handle.shutdown()
    handle.shutdown()


# ------------------------------------------------------------------- enabled
def test_enabled_installs_a_replay_safe_provider_with_the_langfuse_exporter(lf_env, recorder, monkeypatch):
    provider = _install_otel_fakes(monkeypatch, recorder)

    handle = tracing.configure(load_settings("worker", lf_env))

    assert handle.enabled is True
    names = [e[0] for e in recorder]
    assert names == [
        "exporter",
        "disabled",
        "processors",
        "create_tracer_provider",
        "add_span_processor",
        "set_tracer_provider",
    ]
    exporter = dict(recorder)["exporter"]
    assert exporter["endpoint"] == "http://localhost:3000/api/public/otel/v1/traces"
    expected = base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    assert exporter["headers"] == {
        "Authorization": f"Basic {expected}",
        "x-langfuse-ingestion-version": "4",
    }
    assert dict(recorder)["processors"] == []
    assert dict(recorder)["disabled"] is False
    resource = dict(recorder)["create_tracer_provider"]["resource"]
    assert resource.attributes["service.name"] == "specflow-worker"
    assert dict(recorder)["set_tracer_provider"] is provider
    assert tracing.DISABLE_ENV not in os.environ


def test_the_service_name_follows_the_role(lf_env, recorder, monkeypatch):
    _install_otel_fakes(monkeypatch, recorder)

    tracing.configure(load_settings("api", lf_env))

    resource = dict(recorder)["create_tracer_provider"]["resource"]
    assert resource.attributes["service.name"] == "specflow-api"


def test_shutdown_flushes_then_shuts_down_once(lf_env, recorder, monkeypatch):
    _install_otel_fakes(monkeypatch, recorder)
    handle = tracing.configure(load_settings("worker", lf_env))
    recorder.clear()

    handle.shutdown()
    handle.shutdown()

    assert recorder == [("force_flush",), ("shutdown",)]


def test_a_failing_exporter_shutdown_never_raises(lf_env, recorder, monkeypatch):
    provider = _install_otel_fakes(monkeypatch, recorder)
    monkeypatch.setattr(provider, "force_flush", lambda: (_ for _ in ()).throw(OSError("down")), raising=False)
    handle = tracing.configure(load_settings("worker", lf_env))

    handle.shutdown()


# Plugin order (AC-11.1)
def test_building_the_otel_plugin_before_configure_raises(lf_env, monkeypatch):
    """The wrong order: the global provider is not the replay-safe one yet."""

    monkeypatch.setattr(otel_trace, "get_tracer_provider", object)

    with pytest.raises(ValueError, match="ReplaySafeTracerProvider"):
        plugin_module.build_plugin(load_settings("worker", lf_env), otel=True)


def test_building_the_otel_plugin_after_configure_works(lf_env, recorder, monkeypatch):
    real = temporal_otel.create_tracer_provider(shutdown_on_exit=False)

    def real_create(**_kwargs):
        return real

    monkeypatch.setattr(temporal_otel, "create_tracer_provider", real_create)
    installed: list = []
    monkeypatch.setattr(otel_trace, "set_tracer_provider", installed.append)
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: installed[-1])
    settings = load_settings("worker", lf_env)

    tracing.configure(settings)
    plugin = plugin_module.build_plugin(settings)

    assert installed == [real]
    assert plugin.name() == "OpenAIAgentsPlugin"
    real.shutdown()


def test_otel_follows_langfuse_unless_overridden(valid_env, lf_env, monkeypatch):
    seen: list[bool] = []
    monkeypatch.setattr(
        plugin_module,
        "OpenAIAgentsPlugin",
        lambda **kwargs: seen.append(kwargs["use_otel_instrumentation"]),
    )

    plugin_module.build_plugin(load_settings("worker", valid_env))
    plugin_module.build_plugin(load_settings("worker", lf_env))
    plugin_module.build_plugin(load_settings("worker", lf_env), otel=False)

    assert seen == [False, True, False]


# Static scan of the sources (AC-11.1)
def _references(path: Path, name: str) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.Name) and node.id == name)
        or (isinstance(node, ast.Attribute) and node.attr == name)
        or (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and any(alias.name.split(".")[-1] == name for alias in node.names)
        )
    ]


def test_langfuse_get_client_is_never_referenced_in_specflow():
    """It would attach a second span processor and install a plain provider."""

    hits = {
        str(path.relative_to(BACKEND_SRC)): lines
        for path in service_python_files()
        if (lines := _references(path, "get_client"))
    }

    assert hits == {}


def test_specflow_only_imports_the_agents_integration_from_temporalio_openai_agents():
    """The ``contrib`` copy diverges; only ``temporalio.openai_agents`` is allowed."""

    offenders = [
        str(path.relative_to(BACKEND_SRC))
        for path in service_python_files()
        if "contrib.openai_agents" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_the_static_scanner_finds_a_reference(tmp_path):
    sample = tmp_path / "x.py"
    sample.write_text("from langfuse import get_client\nclient = langfuse.get_client()\n")

    assert _references(sample, "get_client") == [1, 2]
    sample.write_text('"""get_client is mentioned only in text."""\n')
    assert _references(sample, "get_client") == []
