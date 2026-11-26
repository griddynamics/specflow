"""Shared fakes of the Linear connection tests (no tests in here): a scripted Linear behind ``httpx.MockTransport`` (the
token endpoint with PKCE, the revoke endpoint and the ``viewer`` read), an in-memory ``ConnectionStore`` and the
settings. Every value is a fake; the canaries must never reach a log line, an error or a response."""

from __future__ import annotations

import base64
import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.fernet import Fernet

from linear.connections import LinearConnections, StoredConnection
from linear.models import Viewer
from linear.oauth import LinearOAuthClient
from settings import LinearSettings

CLIENT_ID = "linear-client-1"
CLIENT_CANARY = "linear-client-secret-canary-91a"
TOKEN_KEY = Fernet.generate_key().decode()
CODE_CANARY = "linear-code-canary-3b7"
NAME_CANARY = "member-name-canary"
WORKSPACE = ("org-1", "Acme Workspace", "acme")
REDIRECT = "http://localhost:3001/api/integrations/linear/callback"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
USER = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)


def linear_settings() -> LinearSettings:
    return LinearSettings(
        LINEAR_OAUTH_CLIENT_ID=CLIENT_ID,
        LINEAR_OAUTH_CLIENT_SECRET=CLIENT_CANARY,
        LINEAR_TOKEN_KEY=TOKEN_KEY,
        PUBLIC_URL="http://localhost:3001",
    )


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class Clock:
    """One clock for the connections' wall time, monotonic time and signing time."""

    def __init__(self) -> None:
        self.seconds = 1_000_000.0

    def __call__(self) -> float:
        return self.seconds

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.seconds, UTC)


class FakeLinear:
    """A scripted Linear. Tokens are ``access-N`` / ``refresh-N``; a refresh rotates the refresh token (the old one
    dies). ``challenges`` holds the PKCE challenge of each code the test registered with ``allow``."""

    def __init__(self) -> None:
        self.challenges: dict[str, str] = {}
        self.access: set[str] = set()
        self.refresh: set[str] = set()
        self.revoked: list[tuple[str, str]] = []
        self.calls: list[str] = []
        self.refreshes = 0
        self.status: int | None = None  # every call answers this status (503: Linear down)
        self.error: str | None = None  # the next token call answers 400 with this OAuth error
        self.revoke_status = 200
        self.expires_in = 86400
        self._serial = 0

    def allow(self, code: str, challenge: str) -> None:
        self.challenges[code] = challenge

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> LinearOAuthClient:
        return LinearOAuthClient(CLIENT_ID, linear_settings().client_secret, REDIRECT, transport=self.transport())

    def _issue(self) -> dict[str, Any]:
        self._serial += 1
        access, refresh = f"access-{self._serial}", f"refresh-{self._serial}"
        self.access.add(access)
        self.refresh.add(refresh)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "scope": ["read", "write"],
            "refresh_token": refresh,
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.status is not None:
            return httpx.Response(self.status)
        if path == "/oauth/token":
            return self._token({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        if path == "/oauth/revoke":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.revoked.append((form["token"], form["token_type_hint"]))
            return httpx.Response(self.revoke_status)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if path != "/graphql" or token not in self.access:
            return httpx.Response(401, json={"errors": [{"message": "text canary from linear"}]})
        organization = {"id": WORKSPACE[0], "name": WORKSPACE[1], "urlKey": WORKSPACE[2]}
        return httpx.Response(
            200, json={"data": {"viewer": {"id": "lin-user-1", "name": NAME_CANARY, "organization": organization}}}
        )

    def _token(self, form: dict[str, str]) -> httpx.Response:
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_CANARY:
            return httpx.Response(401, json={"error": "invalid_client"})
        if self.error is not None:
            error, self.error = self.error, None
            return httpx.Response(400, json={"error": error, "error_description": "text canary from linear"})
        if form.get("grant_type") == "refresh_token":
            self.refreshes += 1
            if form.get("refresh_token", "") not in self.refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.refresh.discard(form["refresh_token"])
            return httpx.Response(200, json=self._issue())
        challenge = self.challenges.pop(form.get("code", ""), None)
        if challenge is None or s256(form.get("code_verifier", "")) != challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json=self._issue())


class MemoryStore:
    """The ``ConnectionStore`` over a dict; ``transaction()`` keeps nothing of a failed block. ``events`` records every
    write and each commit."""

    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, StoredConnection] = {}
        self.events: list[tuple[str, Any]] = []
        self.expired_at: dict[uuid.UUID, datetime] = {}

    def _log(self, event: str, subject: Any = None) -> None:
        self.events.append((event, subject))

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryStore]:
        snapshot = self.rows.copy()
        outcome = "commit"
        try:
            yield self
        except BaseException:
            self.rows, outcome = snapshot, "rollback"
            raise
        finally:
            self._log(outcome)

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None:
        return self.rows.get(user_id)

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        self._log("lock", user_id)
        return await self.get(user_id)

    async def save(
        self,
        user_id: uuid.UUID,
        viewer: Viewer,
        *,
        scopes: str,
        tokens: tuple[bytes, bytes],
        expires_at: datetime,
        now: datetime,
    ) -> None:
        organization = viewer.organization
        who = (viewer.id, viewer.name, organization.id, organization.name, organization.url_key)
        self.rows[user_id] = StoredConnection(
            user_id, *who, scopes, tokens[0], tokens[1], expires_at, "active", now, now
        )
        self._log("save", user_id)

    async def rotate(
        self, user_id: uuid.UUID, *, tokens: tuple[bytes, bytes], expires_at: datetime, now: datetime
    ) -> None:
        self.rows[user_id] = replace(
            self.rows[user_id], access_token=tokens[0], refresh_token=tokens[1], expires_at=expires_at, last_used_at=now
        )
        self._log("rotate", user_id)

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None:
        self.rows[user_id] = replace(self.rows[user_id], status="expired")
        self.expired_at[user_id] = now
        self._log("expire", user_id)

    async def delete(self, user_id: uuid.UUID) -> bool:
        self._log("delete", user_id)
        return self.rows.pop(user_id, None) is not None


def make_connections(linear: FakeLinear, store: MemoryStore, clock: Clock | None = None) -> LinearConnections:
    clock = clock or Clock()
    return LinearConnections(
        linear_settings(), store=store.transaction, client=linear.client(), now=clock.now, monotonic=clock, clock=clock
    )


def consent_of(url: str) -> dict[str, str]:
    """The query of a consent URL as a dict."""

    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


async def connected(
    linear: FakeLinear, store: MemoryStore, clock: Clock | None = None, return_to: str = "/projects/p/integrations"
) -> LinearConnections:
    """Connections after ``USER`` went through consent and callback (the fake knows the PKCE challenge of the code)."""

    connections = make_connections(linear, store, clock)
    query = consent_of(connections.authorize_url(USER, return_to))
    linear.allow(CODE_CANARY, query["code_challenge"])
    await connections.connect(USER, code=CODE_CANARY, state=query["state"])
    return connections
