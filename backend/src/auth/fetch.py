"""A hardened one-shot JSON GET for JWKS and OIDC discovery documents.

* overall monotonic deadline: a watchdog shuts the socket down when it
  passes, so a slow-drip or hanging server cannot keep the thread alive;
* bounded body (``MAX_BODY_BYTES``), fail closed when larger;
* redirects are never followed (a 3xx fails with ``redirect_refused``), so a
  host check on the URL cannot be bypassed by a redirect;
* http(s) only.

Failures raise ``FetchError`` carrying a fixed ``failure`` code for logs.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import ssl
import threading
import time
from http import HTTPStatus
from typing import Final
from urllib.parse import urlsplit

MAX_BODY_BYTES: Final = 1024 * 1024
_CHUNK_BYTES: Final = 64 * 1024


class FetchError(Exception):
    """A failed fetch; ``failure`` is a fixed code (never response content)."""

    def __init__(self, failure: str) -> None:
        super().__init__(failure)
        self.failure = failure


def fetch_json(url: str, *, timeout: float) -> object:
    """GET ``url`` and parse JSON within ``timeout`` seconds overall."""

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("invalid_url")
    deadline = time.monotonic() + timeout
    conn = _connection(parts.scheme, parts.hostname, parts.port, timeout)
    watchdog = threading.Timer(timeout, _abort, args=(conn,))
    watchdog.daemon = True
    watchdog.start()
    body: bytes | None = None
    failure = "connection"
    try:
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request("GET", path, headers={"Accept": "application/json"})
        response = conn.getresponse()
        _check_status(response.status)
        body = _read_bounded(response, deadline)
    except FetchError:
        raise
    except (OSError, http.client.HTTPException):
        failure = "timeout" if time.monotonic() >= deadline else "connection"
    finally:
        watchdog.cancel()
        conn.close()
    if body is None:
        raise FetchError(failure)
    try:
        return json.loads(body)
    except ValueError:
        raise FetchError("invalid_json") from None


def _check_status(status: int) -> None:
    if HTTPStatus.MULTIPLE_CHOICES <= status < HTTPStatus.BAD_REQUEST:
        raise FetchError("redirect_refused")
    if status != HTTPStatus.OK:
        raise FetchError("http_status")


def _connection(scheme: str, host: str, port: int | None, timeout: float) -> http.client.HTTPConnection:
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())
    return http.client.HTTPConnection(host, port, timeout=timeout)


def _read_bounded(response: http.client.HTTPResponse, deadline: float) -> bytes:
    declared = response.getheader("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise FetchError("too_large")
    body = bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise FetchError("timeout")
        chunk = response.read1(_CHUNK_BYTES)
        if not chunk:
            break
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise FetchError("too_large")
    if time.monotonic() >= deadline:  # EOF caused by the watchdog's shutdown
        raise FetchError("timeout")
    return bytes(body)


def _abort(conn: http.client.HTTPConnection) -> None:
    """Watchdog: wake any blocked recv/send by shutting the socket down."""

    sock = conn.sock
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
