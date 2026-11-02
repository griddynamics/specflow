"""``specflow mcp``: uvicorn on ``0.0.0.0:$MCP_PORT`` with the central logger only."""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Iterator
from ipaddress import IPv4Address
from typing import override

import uvicorn
from uvicorn.server import HANDLED_SIGNALS

from api.mcp.app import create_mcp_app
from api.mcp.http import required
from settings import RoleSettings

HOST = str(IPv4Address(0))  # 0.0.0.0, every interface of the container
STARTUP_FAILED = 3


class _GracefulServer(uvicorn.Server):
    """uvicorn's server, except that a signal ends the process normally.

    uvicorn re-raises a captured SIGTERM after the graceful stop, so the process died of the
    signal (exit 143) before ``atexit`` could flush the central logger and the shutdown event
    was lost. Here the handlers are restored and the signal is not re-raised: ``run`` returns 0.
    """

    @override
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield  # handlers can only be installed from the main thread
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)


def run(settings: RoleSettings) -> int:
    """Serve until SIGTERM/SIGINT; 0 after a normal stop, 3 when the startup failed."""

    mcp = required(settings.mcp, "the mcp role requires mcp settings")
    config = uvicorn.Config(
        create_mcp_app(settings),
        host=HOST,
        port=mcp.port,
        lifespan="on",
        log_config=None,
        access_log=False,
        server_header=False,
    )
    server = _GracefulServer(config)
    server.run()
    return 0 if server.started else STARTUP_FAILED
