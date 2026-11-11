"""The MCP role over the REAL API app, in process (plan 06 Do 8; docs/mcp-server/testing.md).

The MCP app's API client talks through ``httpx.ASGITransport`` to the API app of the agent tests (real verifiers, real
agent routes, over ``FakeRepo``, a scripted graph and ``FakePlanService``). A recording transport sits between the two,
so a test can assert on the request the API saw and the answer it gave. Covers: the six tools against their API route,
the error paths end to end, request-id equality, statelessness (FR-MCP-OPS-03), the audit acceptance (FR-MCP-OPS-07)
and the log-hygiene rules (S2) with ``httpx``/``httpcore`` at DEBUG, in process and over a real loopback socket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import uvicorn

from agent_contracts import AGENT_ROUTES, USER_ID_HEADER
from logger.logger import DEBUG, get_logger
from mcp_server.app import create_mcp_app
from mcp_server.errors import MESSAGES
from mcp_server.registry import install
from mcp_support import (
    ALICE,
    ALICE_EMAIL,
    MCP_URL,
    MethodRouter,
    build_app,
    call,
    completed,
    connected,
    mcp_env,
    rpc,
    sdk_session,
    serving,
    structured,
    text,
)
from settings import load_settings
from specflow_api_agent_support import (
    ALPHA,
    BETA,
    CALLS,
    FOREIGN,
    UNKNOWN_PROJECT,
    AgentRig,
    agent_env,
    refuse_database,
    serve_agent,
)

TOOL_NAMES = tuple(CALLS)
FEATURE_TEXT = "An order is stored."  # a done-when line of the seeded plan: feature text the audit must never hold


class Recorder(httpx.AsyncBaseTransport):
    """The API as the MCP client sees it, remembering every exchange (request and answer headers)."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.exchanges: list[tuple[httpx.Request, httpx.Response]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        self.exchanges.append((request, response))
        return response

    @property
    def agent_calls(self) -> list[tuple[httpx.Request, httpx.Response]]:
        return [(q, a) for q, a in self.exchanges if q.url.path in AGENT_ROUTES.values()]


@dataclass
class Stack:
    """The real API (``rig``), the transport the MCP client uses to reach it, and the env of an MCP app over it."""

    rig: AgentRig
    api: Recorder
    env: dict[str, str]
    apps: list[Any] = field(default_factory=list)

    def app(self) -> Any:
        """A new MCP app instance (own settings, client and session manager) over this API."""

        return build_app(self.env, transport=self.api, tools=lambda server: install(server, resource_url=MCP_URL))


@asynccontextmanager
async def over_api(
    monkeypatch: pytest.MonkeyPatch, valid_env: dict[str, str], *, scenario: str = "ok", has_plan: bool = True
) -> AsyncIterator[Stack]:
    async with serve_agent(monkeypatch, agent_env(valid_env), scenario=scenario, has_plan=has_plan) as rig:
        if scenario == "database_down":  # after the rig installed its FakeRepo, which it would replace
            refuse_database(monkeypatch)
        recorder = Recorder(httpx.ASGITransport(app=rig.app, raise_app_exceptions=False))
        yield Stack(rig, recorder, mcp_env(valid_env))


# --- 1. the six tools return what the API route returns -------------------------------------------------------


@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_a_tool_call_returns_the_structured_content_the_api_route_returns_for_the_same_data(
    monkeypatch, valid_env, tool
):
    async with over_api(monkeypatch, valid_env) as stack:
        _path, params = CALLS[tool]
        direct = await stack.rig.call(tool)
        async with sdk_session(stack.app(), ALICE) as session:
            result = await call(session, tool, **params)

    assert direct.status_code == 200, direct.text
    assert not result.is_error
    assert structured(result) == direct.json()
    assert json.loads(text(result)) == direct.json()
    (request, answer), *_ = stack.api.agent_calls
    assert (request.method, request.url.path) == ("GET", AGENT_ROUTES[tool]) and answer.status_code == 200


# --- 1. the error paths end to end ----------------------------------------------------------------------------

ERROR_CASES = [
    pytest.param("ok", "list_features", {"project_id": FOREIGN}, "project_not_found", id="foreign-project"),
    pytest.param("ok", "list_tasks", {"project_id": UNKNOWN_PROJECT}, "project_not_found", id="unknown-project"),
    pytest.param("ok", "list_features", {"project_id": "not-a-uuid LEAK"}, "project_not_found", id="malformed-project"),
    pytest.param("ok", "list_features", {"cursor": "x"}, "invalid_cursor", id="bad-cursor-features"),
    pytest.param("ok", "list_projects", {"cursor": "x"}, "invalid_cursor", id="bad-cursor-projects"),
    pytest.param("no_plan", "list_tasks", {}, "no_plan", id="no-plan-list-tasks"),
    pytest.param("no_plan", "get_task_brief", {}, "no_plan", id="no-plan-brief"),
    pytest.param("ok", "get_task_brief", {"task_id": "TASK::NOPE"}, "task_not_found", id="unknown-task-brief"),
    pytest.param("ok", "get_task_path", {"task_id": "TASK::NOPE"}, "task_not_found", id="unknown-task-path"),
    pytest.param("ok", "list_feature_tasks", {"feature_id": "FEAT::NOPE"}, "feature_not_found", id="unknown-feature"),
    pytest.param("graph_down", "list_features", {}, "graph_unavailable", id="graph-outage"),
    pytest.param("database_down", "list_projects", {}, "database_unavailable", id="postgres-outage"),
    pytest.param("bug", "list_features", {}, "internal_error", id="api-bug"),
]


@pytest.mark.parametrize(("scenario", "tool", "over", "code"), ERROR_CASES)
async def test_an_api_error_is_the_fixed_mcp_message_for_its_code_and_none_of_the_api_text(
    monkeypatch, valid_env, *, scenario, tool, over, code
):
    async with over_api(monkeypatch, valid_env, scenario=scenario, has_plan=scenario != "no_plan") as stack:
        _path, params = CALLS[tool]
        async with sdk_session(stack.app(), ALICE) as session:
            result = await call(session, tool, **{**params, **over})
        _request, answer = stack.api.agent_calls[-1]

    assert result.is_error
    assert structured(result) == {"error": {"code": code, "message": MESSAGES[code]}}  # no details, no extra key
    assert text(result) == json.dumps(structured(result), separators=(",", ":"))
    assert "LEAK" not in text(result) and "details" not in text(result)  # the message is exactly MESSAGES[code]
    assert answer.status_code >= 400


# --- 2. request-id equality ---------------------------------------------------------------------------------------


async def test_the_request_id_the_api_adopted_is_the_one_of_the_audit_event(monkeypatch, valid_env, central_log):
    central_log.configure()
    async with over_api(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        await call(session, "list_projects")
        await call(session, "list_features", project_id=ALPHA)
        await call(session, "list_features", project_id=FOREIGN)  # an error answer echoes it too

    events = central_log.events("mcp.tool.completed")
    exchanges = stack.api.agent_calls
    assert len(events) == len(exchanges) == 3
    for event, (request, answer) in zip(events, exchanges, strict=True):
        assert request.headers["x-request-id"] == answer.headers["x-request-id"] == event["request_id"]
        assert len(event["request_id"]) == 32
    assert len({e["request_id"] for e in events}) == 3


async def test_a_caller_sent_request_id_is_never_adopted_by_mcp_nor_forwarded(monkeypatch, valid_env, central_log):
    central_log.configure()
    sent = "caller-chosen-0123456789"
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}}
    async with over_api(monkeypatch, valid_env) as stack, serving(stack.app()) as client:
        reply = await rpc(client, ALICE, body, headers={"X-Request-ID": sent})

    assert reply.status_code == 200, reply.text
    (event,) = central_log.events("mcp.tool.completed")
    ((request, answer),) = stack.api.agent_calls
    assert event["request_id"] != sent
    assert request.headers["x-request-id"] == answer.headers["x-request-id"] == event["request_id"]
    assert reply.headers.get("x-request-id") in (None, event["request_id"])  # never the caller's own value


# --- 3. FR-MCP-OPS-03: stateless across instances -------------------------------------------------------------


async def test_initialize_on_one_mcp_instance_and_tools_call_on_another_succeeds(monkeypatch, valid_env):
    async with over_api(monkeypatch, valid_env) as stack:
        first, second = stack.app(), stack.app()
        assert first is not second
        router = MethodRouter(first, second)
        async with connected(router, ALICE, [first, second]) as session:
            result = await call(session, "list_projects", limit=2)
        direct = await stack.rig.call("list_projects")

    assert not result.is_error and structured(result) == direct.json()
    assert ("initialize", "first") in router.routed and ("tools/call", "second") in router.routed
    assert all(where == "second" for method, where in router.routed if method == "tools/call")


# --- 4. FR-MCP-OPS-07 acceptance on the MCP side --------------------------------------------------------------------


async def test_several_calls_write_several_audit_events_with_the_documented_fields_and_no_feature_text(
    monkeypatch, valid_env, central_log
):
    central_log.configure()
    async with over_api(monkeypatch, valid_env) as stack:
        user_id = await stack.rig.alice_user_id_header()
        async with sdk_session(stack.app(), ALICE) as session:
            for tool in TOOL_NAMES:
                await call(session, tool, **CALLS[tool][1])
            await call(session, "list_features", project_id=FOREIGN)

    events = central_log.events("mcp.tool.completed")
    assert [e["tool"] for e in events] == [*TOOL_NAMES, "list_features"]
    for event in events:
        assert {"tool", "outcome", "item_count", "duration_ms", "request_id", "user_id"} <= event.keys()
        assert event["user_id"] == user_id
    assert [e.get("project_id") for e in events] == [None, *[ALPHA] * 5, FOREIGN]  # list_projects has none
    assert events[-1]["outcome"] == "project_not_found"
    dump = json.dumps(events, default=str)
    assert not [s for s in (FEATURE_TEXT, "FEAT::", "TASK::", "Alpha", ALICE, ALICE_EMAIL) if s in dump]


async def test_a_caller_sent_user_id_header_is_not_forwarded_and_does_not_change_the_audit_user(
    monkeypatch, valid_env, central_log
):
    central_log.configure()
    forged = "99999999-9999-4999-8999-999999999999"
    body = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_projects", "arguments": {}}}
    async with over_api(monkeypatch, valid_env) as stack, serving(stack.app()) as client:
        real = await stack.rig.alice_user_id_header()
        reply = await rpc(client, ALICE, body, headers={USER_ID_HEADER: forged, **CALLER_HEADERS})

    assert reply.status_code == 200, reply.text
    request, answer = stack.api.agent_calls[-1]
    assert USER_ID_HEADER.lower() not in request.headers  # the API-side request carries none
    assert {name.lower() for name in request.headers} <= API_REQUEST_HEADERS  # S1: an allowlist, no caller header
    assert request.headers.get_list("authorization") == [f"Bearer {ALICE}"]  # the one header of the caller
    (event,) = central_log.events("mcp.tool.completed")
    assert event["user_id"] == answer.headers[USER_ID_HEADER] == real != forged


CALLER_HEADERS = {  # what a caller may send next to its token: none of it may reach the API (S1)
    "Cookie": "session=LEAKCOOKIE",
    "Forwarded": "for=10.9.9.9",
    "X-Forwarded-For": "10.9.9.9",
    "X-Api-Key": "LEAKKEY",
    "Proxy-Authorization": "Basic TEVBSw==",
}
API_REQUEST_HEADERS = {  # every header name the MCP client itself puts on an API request
    "host",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
    "authorization",
    "x-request-id",
}


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_projects", {"limit": 0}),
        ("list_features", {"project_id": ALPHA, "limit": 101}),
        ("list_tasks", {"project_id": ALPHA, "status": "bogus"}),
        ("get_task_brief", {"project_id": ALPHA}),  # task_id missing
    ],
)
async def test_an_input_schema_refusal_makes_no_api_call_and_writes_no_audit_event(
    monkeypatch, valid_env, mcp_log, tool, arguments
):
    async with over_api(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        result = await call(session, tool, **arguments)

    assert result.is_error
    assert stack.api.agent_calls == []
    assert completed(mcp_log) == []


async def test_the_audit_user_comes_from_the_api_header_and_project_id_only_for_a_uuid(monkeypatch, valid_env, mcp_log):
    async with over_api(monkeypatch, valid_env) as stack:
        async with sdk_session(stack.app(), ALICE) as session:
            await call(session, "list_features", project_id=BETA.upper())
            await call(session, "list_features", project_id="not-a-uuid LEAK")
        (_, first), (_, second) = stack.api.agent_calls

    good, bad = completed(mcp_log)
    assert good.user_id == bad.user_id == first.headers[USER_ID_HEADER] == second.headers[USER_ID_HEADER]
    assert good.project_id == BETA and not hasattr(bad, "project_id")
    assert "LEAK" not in str([vars(r) for r in mcp_log.records if r.name.startswith("mcp_server")])


# --- 6. S2 over the real-API path at DEBUG ----------------------------------------------------------------------


def secrets_in(caplog: pytest.LogCaptureFixture, secrets: tuple[str, ...]) -> list[str]:
    """The secrets found in any record (message and every attribute) except the test's own MCP client logger."""

    records = [r for r in caplog.records if not r.name.startswith("mcp.client")]
    haystack = "\n".join(f"{r.name} {r.getMessage()} {sorted(vars(r).items(), key=str)}" for r in records)
    return [s for s in secrets if s in haystack]


def debug_levels(caplog: pytest.LogCaptureFixture) -> None:
    """DEBUG for the loggers of the call path; ``caplog`` restores every level when the test ends."""

    for name in ("httpx", "httpcore", "mcp_server", "api"):
        get_logger(name)  # bootstrap the pipeline first: it must not reset the level set below
        caplog.set_level(DEBUG, logger=name)


async def test_nothing_sensitive_reaches_any_log_record_at_debug_over_the_real_api(monkeypatch, valid_env, mcp_log):
    debug_levels(mcp_log)
    malformed, cursor = "not-a-uuid LEAKPID", "CURSORLEAK"
    secrets = (ALICE, f"Bearer {ALICE}", ALICE_EMAIL, FEATURE_TEXT, malformed, "LEAKPID", cursor)
    async with over_api(monkeypatch, valid_env) as stack, sdk_session(stack.app(), ALICE) as session:
        await call(session, "get_task_brief", **CALLS["get_task_brief"][1])  # carries feature text
        await call(session, "list_features", project_id=malformed)
        await call(session, "list_features", project_id=ALPHA, cursor=cursor)
        await call(session, "list_projects")

    names = {r.name for r in mcp_log.records}
    assert any(n.startswith("httpx") for n in names), "positive control: the httpx request lines are captured"
    assert len(completed(mcp_log)) == 4, "positive control: the audit events are captured"
    assert any("HTTP Request: GET" in r.getMessage() for r in mcp_log.records)
    assert secrets_in(mcp_log, secrets) == []

    get_logger("mcp_server.control").log(logging.INFO, "control %s", f"Bearer {ALICE} {malformed}")
    assert secrets_in(mcp_log, secrets) == [ALICE, f"Bearer {ALICE}", malformed, "LEAKPID"]  # the detector detects


@asynccontextmanager
async def listening(app: Any) -> AsyncIterator[str]:
    """``app`` under uvicorn on a free 127.0.0.1 port (same event loop); yields its base URL. Started when uvicorn's
    ``started`` flag is set, stopped through ``should_exit``: no fixed wait either way."""

    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_config=None, access_log=False)
    server = uvicorn.Server(config)
    serving_task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                assert not serving_task.done(), "uvicorn stopped before it started"
                await asyncio.sleep(0)  # yield to the loop; the bound is the timeout
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving_task


async def test_nothing_sensitive_reaches_any_log_record_at_debug_over_a_real_loopback_socket(
    monkeypatch, valid_env, mcp_log
):
    """The client of the other S2 tests is an in-process transport; here the default one runs: ``httpcore`` really
    connects, sends and receives, and its DEBUG trace is part of the records."""

    debug_levels(mcp_log)
    malformed, cursor = "not-a-uuid LEAKPID", "CURSORLEAK"
    secrets = (ALICE, f"Bearer {ALICE}", ALICE_EMAIL, FEATURE_TEXT, malformed, "LEAKPID", cursor)
    async with serve_agent(monkeypatch, agent_env(valid_env)) as rig, listening(rig.app) as api_url:
        settings = load_settings("mcp", mcp_env(valid_env, API_INTERNAL_URL=api_url))
        app = create_mcp_app(settings, tools=lambda server: install(server, resource_url=MCP_URL))  # default transport
        async with sdk_session(app, ALICE) as session:
            brief = await call(session, "get_task_brief", **CALLS["get_task_brief"][1])  # carries feature text
            await call(session, "list_features", project_id=malformed)
            await call(session, "list_features", project_id=ALPHA, cursor=cursor)
            await call(session, "list_projects")

    assert not brief.is_error
    assert any(r.name.startswith("httpcore") for r in mcp_log.records), "positive control: httpcore traced the socket"
    assert any("connect_tcp" in r.getMessage() for r in mcp_log.records), "positive control: a real connection"
    assert any("HTTP Request: GET" in r.getMessage() and api_url in r.getMessage() for r in mcp_log.records)
    assert len(completed(mcp_log)) == 4, "positive control: the audit events are captured"
    assert secrets_in(mcp_log, secrets) == []

    get_logger("mcp_server.control").log(logging.INFO, "control %s", f"Bearer {ALICE} {malformed}")
    assert secrets_in(mcp_log, secrets) == [ALICE, f"Bearer {ALICE}", malformed, "LEAKPID"]  # the detector detects
