"""
Tier 1 tests of the LLM gateway provider, the plugin and the client wiring order (AC-11.1).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.exceptions import ApplicationError

from agent_runtime import plugin as plugin_module
from agent_runtime.provider import (
    LlmGatewayModelProvider,
    UnconfiguredModelProvider,
)
from agent_runtime.tracing import TracingHandle
from settings import load_settings
from temporal import client as client_module


# -------------------------------------------------------------------- provider
def _agent(valid_env, **env):
    return load_settings("worker", {**valid_env, **env}).agent


def test_provider_builds_a_chat_completions_model_with_one_client(valid_env):
    provider = LlmGatewayModelProvider(_agent(valid_env), default_model="fallback/m")

    first = provider.get_model("explicit/m")
    second = provider.get_model(None)

    assert first.model == "explicit/m"
    assert second.model == "fallback/m"
    assert first._client is second._client
    assert first._client.max_retries == 0
    assert str(first._client.base_url).startswith("https://gateway.example.test/openai")
    assert first._client.timeout == 110.0


def test_provider_never_retries_in_the_sdk_and_keeps_the_key_private(valid_env):
    model = LlmGatewayModelProvider(_agent(valid_env), default_model="m").get_model(None)

    assert model._client.api_key == "gateway-key-3c6e"
    assert "gateway-key-3c6e" not in repr(model._client.base_url)


@pytest.mark.parametrize(
    ("kwargs", "env", "variable"),
    [
        ({"default_model": None}, {}, "AGENT_MODEL"),
        ({"default_model": "  "}, {}, "AGENT_MODEL"),
        ({"default_model": "m"}, {"LLM_GATEWAY_API_KEY": ""}, "LLM_GATEWAY_API_KEY"),
        ({"default_model": "m"}, {"LLM_GATEWAY_API_KEY": "   "}, "LLM_GATEWAY_API_KEY"),
    ],
)
def test_empty_model_or_key_is_a_non_retryable_configuration_error(valid_env, kwargs, env, variable):
    provider = LlmGatewayModelProvider(_agent(valid_env, **env), **kwargs)

    with pytest.raises(ApplicationError) as error:
        provider.get_model(None)

    assert error.value.type == "ConfigurationError"
    assert error.value.non_retryable is True
    assert variable in str(error.value)
    assert "gateway-key-3c6e" not in str(error.value)


def test_empty_key_does_not_create_a_client(valid_env):
    provider = LlmGatewayModelProvider(_agent(valid_env, LLM_GATEWAY_API_KEY=""), default_model="m")

    with pytest.raises(ApplicationError):
        provider.get_model(None)
    assert provider._client is None


def test_unconfigured_provider_refuses_and_names_the_variables():
    with pytest.raises(ApplicationError) as error:
        UnconfiguredModelProvider().get_model("any")

    assert error.value.non_retryable is True
    assert error.value.type == "ConfigurationError"
    assert "LLM_GATEWAY_API_KEY" in str(error.value)


# ---------------------------------------------------------------------- plugin
def test_model_params_match_the_spec():
    params = plugin_module.model_params()

    assert params.start_to_close_timeout == timedelta(seconds=120)
    assert params.schedule_to_close_timeout == timedelta(minutes=5)
    assert params.heartbeat_timeout == timedelta(seconds=30)
    assert params.retry_policy.maximum_attempts == 3
    assert params.retry_policy.non_retryable_error_types == ["ConfigurationError"]
    assert plugin_module.model_params() is not params


@pytest.mark.parametrize("role", ["api", "migrate"])
def test_build_plugin_works_without_llm_gateway_settings(valid_env, role):
    settings = load_settings(role, valid_env)

    assert plugin_module.build_plugin(settings).name() == "OpenAIAgentsPlugin"


def test_build_plugin_uses_the_llm_gateway_when_the_role_has_agent_settings(valid_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        plugin_module,
        "OpenAIAgentsPlugin",
        lambda **kwargs: seen.update(kwargs) or "plugin",
    )

    assert plugin_module.build_plugin(load_settings("worker", valid_env)) == "plugin"

    assert isinstance(seen["model_provider"], LlmGatewayModelProvider)
    assert seen["use_otel_instrumentation"] is False
    assert seen["model_params"].start_to_close_timeout == timedelta(seconds=120)


def test_build_plugin_gives_a_parse_only_worker_the_refusing_provider(valid_env, monkeypatch):
    seen = {}
    monkeypatch.setattr(plugin_module, "OpenAIAgentsPlugin", lambda **kwargs: seen.update(kwargs) or "plugin")

    plugin_module.build_plugin(load_settings("worker", valid_env, queues=["parse"]))

    assert isinstance(seen["model_provider"], UnconfiguredModelProvider)


# ---------------------------------------------------- wiring order (AC-11.1)
def _wire(monkeypatch, events: list, settings_env):
    handle = TracingHandle(enabled=False)
    sentinel = object()

    def configure(settings):
        events.append("tracing.configure")
        return handle

    def build_plugin(settings):
        events.append("build_plugin")
        return sentinel

    async def connect(address, **kwargs):
        events.append("Client.connect")
        connect.kwargs = {"address": address, **kwargs}
        return "client"

    monkeypatch.setattr(client_module.tracing, "configure", configure)
    monkeypatch.setattr(client_module, "build_plugin", build_plugin)
    monkeypatch.setattr(client_module.Client, "connect", connect)
    return handle, sentinel, connect


async def test_connect_configures_tracing_then_builds_the_plugin_then_connects(valid_env, monkeypatch):
    events: list[str] = []
    handle, sentinel, connect = _wire(monkeypatch, events, valid_env)

    connection = await client_module.open_connection(load_settings("worker", valid_env))

    assert events == ["tracing.configure", "build_plugin", "Client.connect"]
    assert connect.kwargs["plugins"] == [sentinel]
    assert connect.kwargs["address"] == "localhost:7233"
    assert connect.kwargs["namespace"] == "specflow-local"
    assert connect.kwargs["api_key"] is None
    assert connect.kwargs["tls"] is False
    assert connection.client == "client"
    assert connection.tracing is handle


async def test_connect_returns_the_client(valid_env, monkeypatch):
    _wire(monkeypatch, [], valid_env)

    assert await client_module.connect(load_settings("api", valid_env)) == "client"


async def test_an_api_key_turns_tls_on(valid_env, monkeypatch):
    events: list[str] = []
    _, _, connect = _wire(monkeypatch, events, valid_env)

    await client_module.open_connection(load_settings("worker", {**valid_env, "TEMPORAL_API_KEY": "temporal-key-1a2b"}))

    assert connect.kwargs["api_key"] == "temporal-key-1a2b"
    assert connect.kwargs["tls"] is True


async def test_client_certificates_are_read_from_a_path_or_given_inline(valid_env, monkeypatch, tmp_path):
    cert = tmp_path / "client.pem"
    cert.write_text("CERT-PEM")
    events: list[str] = []
    _, _, connect = _wire(monkeypatch, events, valid_env)

    await client_module.open_connection(
        load_settings(
            "worker",
            {**valid_env, "TEMPORAL_TLS_CERT": str(cert), "TEMPORAL_TLS_KEY": "KEY-PEM"},
        )
    )

    tls = connect.kwargs["tls"]
    assert tls.client_cert == b"CERT-PEM"
    assert tls.client_private_key == b"KEY-PEM"


async def test_connect_needs_temporal_settings(valid_env):
    with pytest.raises(ValueError, match="has no Temporal settings"):
        await client_module.open_connection(load_settings("migrate", valid_env))


async def test_the_provider_closes_its_client_once_and_not_before_the_first_call(valid_env, monkeypatch):
    """R-code LOW 8: ``aclose`` is idempotent and a no-op before any model was requested."""

    provider = LlmGatewayModelProvider(_agent(valid_env), default_model="m")
    await provider.aclose()  # nothing created yet
    provider.get_model(None)
    client = provider._client
    closed: list[int] = []

    async def fake_close():
        closed.append(1)

    monkeypatch.setattr(client, "close", fake_close)

    await provider.aclose()
    await provider.aclose()

    assert closed == [1]
    assert provider._client is None
