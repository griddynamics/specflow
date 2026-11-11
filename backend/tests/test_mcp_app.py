"""The MCP app: PRM, probes, lifespan events, SDK session, rebinding settings (FR-MCP-AUTH-02, FR-MCP-OPS-02)."""

from __future__ import annotations

import subprocess
import sys
import tomllib

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from mcp_support import (
    BACKEND,
    MCP_URL,
    ONE_MIB,
    READY_BODY,
    build_app,
    mcp_env,
    mcp_token,
    oidc_env,
    ready_api,
    rpc,
    sdk_session,
    serving,
    whoami_tools,
)
from settings import load_settings
from specflow_auth_support import session_idp

PRM_PATHS = ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource")


def event_names(caplog) -> list[str]:
    return [r.event for r in caplog.records if hasattr(r, "event")]


# --- FR-MCP-AUTH-02: the protected resource metadata --------------------------


async def test_both_prm_paths_serve_the_same_four_key_document(oidc_app, oidc_rig):
    async with serving(oidc_app) as client:
        first, second = [await client.get(path) for path in PRM_PATHS]

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json() == {
        "resource": MCP_URL,
        "authorization_servers": [oidc_rig.stub.issuer],
        "bearer_methods_supported": ["header"],
        "resource_name": "SpecFlow",
    }


async def test_the_prm_needs_no_token_and_ignores_forged_host_headers(oidc_app):
    """FR-MCP-AUTH-02: built from settings only, served for any Host."""

    async with serving(oidc_app) as client:
        plain = await client.get(PRM_PATHS[0])
        forged = await client.get(PRM_PATHS[0], headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example"})

    assert forged.status_code == 200
    assert forged.json() == plain.json()


async def test_the_prm_is_served_for_a_host_in_mcp_allowed_hosts(valid_env, oidc_rig):
    app = build_app(oidc_env(valid_env, oidc_rig.stub, MCP_ALLOWED_HOSTS="other.example"))
    async with serving(app) as client:
        response = await client.get(PRM_PATHS[0], headers={"Host": "other.example"})

    assert response.status_code == 200
    assert response.json()["resource"] == MCP_URL


async def test_the_prm_answers_get_only_and_has_no_trailing_slash_variant(oidc_app):
    async with serving(oidc_app) as client:
        posted = await client.post(PRM_PATHS[0])
        slash = await client.get(PRM_PATHS[0] + "/")

    assert (posted.status_code, slash.status_code) == (405, 404)


async def test_the_prm_is_always_served_and_names_the_configured_issuer(whoami_app):
    """FR-MCP-AUTH-02: both well-known paths answer 200 on the run's IdP too (no mode without a PRM)."""

    async with serving(whoami_app) as client:
        responses = [await client.get(path) for path in PRM_PATHS]

    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json()["authorization_servers"] == [session_idp().issuer]


# --- FR-MCP-OPS-02: probes -----------------------------------------------------


async def test_healthz_and_a_ready_readyz(whoami_app):
    async with serving(whoami_app) as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert (health.status_code, health.json()) == (200, {"status": "ok"})
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert {name: check["ok"] for name, check in body["checks"].items()} == {"database": True, "graph": True}


def api_ready(*, graph_ok: bool = True, database_ok: bool = True) -> httpx.MockTransport:
    """A fake API whose ``/readyz`` answers the four checks of the real one (the MCP role passes two on)."""

    def check(detail: str) -> dict[str, object]:
        return {"ok": detail == "ok", "detail": detail, "duration_ms": 3}

    body = {
        "status": "ready" if graph_ok and database_ok else "not_ready",
        "checks": {
            "database": check("ok" if database_ok else "unreachable"),
            "temporal": check("ok"),
            "storage": check("ok"),
            "graph": check("ok" if graph_ok else "neo4j_unreachable"),
        },
    }
    return httpx.MockTransport(lambda _request: httpx.Response(200 if graph_ok and database_ok else 503, json=body))


async def test_readyz_passes_the_database_and_graph_checks_of_the_api_and_no_others(valid_env):
    async with serving(build_app(mcp_env(valid_env), transport=api_ready())) as client:
        ready = await client.get("/readyz")

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "checks": {
            "database": {"ok": True, "detail": "ok", "duration_ms": 3},
            "graph": {"ok": True, "detail": "ok", "duration_ms": 3},
        },
    }


async def test_readyz_is_503_when_an_api_check_fails_and_healthz_stays_200(valid_env):
    app = build_app(mcp_env(valid_env), transport=api_ready(graph_ok=False))
    async with serving(app) as client:
        ready = await client.get("/readyz")
        health = await client.get("/healthz")

    body = ready.json()
    assert (ready.status_code, body["status"]) == (503, "not_ready")
    assert (body["checks"]["graph"], body["checks"]["database"]["ok"]) == (
        {"ok": False, "detail": "neo4j_unreachable", "duration_ms": 3},
        True,
    )
    assert health.status_code == 200


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
async def test_readyz_is_503_with_both_checks_not_ok_when_the_api_is_unreachable(valid_env, failure):
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise failure

    async with serving(build_app(mcp_env(valid_env), transport=httpx.MockTransport(refuse))) as client:
        ready = await client.get("/readyz")
        health = await client.get("/healthz")

    unreachable = {"ok": False, "detail": "api_unreachable", "duration_ms": 0}
    assert (ready.status_code, ready.json()["checks"]) == (503, {"database": unreachable, "graph": unreachable})
    assert health.status_code == 200


DATABASE_CHECK = READY_BODY["checks"]["database"]
NOT_A_PROBE_ANSWER = [
    pytest.param(500, {"json": READY_BODY}, id="500-with-a-probe-shaped-body"),
    pytest.param(200, {"json": {"status": "ready", "checks": {"database": DATABASE_CHECK}}}, id="no-graph-check"),
    pytest.param(200, {"text": "<html>not json</html>"}, id="200-not-json"),
]


@pytest.mark.parametrize(("status", "content"), NOT_A_PROBE_ANSWER)
async def test_readyz_is_503_with_both_checks_not_ok_when_the_api_answer_is_not_a_probe_answer(
    valid_env, status, content
):
    def api(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, **content)

    async with serving(build_app(mcp_env(valid_env), transport=httpx.MockTransport(api))) as client:
        ready = await client.get("/readyz")

    unreachable = {"ok": False, "detail": "api_unreachable", "duration_ms": 0}
    assert (ready.status_code, ready.json()["checks"]) == (503, {"database": unreachable, "graph": unreachable})


async def test_readyz_sends_no_token(valid_env):
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=READY_BODY)

    async with serving(build_app(mcp_env(valid_env), transport=httpx.MockTransport(api))) as client:
        await client.get("/readyz", headers={"Authorization": f"Bearer {mcp_token()}"})

    assert [(r.url.path, "authorization" in r.headers) for r in seen] == [("/readyz", False)]


# --- the SDK over HTTP and the tool seam -----------------------------------------


async def test_initialize_answers_the_server_info_and_the_tools_capability(oidc_app, oidc_rig):
    async with serving(oidc_app) as client:
        response = await rpc(client, oidc_rig.token())

    result = response.json()["result"]
    assert result["serverInfo"]["name"] == "SpecFlow"
    assert result["capabilities"]["tools"] is not None


async def test_the_sdk_client_lists_no_tools_over_http(oidc_app, oidc_rig):
    async with sdk_session(oidc_app, oidc_rig.token()) as session:
        tools = await session.list_tools()

    assert tools.tools == []


async def test_the_tools_hook_registers_tools_the_sdk_lists_and_calls(valid_env):
    app = build_app(mcp_env(valid_env), tools=whoami_tools)
    async with sdk_session(app, mcp_token("bob@example.com")) as session:
        listed = await session.list_tools()
        called = await session.call_tool("whoami", {})

    assert [t.name for t in listed.tools] == ["whoami"]
    assert called.content[0].text == "user:bob@example.com"


# --- D2: the explicit transport settings -------------------------------------------


async def test_the_server_is_built_with_explicit_rebinding_protection_and_no_sdk_auth(valid_env, monkeypatch):
    """D2: stateless JSON, 1 MiB cap, rebinding protection ON, derived allowlists; AA3: no SDK auth."""

    built, exposed = {}, {}
    init, expose = MCPServer.__init__, MCPServer.streamable_http_app

    def spy_init(self, *args, **kwargs):
        built.update(kwargs)
        init(self, *args, **kwargs)

    def spy_expose(self, **kwargs):
        exposed.update(kwargs)
        return expose(self, **kwargs)

    monkeypatch.setattr(MCPServer, "__init__", spy_init)
    monkeypatch.setattr(MCPServer, "streamable_http_app", spy_expose)
    env = mcp_env(valid_env, MCP_ALLOWED_HOSTS="alt.example", MCP_ALLOWED_ORIGINS="https://o.example")
    build_app(env)

    security = exposed["transport_security"]
    assert (exposed["json_response"], exposed["stateless_http"], exposed["max_request_body_size"]) == (
        True,
        True,
        ONE_MIB,
    )
    assert security.enable_dns_rebinding_protection is True
    assert (
        security.allowed_hosts
        == list(load_settings("mcp", env).mcp.transport_hosts)
        == ["localhost:8002", "alt.example"]
    )
    assert security.allowed_origins == ["https://o.example"]
    assert "auth" not in built
    assert "token_verifier" not in built


# --- lifespan events (architecture.md, Logging) -------------------------------------


class ClosingTransport(httpx.MockTransport):
    """A fake API that notes when the client closes it (the lifespan owns the client)."""

    def __init__(self) -> None:
        super().__init__(ready_api)
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_startup_and_shutdown_events_and_the_api_client_is_closed(valid_env, mcp_log):
    transport = ClosingTransport()

    async with serving(build_app(mcp_env(valid_env), transport=transport)):
        assert event_names(mcp_log).count("mcp.startup.completed") == 1
        assert not transport.closed

    assert transport.closed
    names = event_names(mcp_log)
    assert names.index("mcp.startup.completed") < names.index("mcp.shutdown.completed")
    started = next(r for r in mcp_log.records if getattr(r, "event", "") == "mcp.startup.completed")
    assert started.port == 8002
    assert not hasattr(started, "auth_mode")  # one auth path: nothing to name


async def test_a_client_that_cannot_be_built_logs_startup_failed(valid_env, mcp_log, monkeypatch):
    def broken(*_args, **_kwargs):
        raise ConnectionError("no api")

    monkeypatch.setattr("mcp_server.app.build_api_client", broken)
    with pytest.raises(ConnectionError):
        async with serving(build_app(mcp_env(valid_env))):
            pass

    failed = [r for r in mcp_log.records if getattr(r, "event", "") == "mcp.startup.failed"]
    assert [r.error_type for r in failed] == ["ConnectionError"]
    assert "mcp.startup.completed" not in event_names(mcp_log)


# --- NFR-MCP-03 (runtime side): the REST app and the MCP role stay apart -----------------


def modules_loaded_by(statement: str) -> set[str]:
    code = f"import sys; sys.path.insert(0, 'src'); {statement}; print(' '.join(sorted(sys.modules)))"
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120, check=True
    )
    return set(done.stdout.split())


def test_the_mcp_app_loads_no_fastapi_and_the_rest_app_loads_no_mcp_role():
    mcp_role = modules_loaded_by("import mcp_server.app, mcp_server.server")
    rest = modules_loaded_by("import api.app")

    assert not {m for m in mcp_role if m == "fastapi" or m.startswith(("fastapi.", "auth.dependencies", "api.app"))}
    assert not {m for m in rest if m == "mcp_server" or m.startswith("mcp_server.")}


def test_both_import_contracts_cover_the_whole_rest_package():
    """R8: mcp-role-isolated and rest-without-mcp name the whole api package, so every REST module is in them."""

    contracts = {
        c["id"]: c
        for c in tomllib.loads((BACKEND / "importlinter.toml").read_text())["tool"]["importlinter"]["contracts"]
    }

    assert contracts["rest-without-mcp"]["source_modules"] == ["api"]
    assert "api" in contracts["mcp-role-isolated"]["forbidden_modules"]
    assert not (BACKEND / "src" / "api" / "mcp").exists()
