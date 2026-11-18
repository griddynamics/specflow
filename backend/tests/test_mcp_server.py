"""``mcp_server.server``: the startup-failure exit code and a SIGTERM that stops it gracefully (FR-MCP-OPS-01)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import anyio
import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from mcp.types import METHOD_NOT_FOUND

from mcp_server import server
from mcp_support import BACKEND, MCP_HOST, build_app, mcp_env, mcp_token
from settings import load_settings

# The real command path (``cli.main(["mcp"])`` -> ``mcp_server.server.run`` -> ``_GracefulServer``); only the API
# transport is replaced, through the app factory's seam, so no API is needed. An optional argument replaces the
# graceful-shutdown bound, so a test of the bound does not wait the production value.
SIGTERM_SCRIPT = textwrap.dedent(
    """
    import sys
    sys.path[:0] = ["src", "tests"]
    from mcp_server import server
    import httpx
    from mcp_support import ready_api
    import cli

    real = server.create_mcp_app
    server.create_mcp_app = lambda settings: real(settings, transport=httpx.MockTransport(ready_api))
    if len(sys.argv) > 1:
        server.GRACEFUL_SHUTDOWN_SECONDS = int(sys.argv[1])
    sys.exit(cli.main(["mcp"]))
    """
)
STARTED = "mcp.startup.completed"
SHUTDOWN = "mcp.shutdown.completed"
WAIT_SECONDS = 30
# A stop that waits for a held request takes as long as the orchestrator lets it; a prompt one takes about a second.
STOP_SECONDS = 10
GRACEFUL_BOUND_HIT = "timeout graceful shutdown exceeded"  # uvicorn's error line when the bound cancels requests


@dataclass
class Served:
    process: subprocess.Popen[str]
    port: int
    output: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.output)


def _lines(stream, found: threading.Event, output: list[str]) -> None:
    for line in stream:
        output.append(line)
        if STARTED in line:
            found.set()


@contextmanager
def _serving(valid_env: dict[str, str], *args: str) -> Iterator[Served]:
    """``specflow mcp`` in a subprocess on a free port, started; killed on exit if it still runs.

    ``Host: 127.0.0.1:<port>`` is allowed, so a test can call ``/mcp`` on the real socket.
    """

    port = free_port()
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/"),
        **mcp_env(valid_env, MCP_PORT=str(port), MCP_ALLOWED_HOSTS=f"127.0.0.1:{port}"),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", SIGTERM_SCRIPT, *args],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    served = Served(process, port)
    started = threading.Event()
    reader = threading.Thread(target=_lines, args=(process.stdout, started, served.output), daemon=True)
    reader.start()
    try:
        assert started.wait(WAIT_SECONDS), served.text
        yield served
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        reader.join(WAIT_SECONDS)
        process.stdout.close()


def test_a_sigterm_ends_the_real_server_with_its_shutdown_event_and_exit_0(valid_env):
    """uvicorn re-raises SIGTERM after a graceful stop (exit 143, no logger flush); ``run`` must not."""

    with _serving(valid_env) as served:
        served.process.send_signal(signal.SIGTERM)
        code = served.process.wait(timeout=WAIT_SECONDS)

    assert code == 0, served.text
    assert SHUTDOWN in served.text, served.text


async def test_a_client_that_asks_for_subscriptions_listen_does_not_delay_a_sigterm(valid_env):
    """Claude Code's v2 MCP runtime opens ``subscriptions/listen`` (protocol 2026-07-28) and holds it for its whole
    session. Served, the stream kept uvicorn "Waiting for connections to close" until the orchestrator killed it.
    The role refuses listen, so the client holds no request and a SIGTERM stops the server at once."""

    refused: list[int] = []
    asked = anyio.Event()

    async def client_session(port: int) -> None:
        # No read timeout: httpx's default 5 s would drop an idle stream that Claude Code keeps open.
        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {mcp_token()}"}, timeout=httpx2.Timeout(WAIT_SECONDS, read=None)
        )
        url = f"http://127.0.0.1:{port}/mcp"
        async with http, Client(streamable_http_client(url, http_client=http)) as client:
            try:
                async with client.listen(tools_list_changed=True):
                    asked.set()
                    await anyio.sleep_forever()  # held as Claude Code holds it
            except MCPError as error:
                refused.append(error.code)
                asked.set()
                await anyio.sleep_forever()  # the session stays open, with nothing in flight

    with _serving(valid_env) as served:
        async with anyio.create_task_group() as group:
            group.start_soon(client_session, served.port)
            with anyio.fail_after(WAIT_SECONDS):
                await asked.wait()
            served.process.send_signal(signal.SIGTERM)
            try:
                code = await anyio.to_thread.run_sync(lambda: served.process.wait(timeout=STOP_SECONDS))
            finally:
                group.cancel_scope.cancel()

    assert code == 0, served.text
    assert SHUTDOWN in served.text, served.text
    assert refused == [METHOD_NOT_FOUND], served.text
    assert GRACEFUL_BOUND_HIT not in served.text, served.text  # it stopped at once, not by the backstop


def test_a_request_that_never_completes_is_cut_after_the_graceful_shutdown_bound(valid_env):
    """The backstop: whatever else holds a request open (here a body that never arrives) delays a SIGTERM by at
    most ``GRACEFUL_SHUTDOWN_SECONDS`` (1 s in this test), then the request is cancelled and the server stops."""

    with _serving(valid_env, "1") as served, socket.create_connection(("127.0.0.1", served.port)) as stuck:
        stuck.sendall(
            (
                "POST /mcp HTTP/1.1\r\n"
                f"Host: {MCP_HOST}\r\n"
                f"Authorization: Bearer {mcp_token()}\r\n"
                "Content-Type: application/json\r\n"
                "Accept: application/json, text/event-stream\r\n"
                "Content-Length: 1000\r\n"
                "\r\n"
                '{"jsonrpc": "2.0"'
            ).encode()
        )
        # A request on a second connection: once it is answered, the server has read the stuck one's headers too.
        with urllib.request.urlopen(f"http://127.0.0.1:{served.port}/healthz", timeout=WAIT_SECONDS) as probe:
            assert probe.status == 200
        served.process.send_signal(signal.SIGTERM)
        code = served.process.wait(timeout=STOP_SECONDS)

    assert code == 0, served.text
    assert SHUTDOWN in served.text, served.text
    assert GRACEFUL_BOUND_HIT in served.text, served.text


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_failed_startup_exits_the_process_with_3(valid_env, monkeypatch):
    def broken(*_args, **_kwargs):
        raise ConnectionError("no api")

    env = mcp_env(valid_env, MCP_PORT=str(free_port()))
    monkeypatch.setattr("mcp_server.app.build_api_client", broken)
    monkeypatch.setattr(server, "create_mcp_app", lambda _settings: build_app(env))

    with pytest.raises(SystemExit) as raised:  # uvicorn itself exits with its startup-failure code
        server.run(load_settings("mcp", env))

    assert raised.value.code == server.STARTUP_FAILED
