"""Liveness endpoint of a worker process: ``GET /healthz`` answers 200 while it runs.

A worker has no HTTP API, but a platform that needs a port and an HTTP probe
(for example Kubernetes) can probe this one. It checks no dependency on purpose: a Temporal
or database outage must not restart every worker (readiness stays
``specflow worker --check``). The server runs in a daemon thread from the
standard library, so it needs no event loop and no extra dependency.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
from typing import Final, override

from logger.logger import ERROR, INFO, get_logger, log_event

logger = get_logger("temporal.health_server")

HOST: Final = str(IPv4Address(0))  # 0.0.0.0, every interface of the container (as api.server)
PATH: Final = "/healthz"
#: Seconds the serve thread gets to end on ``close()``.
CLOSE_JOIN_SECONDS: Final = 5
_BODY: Final = b'{"status":"ok"}'


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self._answer(with_body=True)

    def do_HEAD(self) -> None:
        self._answer(with_body=False)

    def _answer(self, *, with_body: bool) -> None:
        found = self.path.split("?", 1)[0] == PATH
        body = _BODY if found else b'{"status":"not_found"}'
        self.send_response(200 if found else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: object) -> None:
        """No access log: probes run every few seconds."""


class HealthServer:
    """A running liveness server; ``close()`` stops it and frees the port."""

    def __init__(self, server: ThreadingHTTPServer) -> None:
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="worker-health", daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=CLOSE_JOIN_SECONDS)


def start(port: int, host: str = HOST) -> HealthServer | None:
    """Serve ``/healthz`` on ``host:port``; a port in use is logged, not fatal."""

    try:
        server = ThreadingHTTPServer((host, port), _Handler)
    except OSError as error:
        log_event(
            logger,
            ERROR,
            "worker.health.unavailable",
            port=port,
            error_type=type(error).__name__,
        )
        return None
    server.daemon_threads = True
    health = HealthServer(server)
    log_event(logger, INFO, "worker.health.started", port=health.port, path=PATH)
    return health
