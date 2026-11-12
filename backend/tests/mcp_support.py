"""Shared fixtures of the MCP role tests (docs/mcp-server/testing.md): fake-runtime app, tokens, SDK session.

Boundaries faked: the JWKS endpoint (``specflow_auth_support.StubIssuer``, loopback) and the API the MCP role calls
(``build_app(..., transport=...)``: an ``httpx.MockTransport`` or the real API app in-process). Nothing here opens a
socket to a real service. ``build_app(..., tools=...)`` takes the tool installer, ``MethodRouter`` splits one client
over two apps (FR-MCP-OPS-03).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette

from mcp_server.app import create_mcp_app, install_no_tools
from mcp_server.auth import request_principal
from settings import load_settings
from specflow_api_support import token
from specflow_auth_support import StubIssuer, claims, jwk, rsa_key, sign
from test_specflow_db_units import EXTREME_CURSOR

BACKEND = Path(__file__).resolve().parent.parent
MCP_HOST = "localhost:8002"
MCP_URL = f"http://{MCP_HOST}/mcp"
PRM_URL = f"http://{MCP_HOST}/.well-known/oauth-protected-resource/mcp"
API_URL = "http://api.test:8000"  # API_INTERNAL_URL of the tests: a name no one resolves
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
    """A valid local environment for role ``mcp``: the loopback IdP, ``MCP_RESOURCE_URL``, ``API_INTERNAL_URL``."""

    return {**valid_env, "MCP_RESOURCE_URL": MCP_URL, "API_INTERNAL_URL": API_URL, **over}


def oidc_env(valid_env: dict[str, str], stub: StubIssuer, **over: str) -> dict[str, str]:
    """``mcp_env`` against a test's own loopback JWKS stub (a key set the test controls)."""

    return mcp_env(valid_env, OIDC_ISSUER=stub.issuer, AUTH_JWKS_URL=stub.jwks_url, **over)


READY_BODY = {
    "status": "ready",
    "checks": {
        "database": {"ok": True, "detail": "ok", "duration_ms": 1},
        "graph": {"ok": True, "detail": "ok", "duration_ms": 1},
    },
}


def ready_api(request: httpx.Request) -> httpx.Response:
    """A fake API that is ready and serves no agent route (the transport and guard tests make no tool call)."""

    if request.url.path == "/readyz":
        return httpx.Response(200, json=READY_BODY)
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "x", "details": {}}})


def whoami_tools(server: MCPServer) -> None:
    """The ``tools`` hook of the tests: one tool that returns the caller the guard verified."""

    async def whoami(ctx: Context) -> str:
        return request_principal(ctx.request_context.request).subject

    server.add_tool(whoami, name="whoami", description="the verified subject")


def build_app(
    env: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    tools: Callable[[MCPServer], None] = install_no_tools,
) -> Starlette:
    """The MCP app whose API client talks to ``transport`` (default: ``ready_api``, no agent route)."""

    return create_mcp_app(load_settings("mcp", env), transport=transport or httpx.MockTransport(ready_api), tools=tools)


@asynccontextmanager
async def serving(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    """The app's lifespan (session manager, API client) around an in-process client."""

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://{MCP_HOST}") as client,
    ):
        yield client


@asynccontextmanager
async def connected(
    transport: httpx2.AsyncBaseTransport, token: str, apps: Sequence[Starlette]
) -> AsyncIterator[ClientSession]:
    """An initialized SDK client session over ``transport``, the lifespan of every app in ``apps`` around it."""

    http = httpx2.AsyncClient(
        transport=transport,
        base_url=f"http://{MCP_HOST}",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with AsyncExitStack() as stack:
        for app in apps:
            await stack.enter_async_context(app.router.lifespan_context(app))
        await stack.enter_async_context(http)
        streams = await stack.enter_async_context(streamable_http_client(MCP_URL, http_client=http))
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        yield session


@asynccontextmanager
async def sdk_session(app: Starlette, token: str) -> AsyncIterator[ClientSession]:
    """An initialized MCP SDK client session over the app, lifespan included (the SDK client uses ``httpx2``)."""

    async with connected(httpx2.ASGITransport(app=app), token, [app]) as session:
        yield session


class MethodRouter(httpx2.AsyncBaseTransport):
    """One client, two stateless apps: ``initialize`` goes to ``first``, every other request to ``second``."""

    def __init__(self, first: Starlette, second: Starlette) -> None:
        self.first = httpx2.ASGITransport(app=first)
        self.second = httpx2.ASGITransport(app=second)
        self.routed: list[tuple[str, str]] = []  # (JSON-RPC method or HTTP verb, "first" or "second")

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        method = json.loads(body).get("method", "") if body else request.method
        initial = method == "initialize"
        self.routed.append((method, "first" if initial else "second"))
        return await (self.first if initial else self.second).handle_async_request(request)


async def call(session: ClientSession, tool: str, **arguments: Any) -> CallToolResult:
    return await session.call_tool(tool, arguments)


async def rpc(
    client: httpx.AsyncClient,
    token: str | None = None,
    body: dict[str, Any] = INITIALIZE,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """``POST /mcp`` with a JSON-RPC body; ``token`` becomes the Bearer header."""

    sent = {**JSON_HEADERS, **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})}
    return await client.post("/mcp", json=body, headers=sent)


def mcp_token(email: str = "alice@example.com", **over: Any) -> str:
    """A token of the run's loopback IdP for the MCP surface: the API token with the MCP audience and client."""

    return token(email, **{"aud": MCP_URL, "azp": MCP_CLIENT, **over})


def suite_token(email: str) -> str:
    """An MCP token for a module-level constant: minted at import, so it must outlive the whole test run (a CI run
    is longer than the 300 s of `mcp_token`)."""

    return mcp_token(email, exp=int(time.time()) + SUITE_TOKEN_SECONDS)


SUITE_TOKEN_SECONDS = 6 * 3600
ALICE_EMAIL = "alice@example.com"
ALICE = suite_token(ALICE_EMAIL)


def completed(caplog: pytest.LogCaptureFixture) -> list[Any]:
    """The ``mcp.tool.completed`` audit records of ``caplog``."""

    return [r for r in caplog.records if getattr(r, "event", "") == "mcp.tool.completed"]


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
def whoami_app(valid_env: dict[str, str]) -> Starlette:
    """The app on the run's IdP with the ``whoami`` tool (tokens: ``mcp_token``)."""

    return build_app(mcp_env(valid_env), tools=whoami_tools)


def log_text(caplog: pytest.LogCaptureFixture, logger_prefix: str = "") -> str:
    """Every captured record of the loggers under ``logger_prefix`` as text: message and all record attributes
    (a leak shows up anywhere); the SDK client's own debug lines carry the caller's input, so tests narrow to ours."""

    records = [r for r in caplog.records if r.name.startswith(logger_prefix)]
    if logger_prefix in {"mcp_server", "mcp"}:  # an "absent from the logs" check must not pass on empty logs
        assert records, f"no log record under {logger_prefix!r}: the logger names no longer match the filter"
    return "\n".join(f"{r.getMessage()} {sorted(vars(r).items(), key=str)}" for r in records)


@pytest.fixture
def mcp_log(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.DEBUG)
    return caplog


# --- reading tool results (shared by the HTTP and live tiers) ---


def structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return result.structured_content


def text(result: CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, TextContent))


def code(result: CallToolResult) -> str:
    assert result.is_error
    return str(structured(result)["error"]["code"])


def cursor_of(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8", "surrogatepass")).decode().rstrip("=")


def feature_ids(result: CallToolResult) -> list[str]:
    return [item["spec_id"] for item in structured(result)["items"]]


EXTREME_PROJECT_CURSOR = EXTREME_CURSOR  # the one builder lives with the repository tests (no UTC instant, F1)


def assert_no_error_records(caplog: pytest.LogCaptureFixture) -> None:
    """No record of any logger (ours, the SDK server's, starlette's) is ERROR or carries ``exc_info``: a tool failure is
    a mapped result, never a logged traceback (the SDK server logs an exception that escapes a tool)."""

    bad = [(r.name, r.levelname, r.getMessage()) for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]
    assert bad == []
