"""Shared fixtures of the MCP role tests (docs/mcp-server/testing.md): fake-runtime app, tokens, SDK session.

Boundaries faked: the JWKS endpoint (``specflow_auth_support.StubIssuer``, loopback) and the runtime probes.
Nothing here opens a socket to a real service. MCP-M5 extends it: ``build_app(..., tools=...)`` takes the tool
installer and ``fake_runtime`` the reader and the graph executor.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import Context, MCPServer
from starlette.applications import Starlette

from api.mcp.app import create_mcp_app, install_no_tools
from api.mcp.auth import request_principal
from api.mcp.runtime import McpRuntime, RuntimeFactory
from health import Readiness
from settings import load_settings
from specflow_api_support import token as dev_token
from specflow_auth_support import StubIssuer, claims, jwk, rsa_key, sign

BACKEND = Path(__file__).resolve().parent.parent
MCP_HOST = "localhost:8002"
MCP_URL = f"http://{MCP_HOST}/mcp"
PRM_URL = f"http://{MCP_HOST}/.well-known/oauth-protected-resource/mcp"
MCP_CLIENT = "specflow-mcp"
JSON_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
ONE_MIB = 1024 * 1024


def mcp_env(valid_env: dict[str, str], **over: str) -> dict[str, str]:
    """A valid local environment for role ``mcp`` (dev mode, ``MCP_RESOURCE_URL`` set)."""

    return {**valid_env, "MCP_RESOURCE_URL": MCP_URL, **over}


def oidc_env(valid_env: dict[str, str], stub: StubIssuer, **over: str) -> dict[str, str]:
    """``mcp_env`` in oidc mode against the loopback JWKS stub (no dev key)."""

    env = mcp_env(valid_env, AUTH_MODE="oidc", OIDC_ISSUER=stub.issuer, AUTH_JWKS_URL=stub.jwks_url, **over)
    env.pop("AUTH_DEV_SIGNING_KEY")
    return env


def fake_runtime(
    *,
    neo4j_ok: bool = True,
    reader: Any = None,
    session_factory: Any = None,
    ontology: Any = None,
) -> McpRuntime:
    """A runtime with fake probes: no engine, no driver, no I/O; MCP-M5 passes its reader, sessions, ontology."""

    async def ok() -> None:
        return None

    async def down() -> None:
        raise ConnectionError("down")

    async def close() -> None:
        return None

    unused = cast(Any, None)
    probes = {"database": ok, "neo4j": ok if neo4j_ok else down}
    return McpRuntime(
        engine=unused,
        session_factory=session_factory or unused,
        driver=unused,
        ontology=ontology or unused,
        reader=reader or unused,
        readiness=Readiness(probes),
        close=close,
    )


def runtime_factory(runtime: McpRuntime | None = None) -> RuntimeFactory:
    async def build(_: Any) -> McpRuntime:
        return runtime or fake_runtime()

    return build


def whoami_tools(server: MCPServer) -> None:
    """The ``tools`` hook of the tests: one tool that returns the caller the guard verified."""

    async def whoami(ctx: Context) -> str:
        return request_principal(ctx.request_context.request).subject

    server.add_tool(whoami, name="whoami", description="the verified subject")


def build_app(
    env: dict[str, str],
    *,
    runtime: McpRuntime | None = None,
    factory: RuntimeFactory | None = None,
    tools: Callable[[MCPServer], None] = install_no_tools,
) -> Starlette:
    return create_mcp_app(load_settings("mcp", env), runtime_factory=factory or runtime_factory(runtime), tools=tools)


@asynccontextmanager
async def serving(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    """The app's lifespan (session manager, runtime) around an in-process client."""

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{MCP_HOST}") as client,
    ):
        yield client


@asynccontextmanager
async def sdk_session(app: Starlette, token: str) -> AsyncIterator[ClientSession]:
    """An initialized MCP SDK client session over the app, lifespan included (the SDK client uses ``httpx2``)."""

    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url=f"http://{MCP_HOST}",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with (
        app.router.lifespan_context(app),
        http,
        streamable_http_client(MCP_URL, http_client=http) as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()
        yield session


async def rpc(
    client: httpx.AsyncClient,
    token: str | None = None,
    body: dict[str, Any] = INITIALIZE,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """``POST /mcp`` with a JSON-RPC body; ``token`` becomes the Bearer header."""

    sent = {**JSON_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
    return await client.post("/mcp", json=body, headers=sent)


def mcp_dev_token(email: str = "alice@example.com", **over: Any) -> str:
    """A dev token for the MCP surface: the web dev token with the MCP audience and client."""

    return dev_token(email, **{"aud": MCP_URL, "azp": MCP_CLIENT, **over})


@dataclass
class OidcRig:
    """The loopback IdP of the oidc tests: a signing key published by the stub."""

    stub: StubIssuer
    key: Any

    def token(self, **over: Any) -> str:
        payload = claims(**{"iss": self.stub.issuer, "aud": MCP_URL, "azp": MCP_CLIENT, **over})
        return sign(payload, self.key, kid="rsa")


@pytest.fixture
def oidc_rig(stub_issuer: StubIssuer) -> OidcRig:
    key = rsa_key()
    stub_issuer.keys = [jwk(key, "rsa")]
    return OidcRig(stub_issuer, key)


@pytest.fixture
def oidc_app(valid_env: dict[str, str], oidc_rig: OidcRig) -> Starlette:
    return build_app(oidc_env(valid_env, oidc_rig.stub))


@pytest.fixture
def dev_app(valid_env: dict[str, str]) -> Starlette:
    return build_app(mcp_env(valid_env), tools=whoami_tools)


def log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record as text: message and all record attributes (a leak shows up anywhere)."""

    return "\n".join(f"{r.getMessage()} {sorted(vars(r).items(), key=str)}" for r in caplog.records)


@pytest.fixture
def mcp_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog
