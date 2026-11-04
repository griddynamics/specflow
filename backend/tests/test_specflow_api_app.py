"""Tier 1: server entrypoint (AC-2.1), lifespan wiring and shutdown."""

from __future__ import annotations

from ipaddress import IPv4Address
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider

from api import server
from api.app import create_app, untraced
from api.runtime import Runtime
from health import Readiness
from settings import load_settings
from specflow_api_support import FakeGraphStore, FakeRepo, FakeTemporal, bearer
from specflow_auth_support import retired_dev_token


def test_run_serves_uvicorn_on_all_interfaces_with_the_central_logger_only(monkeypatch, valid_env):
    seen: dict = {}

    class FakeServer:
        def __init__(self, config) -> None:
            seen["config"] = config

        def run(self) -> None:
            seen["ran"] = True

    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    assert server.run(load_settings("api", {**valid_env, "API_PORT": "8123"})) == 0
    config = seen["config"]
    assert (config.host, config.port, config.log_config) == (str(IPv4Address(0)), 8123, None)
    assert seen["ran"] is True


async def lifespan_run(env):
    closed: list[str] = []

    async def factory(settings):
        async def cleanup() -> None:
            closed.append("runtime")

        return Runtime(
            settings=settings,
            session_factory=FakeRepo(),
            store=None,
            temporal=FakeTemporal(),
            graph_store=FakeGraphStore(),
            readiness=Readiness({}),
            cleanups=[cleanup],
        )

    app = create_app(load_settings("api", env), runtime_factory=factory)
    async with app.router.lifespan_context(app):
        assert app.state.token_verifier is not None and app.state.runtime is not None
    return closed


async def test_the_lifespan_builds_the_verifier_and_closes_the_runtime(valid_env):
    closed = await lifespan_run(valid_env)
    assert closed == ["runtime"]


async def test_a_missing_issuer_fails_the_start_before_any_resource_is_built(valid_env):
    settings = load_settings("api", valid_env)
    no_issuer = settings.auth.model_copy(update={"issuer": None})
    built: list[bool] = []

    async def factory(settings):
        built.append(True)

    app = create_app(
        SimpleNamespace(**{**settings.__dict__, "auth": no_issuer}),
        runtime_factory=factory,
    )
    with pytest.raises(ValueError, match="OIDC_ISSUER is required"):
        async with app.router.lifespan_context(app):
            pass
    assert built == []


def test_docs_are_served_only_locally(valid_env):
    local = create_app(load_settings("api", valid_env))
    assert local.openapi_url == "/openapi.json"
    deployed_env = {
        **valid_env,
        "APP_ENV": "dev",
        "OIDC_ISSUER": "https://idp.example.com/realms/x",
        "AUTH_JWKS_URL": "",
        "STORAGE_PREFIX": "dev/shared/",
    }
    deployed = create_app(load_settings("api", deployed_env))
    assert deployed.openapi_url is None and deployed.docs_url is None


async def test_probe_requests_make_no_spans_but_other_requests_do(monkeypatch, valid_env):
    """Offline span-processor fake: /healthz and /readyz are not exported (Langfuse noise)."""

    class Recorder(SpanProcessor):
        def __init__(self) -> None:
            self.names: list[str] = []
            self.paths: list[str] = []

        def on_end(self, span) -> None:
            self.names.append(span.name)
            self.paths.append(str((span.attributes or {}).get("url.path")))

    recorder = Recorder()
    provider = TracerProvider()
    provider.add_span_processor(recorder)

    async def factory(settings):
        return Runtime(
            settings=settings,
            session_factory=FakeRepo(),
            store=None,
            temporal=FakeTemporal(),
            graph_store=FakeGraphStore(),
            readiness=Readiness({}, ttl=0),
        )

    app = create_app(load_settings("api", valid_env), runtime_factory=factory)
    app._telemetry["tracer_provider"] = provider  # the global provider is set once per process

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        await client.get("/readyz")
        await client.get("/api/v1/me")  # 401 without a token, but it is traced

    assert "/healthz" not in recorder.paths and "/readyz" not in recorder.paths
    assert recorder.paths.count("/api/v1/me") >= 1


def test_the_exclude_hook_names_only_the_two_probes():

    assert untraced({"path": "/healthz"}) and untraced({"path": "/readyz"})
    assert not untraced({"path": "/api/v1/projects/x/documents/y/summarize"})
    assert not untraced({})


async def test_a_removed_dev_login_token_is_401_and_a_run_idp_token_is_accepted(harness):
    """B-05: HS256 and the removed dev login issuer never authenticate; the IdP's RS256 token does."""

    forged = await harness.client.get("/api/v1/me", headers={"Authorization": f"Bearer {retired_dev_token()}"})
    valid = await harness.client.get("/api/v1/me", headers=bearer())

    assert forged.status_code == 401
    assert forged.headers["www-authenticate"] == "Bearer"
    assert forged.json()["error"]["code"] == "invalid_token"
    assert valid.status_code == 200
