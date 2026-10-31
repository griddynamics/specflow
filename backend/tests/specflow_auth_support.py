"""Shared offline helpers for the specflow auth tests (no tests in here).

Keys are generated in-process; the JWKS endpoint is a stub HTTP server bound
to 127.0.0.1 on a random port that counts requests, can rotate its key set,
and can hang. Nothing here touches the network beyond that loopback stub.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from auth.claims import ClaimPolicy
from settings import AuthSettings

ISSUER = "http://127.0.0.1/realms/specflow-test"
AUDIENCE = "specflow-api"
CLIENT_ID = "specflow-web"
DOMAIN = "example.com"
EMAIL = f"alice@{DOMAIN}"
# Synthetic, test-only HS256 key material (not a real secret).
DEV_KEY = "k" * 40

POLICY = ClaimPolicy.build(
    issuer=ISSUER,
    audience=AUDIENCE,
    client_ids=[CLIENT_ID],
    allowed_email_domains=[DOMAIN],
    typ_rule="keycloak",  # the earlier tests assert the claim `typ` rule
)


def claims(**overrides: Any) -> dict[str, Any]:
    """A valid claim set; ``overrides`` replace keys, ``None`` removes one."""

    now = int(time.time())
    base: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "azp": CLIENT_ID,
        "sub": "user-123",
        "typ": "Bearer",
        "email": EMAIL,
        "email_verified": True,
        "name": "Alice Example",
        "iat": now,
        "exp": now + 300,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def jwk(private_key: Any, kid: str) -> dict[str, Any]:
    """The public JWK for ``private_key`` with ``kid``/``use``/``alg``."""

    if isinstance(private_key, rsa.RSAPrivateKey):
        data = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        alg = "RS256"
    else:
        data = ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
        alg = "ES256"
    return {**data, "kid": kid, "use": "sig", "alg": alg}


def sign(payload: dict[str, Any], key: Any, *, kid: str | None, alg: str = "RS256") -> str:
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(payload, key, algorithm=alg, headers=headers)


def auth_settings(**env: str) -> AuthSettings:
    """``AuthSettings`` built directly (no ``load_settings`` guards)."""

    values = {
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER": ISSUER,
        "AUTH_API_AUDIENCE": AUDIENCE,
        "AUTH_API_CLIENT_IDS": CLIENT_ID,
        "AUTH_ALLOWED_EMAIL_DOMAINS": DOMAIN,
        "AUTH_TOKEN_TYPE": "keycloak",
    }
    values.update(env)
    return AuthSettings(**values)


class _StubHandler(BaseHTTPRequestHandler):
    """The loopback endpoint; the ``StubIssuer`` that owns the server shapes every answer."""

    server: _StubServer

    def do_GET(self) -> None:
        stub = self.server.stub
        if self.path == "/jwks":
            with stub._lock:
                stub.jwks_fetches += 1
            if stub.mode != "ok":
                self._misbehave(stub.mode)
                return
            body: dict[str, Any] = {"keys": list(stub.keys)}
        elif self.path == "/realms/t/.well-known/openid-configuration":
            with stub._lock:
                stub.discovery_fetches += 1
            if stub.discovery_redirect:
                self._redirect(stub.discovery_redirect)
                return
            body = {"jwks_uri": stub.jwks_uri_override or f"{stub.base}/jwks"}
        else:
            self.send_response(404)
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _misbehave(self, mode: str) -> None:
        stub = self.server.stub
        if mode == "hang":
            stub.release.wait(timeout=30)
        elif mode == "redirect":
            self._redirect(stub.redirect_to or "http://localhost:1/jwks")
        elif mode == "huge_declared":
            self.send_response(200)
            self.send_header("Content-Length", str(60 * 1024 * 1024))
            self.end_headers()
            self.wfile.write(b"[" * 1024)
        elif mode in ("drip", "huge_streamed"):
            self.send_response(200)
            self.end_headers()
            self._stream(mode)

    def _stream(self, mode: str) -> None:
        stub = self.server.stub
        try:
            if mode == "huge_streamed":
                for _ in range(32):
                    self.wfile.write(b" " * 65536)
            while mode == "drip" and not stub.release.wait(timeout=0.2):
                self.wfile.write(b" ")
                self.wfile.flush()
        except OSError:
            return  # the client gave up: expected

    def log_message(self, *_: Any) -> None:  # silence stderr
        return


class _StubServer(ThreadingHTTPServer):
    stub: StubIssuer


class StubIssuer:
    """A loopback JWKS + discovery endpoint with request counters.

    ``mode`` shapes the JWKS answer: ``ok``, ``hang`` (no answer),
    ``drip`` (1 byte per 0.2 s), ``huge_declared`` (Content-Length 60 MB),
    ``huge_streamed`` (2 MiB with no Content-Length) or ``redirect`` (302 to
    ``redirect_to``). ``discovery_redirect`` makes discovery answer a 302.
    """

    def __init__(self) -> None:
        self.keys: list[dict[str, Any]] = []
        self.jwks_fetches = 0
        self.discovery_fetches = 0
        self.mode = "ok"
        self.redirect_to: str | None = None
        self.discovery_redirect: str | None = None
        self.jwks_uri_override: str | None = None
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.server = _StubServer(("127.0.0.1", 0), _StubHandler)
        self.server.stub = self
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.jwks_url = f"{self.base}/jwks"
        self.issuer = f"{self.base}/realms/t"
        self._thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub_issuer() -> Iterator[StubIssuer]:
    stub = StubIssuer()
    try:
        yield stub
    finally:
        stub.close()


class FakeClock:
    """A controllable monotonic clock for cache lifetimes."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
