"""The router seam of the MCP role over a fake API (architecture.md, Tool registry and Errors).

What a fake API can show that the real one (``test_mcp_over_api.py``) cannot produce: the request an API call sends
(route, query parameters, headers), an answer the real API never gives (200 without the user header, 302, non-JSON,
a transport failure, a hang) and how the client behaves (no retry, redirect, proxy or cookie, one total timeout).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from agent_contracts import AGENT_ROUTES
from mcp_server.api_client import API_TIMEOUT_SECONDS, READYZ_TIMEOUT_SECONDS, ApiClient
from mcp_server.registry import install
from mcp_support import ALICE, MCP_URL, build_app, call, completed, mcp_env, sdk_session, serving, structured

USER = "11111111-1111-4111-8111-111111111111"
PROJECT = "00000000-0000-4000-8000-000000000001"
PAGE = {"items": [{"project_id": PROJECT, "name": "Alpha", "created_at": "2026-03-03T09:30:00Z"}], "next_cursor": None}


def router(valid_env, handler: Any):
    return build_app(
        mcp_env(valid_env),
        transport=httpx.MockTransport(handler),
        tools=lambda server: install(server, resource_url=MCP_URL),
    )


async def test_a_tool_call_is_one_get_on_its_route_with_the_callers_token_and_the_mcp_request_id(
    valid_env, central_log
):
    central_log.configure()
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PAGE, headers={"X-SpecFlow-User-Id": USER})

    async with sdk_session(router(valid_env, api), ALICE) as session:
        result = await call(session, "list_projects", limit=2)

    (request,) = seen
    assert (request.method, request.url.path, dict(request.url.params)) == (
        "GET",
        AGENT_ROUTES["list_projects"],
        {"limit": "2"},
    )
    assert request.headers["authorization"] == f"Bearer {ALICE}"
    assert not result.is_error and structured(result) == PAGE
    (event,) = central_log.events("mcp.tool.completed")
    assert request.headers["x-request-id"] == event["request_id"] and len(event["request_id"]) == 32
    assert (event["tool"], event["outcome"], event["item_count"], event["user_id"]) == ("list_projects", "ok", 1, USER)
    assert "project_id" not in event and "error_type" not in event


async def test_the_arguments_become_query_parameters_booleans_lower_case_integers_decimal_none_omitted(
    valid_env,
) -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PAGE, headers={"X-SpecFlow-User-Id": USER})

    async with sdk_session(router(valid_env, api), ALICE) as session:
        await call(session, "list_tasks", project_id=PROJECT, ready_only=False, limit=7)
        await call(session, "list_tasks", project_id=PROJECT, status="pending", cursor="c")

    first, second = seen
    assert dict(first.url.params) == {"project_id": PROJECT, "ready_only": "false", "limit": "7"}
    assert dict(second.url.params) == {
        "project_id": PROJECT,
        "ready_only": "true",
        "status": "pending",
        "cursor": "c",
        "limit": "50",
    }


@pytest.mark.parametrize(
    ("handler", "error_type"),
    [
        (lambda _r: (_ for _ in ()).throw(httpx.ConnectError("refused LEAK")), "ApiUnreachable"),
        (lambda _r: (_ for _ in ()).throw(httpx.ReadTimeout("slow LEAK")), "ApiTimeout"),
        (lambda _r: (_ for _ in ()).throw(httpx.RemoteProtocolError("broken LEAK")), "ApiProtocolError"),
        (lambda _r: httpx.Response(200, json=PAGE), "ApiProtocolError"),  # no user header
        (lambda _r: httpx.Response(200, text="<html>LEAK"), "ApiProtocolError"),
        (
            lambda _r: httpx.Response(401, json={"error": {"code": "invalid_token", "message": "LEAK"}}),
            "ApiAuthRejected",
        ),
        (lambda _r: httpx.Response(302, headers={"Location": "http://elsewhere.test/LEAK"}), "ApiProtocolError"),
    ],
)
async def test_a_failed_call_is_internal_error_with_a_fixed_message_and_the_named_failure(
    valid_env, mcp_log, handler, error_type
):
    attempts: list[httpx.Request] = []

    def counted(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return handler(request)

    async with sdk_session(router(valid_env, counted), ALICE) as session:
        result = await call(session, "list_projects")

    assert len(attempts) == 1  # no retry, not even of a connect failure or a timeout
    assert result.is_error and structured(result)["error"]["code"] == "internal_error"
    assert "LEAK" not in json.dumps(structured(result))
    (event,) = completed(mcp_log)
    assert (event.outcome, event.error_type) == ("internal_error", error_type)
    assert not hasattr(event, "user_id")


async def test_a_redirect_is_not_followed_and_the_total_timeout_is_25_seconds() -> None:
    calls: list[str] = []

    def api(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://elsewhere.test/x"})

    client = ApiClient("http://api.test:8000", transport=httpx.MockTransport(api))
    reply = await client.call(AGENT_ROUTES["list_projects"], {}, "Bearer t", "a" * 32)
    await client.aclose()

    assert (reply.status, calls) == (302, ["http://api.test:8000" + AGENT_ROUTES["list_projects"]])  # not followed
    assert API_TIMEOUT_SECONDS == 25  # the value the hanging-API test shortens; its effect is shown there


@asynccontextmanager
async def accepting() -> AsyncIterator[tuple[str, list[int]]]:
    """A loopback listener that counts connections and drops them: the "proxy" of the host environment."""

    accepted: list[int] = []

    async def drop(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        accepted.append(1)
        writer.close()

    server = await asyncio.start_server(drop, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", accepted
    finally:
        server.close()
        await server.wait_closed()


async def test_the_proxy_variables_of_the_host_are_ignored_the_token_goes_to_the_configured_address_only(
    monkeypatch,
) -> None:
    async def api(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        body = b'{"items": [], "next_cursor": null}'
        head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: %d\r\n"
        writer.write(head % len(body) + b"X-SpecFlow-User-Id: " + USER.encode() + b"\r\n\r\n" + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(api, "127.0.0.1", 0)
    api_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    async with server, accepting() as (proxy_url, proxied):
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(name, proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        async with httpx.AsyncClient() as honoring:  # positive control: the same environment does reach the proxy
            with pytest.raises(httpx.TransportError):
                await honoring.get(api_url)
        assert len(proxied) == 1

        client = ApiClient(api_url)
        reply = await client.call(AGENT_ROUTES["list_projects"], {}, "Bearer alice", "a" * 32)
        await client.aclose()

    assert (reply.status, reply.user_id, len(proxied)) == (200, USER, 1)  # answered by the API, no second proxy hit


async def test_no_cookie_of_the_api_side_is_stored_or_replayed_to_another_caller_or_the_probe(valid_env) -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=PAGE, headers={"X-SpecFlow-User-Id": USER, "Set-Cookie": "lb=sticky; Path=/"})

    client = ApiClient("http://api.test:8000", transport=httpx.MockTransport(api))
    route = AGENT_ROUTES["list_projects"]
    await client.call(route, {}, "Bearer alice", "a" * 32)
    await client.call(route, {}, "Bearer bob", "b" * 32)
    await client.readyz()
    await client.aclose()

    assert len(seen) == 3 and all("cookie" not in r.headers for r in seen)


async def test_a_hanging_api_ends_the_call_at_the_total_timeout_as_internal_error_api_timeout(
    valid_env, mcp_log, monkeypatch
) -> None:
    attempts: list[httpx.Request] = []

    async def hang(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        await asyncio.Event().wait()  # never answers
        return httpx.Response(200)

    # the total timeout is the default of ``_get``, bound when the function was defined: patching the module
    # constant would not reach it, so the default itself is replaced (no product change)
    monkeypatch.setattr(ApiClient._get, "__defaults__", (0.2,))
    async with sdk_session(router(valid_env, hang), ALICE) as session:
        started = time.monotonic()
        result = await call(session, "list_projects")
        waited = time.monotonic() - started

    assert waited < API_TIMEOUT_SECONDS / 2 and len(attempts) == 1
    assert result.is_error and structured(result)["error"]["code"] == "internal_error"
    (event,) = completed(mcp_log)
    assert (event.outcome, event.error_type) == ("internal_error", "ApiTimeout")


async def test_a_hanging_api_makes_readyz_503_api_unreachable_within_the_probe_timeout(valid_env) -> None:
    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()  # never answers
        return httpx.Response(200)

    assert READYZ_TIMEOUT_SECONDS == 3 < API_TIMEOUT_SECONDS
    async with serving(build_app(mcp_env(valid_env), transport=httpx.MockTransport(hang))) as client:
        started = time.monotonic()
        reply = await client.get("/readyz")

    assert time.monotonic() - started < READYZ_TIMEOUT_SECONDS + 1
    assert reply.status_code == 503
    assert {c["detail"] for c in reply.json()["checks"].values()} == {"api_unreachable"}
