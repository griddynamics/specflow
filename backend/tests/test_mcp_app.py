"""The MCP app: PRM, probes, lifespan events, SDK session, rebinding settings (FR-MCP-AUTH-02, FR-MCP-OPS-02)."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import tomllib
import types

import pytest
from mcp.server.mcpserver import MCPServer

from api.mcp.app import create_mcp_app
from graph.core import OntologyError
from mcp_support import (
    BACKEND,
    MCP_URL,
    ONE_MIB,
    build_app,
    fake_runtime,
    mcp_dev_token,
    mcp_env,
    oidc_env,
    rpc,
    sdk_session,
    serving,
    whoami_tools,
)
from settings import load_settings

NO_FEATURE_ONTOLOGY = types.SimpleNamespace(node_types={"module": 1})
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


async def test_dev_mode_has_no_prm(dev_app):
    """FR-MCP-AUTH-11: both well-known paths are 404 in dev mode."""

    async with serving(dev_app) as client:
        responses = [await client.get(path) for path in PRM_PATHS]

    assert [r.status_code for r in responses] == [404, 404]
    assert responses[0].json()["error"]["code"] == "not_found"


# --- FR-MCP-OPS-02: probes -----------------------------------------------------


async def test_healthz_and_a_ready_readyz(dev_app):
    async with serving(dev_app) as client:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")

    assert (health.status_code, health.json()) == (200, {"status": "ok"})
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == "ready"
    assert {name: check["ok"] for name, check in body["checks"].items()} == {"database": True, "neo4j": True}


async def test_readyz_is_503_when_neo4j_is_down_and_healthz_stays_200(valid_env):
    app = build_app(mcp_env(valid_env), runtime=fake_runtime(neo4j_ok=False))
    async with serving(app) as client:
        ready = await client.get("/readyz")
        health = await client.get("/healthz")

    body = ready.json()
    assert (ready.status_code, body["status"]) == (503, "not_ready")
    assert (body["checks"]["neo4j"]["ok"], body["checks"]["database"]["ok"]) == (False, True)
    assert "down" not in ready.text  # a fixed detail code, never exception text
    assert health.status_code == 200


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
    async with sdk_session(app, mcp_dev_token("bob@example.com")) as session:
        listed = await session.list_tools()
        called = await session.call_tool("whoami", {})

    assert [t.name for t in listed.tools] == ["whoami"]
    assert called.content[0].text == "dev:bob@example.com"


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


async def test_startup_and_shutdown_events_and_the_runtime_is_closed(valid_env, mcp_log):
    closed = []

    async def close():
        closed.append(True)

    runtime = dataclasses.replace(fake_runtime(), close=close)
    async with serving(build_app(mcp_env(valid_env), runtime=runtime)):
        assert event_names(mcp_log).count("mcp.startup.completed") == 1
        assert closed == []

    assert closed == [True]
    names = event_names(mcp_log)
    assert names.index("mcp.startup.completed") < names.index("mcp.shutdown.completed")
    started = next(r for r in mcp_log.records if getattr(r, "event", "") == "mcp.startup.completed")
    assert (started.port, started.auth_mode) == (8002, "dev")


async def test_a_runtime_factory_that_raises_logs_startup_failed(valid_env, mcp_log):
    async def broken(_):
        raise ConnectionError("no database")

    app = build_app(mcp_env(valid_env), factory=broken)
    with pytest.raises(ConnectionError):
        async with serving(app):
            pass

    failed = [r for r in mcp_log.records if getattr(r, "event", "") == "mcp.startup.failed"]
    assert [r.error_type for r in failed] == ["ConnectionError"]
    assert "mcp.startup.completed" not in event_names(mcp_log)


async def test_an_ontology_without_a_feature_type_refuses_the_start(valid_env, mcp_log, monkeypatch):
    """architecture.md Operations: the ontology must hold a feature type; the opened driver and engine are closed."""

    monkeypatch.setattr("api.mcp.runtime.graph_core.load_ontology", lambda: NO_FEATURE_ONTOLOGY)
    app = create_mcp_app(load_settings("mcp", mcp_env(valid_env)))  # the real build_runtime: lazy engine and driver
    with pytest.raises(OntologyError):
        async with serving(app):
            pass

    failed = [r for r in mcp_log.records if getattr(r, "event", "") == "mcp.startup.failed"]
    assert [r.error_type for r in failed] == ["OntologyError"]


# --- NFR-MCP-03 (runtime side): the REST app and the MCP role stay apart -----------------


def modules_loaded_by(statement: str) -> set[str]:
    code = f"import sys; sys.path.insert(0, 'src'); {statement}; print(' '.join(sorted(sys.modules)))"
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=120, check=True
    )
    return set(done.stdout.split())


def test_the_mcp_app_loads_no_fastapi_and_the_rest_app_loads_no_mcp_role():
    mcp_role = modules_loaded_by("import api.mcp.app, api.mcp.server")
    rest = modules_loaded_by("import api.app")

    assert not {m for m in mcp_role if m == "fastapi" or m.startswith(("fastapi.", "auth.dependencies", "api.app"))}
    assert not {m for m in rest if m == "api.mcp" or m.startswith("api.mcp.")}


def test_both_rest_lists_of_the_import_contracts_name_every_rest_module():
    """R8: a new module or package under src/api must join mcp-role-isolated and rest-without-mcp."""

    api = BACKEND / "src" / "api"
    rest = {
        f"api.{entry.stem if entry.is_file() else entry.name}"
        for entry in api.iterdir()
        if entry.name not in {"__init__.py", "__pycache__", "mcp", "fixtures"}
        and (entry.is_dir() or entry.suffix == ".py")
    }
    contracts = {
        c["id"]: c
        for c in tomllib.loads((BACKEND / "importlinter.toml").read_text())["tool"]["importlinter"]["contracts"]
    }
    isolated = {m for m in contracts["mcp-role-isolated"]["forbidden_modules"] if m.startswith("api.")}
    sources = set(contracts["rest-without-mcp"]["source_modules"])

    assert rest <= isolated
    assert rest == sources
