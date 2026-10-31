"""Transport rules of ``/mcp``: Host, Origin, body size, methods, unknown paths, failures (FR-MCP-OPS-04 to -06)."""

from __future__ import annotations

import dataclasses
from typing import Any, cast

import httpx
import pytest
from mcp.server.mcpserver import MCPServer

from logger.logger import INFO, get_logger, log_event
from mcp_support import (
    JSON_HEADERS,
    MCP_HOST,
    ONE_MIB,
    build_app,
    fake_runtime,
    mcp_dev_token,
    mcp_env,
    rpc,
    serving,
)

ORIGIN = "https://client.example"


@pytest.fixture
def token() -> str:
    return mcp_dev_token()


@pytest.fixture
def transport_app(valid_env):
    return build_app(mcp_env(valid_env, MCP_ALLOWED_ORIGINS=ORIGIN, MCP_ALLOWED_HOSTS="alt.example"))


# --- FR-MCP-OPS-04: Host and Origin ----------------------------------------------


async def test_a_host_outside_the_allowlist_is_421_with_a_valid_token(transport_app, token):
    async with serving(transport_app) as client:
        evil = await rpc(client, token, headers={"Host": "evil.example"})
        wrong_port = await rpc(client, token, headers={"Host": "localhost:9999"})
        listed = await rpc(client, token, headers={"Host": "alt.example"})

    assert [evil.status_code, wrong_port.status_code, listed.status_code] == [421, 421, 200]


async def test_a_host_in_upper_case_is_421_the_sdk_matches_exactly(transport_app, token):
    """R10: the allowlist holds the lower-case host; the SDK compares case-sensitively (clients send lower case)."""

    async with serving(transport_app) as client:
        response = await rpc(client, token, headers={"Host": "LOCALHOST:8002"})

    assert response.status_code == 421


async def test_an_origin_outside_the_allowlist_is_403_and_an_allowed_one_passes(transport_app, token):
    async with serving(transport_app) as client:
        evil = await rpc(client, token, headers={"Origin": "https://evil.example"})
        allowed = await rpc(client, token, headers={"Origin": ORIGIN})
        absent = await rpc(client, token)

    assert [evil.status_code, allowed.status_code, absent.status_code] == [403, 200, 200]


async def test_without_a_token_host_and_origin_violations_answer_401(transport_app):
    """Decision R1: the guard runs first; a rebinding request without a bearer token never reaches the transport."""

    async with serving(transport_app) as client:
        host = await rpc(client, headers={"Host": "evil.example"})
        origin = await rpc(client, headers={"Origin": "https://evil.example"})

    assert [host.status_code, origin.status_code] == [401, 401]


async def test_a_non_json_content_type_is_400(transport_app, token):
    async with serving(transport_app) as client:
        response = await client.post(
            "/mcp",
            content=b"{}",
            headers={**JSON_HEADERS, "Content-Type": "text/plain", "Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 400


# --- FR-MCP-OPS-05: the 1 MiB body cap -----------------------------------------------


async def test_a_two_mib_body_is_413_declared_or_streamed(transport_app, token):
    headers = {**JSON_HEADERS, "Authorization": f"Bearer {token}"}

    async def chunks():
        for _ in range(32):
            yield b"x" * (64 * 1024)

    async with serving(transport_app) as client:
        declared = await client.post("/mcp", content=b"x" * (2 * ONE_MIB), headers=headers)
        streamed = await client.post("/mcp", content=chunks(), headers=headers)
        ordinary = await rpc(client, token, headers={})

    assert [declared.status_code, streamed.status_code, ordinary.status_code] == [413, 413, 200]


@pytest.mark.parametrize(("size", "over_the_cap"), [(ONE_MIB, False), (ONE_MIB + 1, True)], ids=["1 MiB", "1 MiB + 1"])
async def test_the_cap_is_exactly_one_mib(transport_app, token, size, over_the_cap):
    """FR-MCP-OPS-05 boundary: 1 MiB passes the cap check (not JSON-RPC, so 400); one byte more is 413."""

    headers = {**JSON_HEADERS, "Authorization": f"Bearer {token}"}

    async def streamed():
        yield b" " * size

    async with serving(transport_app) as client:
        declared = await client.post("/mcp", content=b" " * size, headers=headers)
        chunked = await client.post("/mcp", content=streamed(), headers=headers)

    assert [r.status_code == 413 for r in (declared, chunked)] == [over_the_cap] * 2


async def test_a_big_body_without_a_token_is_401_and_never_read(transport_app):
    async with serving(transport_app) as client:
        response = await client.post("/mcp", content=b"x" * (2 * ONE_MIB), headers=JSON_HEADERS)

    assert response.status_code == 401


# --- FR-MCP-OPS-06: everything else ----------------------------------------------------


async def test_unknown_paths_are_404_with_the_json_envelope_and_no_redirect(transport_app, token):
    async with serving(transport_app) as client:
        responses = [
            await client.get("/nope"),
            await client.post("/mcp/", headers={"Authorization": f"Bearer {token}"}),
            await client.get("//mcp"),
            await client.get("/MCP"),
        ]

    assert [r.status_code for r in responses] == [404] * 4
    assert all(r.json()["error"]["code"] == "not_found" for r in responses)
    assert all("location" not in r.headers for r in responses)


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "HEAD", "PATCH"])
async def test_only_post_is_served_on_mcp(transport_app, token, method):
    """No server-initiated stream and no session termination: the other methods are 405 (with a token)."""

    async with serving(transport_app) as client:
        response = await client.request(method, "/mcp", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"
    if method != "HEAD":
        assert response.json()["error"]["code"] == "method_not_allowed"


async def test_the_guard_answers_before_the_method_check(transport_app):
    async with serving(transport_app) as client:
        response = await client.get("/mcp")

    assert response.status_code == 401


async def test_other_routes_do_not_take_a_token(transport_app):
    async with serving(transport_app) as client:
        posted = await client.post("/healthz")

    assert posted.status_code == 405
    assert posted.json()["error"]["code"] == "method_not_allowed"


class BrokenReadiness:
    """A readiness double whose snapshot fails unexpectedly: a bug behind the runtime boundary."""

    async def snapshot(self) -> None:
        raise ZeroDivisionError("secret detail")


async def test_an_unexpected_failure_is_500_json_logged_with_its_class_and_never_echoed(valid_env, mcp_log):
    runtime = dataclasses.replace(fake_runtime(), readiness=cast(Any, BrokenReadiness()))
    app = build_app(mcp_env(valid_env), runtime=runtime)
    # Starlette re-raises after answering; the quiet transport returns the answer instead.
    quiet = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url=f"http://{MCP_HOST}"
    )
    async with serving(app), quiet:
        response = await quiet.get("/readyz")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret" not in response.text
    failed = [r for r in mcp_log.records if getattr(r, "event", "") == "mcp.request.failed"]
    assert [(r.method, r.path, r.error_type) for r in failed] == [("GET", "/readyz", "ZeroDivisionError")]
    assert failed[0].exc_info is not None


# --- R11: a client that disconnects mid-POST -------------------------------------------


async def test_a_client_disconnect_mid_post_logs_no_request_failed(transport_app, token, mcp_log):
    messages = iter(
        [
            {"type": "http.request", "body": b'{"jsonrpc"', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    async def send(message):
        """The client is gone: the answer (the SDK's own 500) goes nowhere and is not the subject here."""

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", MCP_HOST.encode()),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"content-length", b"1000"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8002),
        "state": {},
    }
    async with serving(transport_app) as client:
        await transport_app(scope, receive, send)
        again = await rpc(client, token)

    assert not [r for r in mcp_log.records if getattr(r, "event", "") == "mcp.request.failed"]
    assert again.status_code == 200  # the endpoint still answers after the aborted request


# --- SEC-M8-1 / FR-MCP-OPS-07: one request_id per HTTP request ---------------------------------


def probing_tools(server: MCPServer) -> None:
    """A tool that logs two events: they must share the request's id."""

    def probe() -> str:
        log = get_logger("tests.probe")
        log_event(log, INFO, "test.probe.first")
        log_event(log, INFO, "test.probe.second")
        return "ok"

    server.add_tool(probe, name="probe", description="logs two events")


async def test_each_request_binds_its_own_request_id(valid_env, token, central_log):
    central_log.configure()
    app = build_app(mcp_env(valid_env), tools=probing_tools)
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "probe", "arguments": {}}}
    async with serving(app) as client:
        first = await rpc(client, token, call)
        second = await rpc(client, token, call)
        rejected = await rpc(client, None)  # 401 path: no token

    assert (first.status_code, second.status_code, rejected.status_code) == (200, 200, 401)
    first_ids = [e["request_id"] for e in central_log.events("test.probe.first")]
    second_ids = [e["request_id"] for e in central_log.events("test.probe.second")]
    assert len(first_ids) == len(second_ids) == 2
    assert first_ids[0] == second_ids[0] and first_ids[1] == second_ids[1]  # within one request
    (rejected_id,) = [e["request_id"] for e in central_log.events("mcp.auth.rejected")]
    assert len({first_ids[0], first_ids[1], rejected_id}) == 3  # across requests
