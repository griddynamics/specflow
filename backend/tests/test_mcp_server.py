"""``mcp_server.server``: the startup-failure exit code and a SIGTERM that stops it gracefully (FR-MCP-OPS-01)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading

import pytest

from mcp_server import server
from mcp_support import BACKEND, build_app, mcp_env
from settings import load_settings

# The real command path (``cli.main(["mcp"])`` -> ``mcp_server.server.run`` -> ``_GracefulServer``); only the API
# transport is replaced, through the app factory's seam, so no API is needed.
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
    sys.exit(cli.main(["mcp"]))
    """
)
STARTED = "mcp.startup.completed"
SHUTDOWN = "mcp.shutdown.completed"
WAIT_SECONDS = 30


def _lines(stream, found: threading.Event, output: list[str]) -> None:
    for line in stream:
        output.append(line)
        if STARTED in line:
            found.set()


def test_a_sigterm_ends_the_real_server_with_its_shutdown_event_and_exit_0(valid_env):
    """uvicorn re-raises SIGTERM after a graceful stop (exit 143, no logger flush); ``run`` must not."""

    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/"),
        **mcp_env(valid_env, MCP_PORT=str(free_port())),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", SIGTERM_SCRIPT],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    started = threading.Event()
    reader = threading.Thread(target=_lines, args=(process.stdout, started, output), daemon=True)
    reader.start()
    try:
        assert started.wait(WAIT_SECONDS), "".join(output)
        process.send_signal(signal.SIGTERM)
        code = process.wait(timeout=WAIT_SECONDS)
        reader.join(WAIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()

    text = "".join(output)
    assert code == 0, text
    assert SHUTDOWN in text, text


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
