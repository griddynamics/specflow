"""Shared fakes of the GitHub integration tests (no tests in here): a scripted GitHub behind ``httpx.MockTransport``
(``github.com`` OAuth and ``api.github.com`` REST), an in-memory ``ConnectionStore`` and the settings. Every value is a
fake; the canaries must never reach a log line, an error or a response."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
from cryptography.fernet import Fernet

from connection_store_support import MemoryConnectionStore
from github.client import GitHubClient, GitHubUser
from github.connections import StoredConnection
from settings import GitHubSettings

CLIENT_ID = "Iv23-client-1"
CLIENT_CANARY = "gh-client-secret-canary-91a"
TOKEN_KEY = Fernet.generate_key().decode()
CODE_CANARY = "gh-code-canary-3b7"
LOGIN = "login-canary-octo"
USER_ID = 5150
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def github_env() -> dict[str, str]:
    return {
        "GITHUB_APP_CLIENT_ID": CLIENT_ID,
        "GITHUB_APP_CLIENT_SECRET": CLIENT_CANARY,
        "GITHUB_APP_SLUG": "specflow-local",
        "GITHUB_TOKEN_KEY": TOKEN_KEY,
        "PUBLIC_URL": "http://localhost:3001",
    }


def github_settings() -> GitHubSettings:
    return GitHubSettings(**github_env())


def repo(repo_id: int, name: str, *, private: bool = True, size: int = 10, branch: str = "main") -> dict[str, Any]:
    return {
        "id": repo_id,
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "private": private,
        "default_branch": branch,
        "size": size,
    }


class FakeGitHub:
    """A scripted GitHub. ``installations`` maps a GitHub user id to ``{installation_id: [repo, ...]}``. Tokens are
    ``access-N`` / ``refresh-N``; a refresh rotates the refresh token (the old one dies, as on GitHub)."""

    def __init__(self) -> None:
        self.codes: dict[str, int] = {CODE_CANARY: USER_ID}
        self.logins: dict[int, str] = {USER_ID: LOGIN}
        self.access: dict[str, int] = {}
        self.refresh: dict[str, int] = {}
        self.installations: dict[int, dict[int, list[dict[str, Any]]]] = {USER_ID: {}}
        self.calls: list[tuple[str, str]] = []
        self.grant_error: str | None = None  # the next token call answers 200 {"error": ...}
        self.with_refresh = True  # False: the app does not expire user tokens (no refresh token in a grant)
        self.status: int | None = None  # every call answers this status (503: GitHub down)
        self.revoked: list[str] = []
        self.refreshes = 0
        self.expires_in = 28800
        self.gate: asyncio.Event | None = None  # a refresh waits for it
        self.entered = asyncio.Event()  # set when a refresh reaches the gate
        self._serial = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> GitHubClient:
        return GitHubClient(
            CLIENT_ID,
            github_settings().client_secret,
            "http://localhost:3001/api/integrations/github/callback",
            transport=self.transport(),
        )

    def _issue(self, user_id: int) -> dict[str, Any]:
        self._serial += 1
        access, refresh = f"access-{self._serial}", f"refresh-{self._serial}"
        self.access[access], self.refresh[refresh] = user_id, user_id
        grant: dict[str, Any] = {"access_token": access, "scope": "", "token_type": "bearer"}
        if self.with_refresh:
            grant |= {"expires_in": self.expires_in, "refresh_token": refresh, "refresh_token_expires_in": 15897600}
        return grant

    def expire_access(self) -> None:
        """GitHub revokes every access token early (a 401 next)."""

        self.access.clear()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.status is not None:
            return httpx.Response(self.status)
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            return await self._token(request)
        if request.url.host != "api.github.com":
            return httpx.Response(404)
        if request.method == "DELETE" and request.url.path == f"/applications/{CLIENT_ID}/grant":
            self.revoked.append(json.loads(request.content)["access_token"])
            return httpx.Response(204)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        user_id = self.access.get(token)
        if user_id is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        return self._rest(request, user_id)

    async def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_CANARY:
            return httpx.Response(200, json={"error": "incorrect_client_credentials"})
        if self.grant_error is not None:
            error, self.grant_error = self.grant_error, None
            return httpx.Response(200, json={"error": error, "error_description": "text canary from github"})
        if form.get("grant_type") == "refresh_token":
            if self.gate is not None:
                self.entered.set()
                await self.gate.wait()
            self.refreshes += 1
            user_id = self.refresh.pop(form.get("refresh_token", ""), None)
            if user_id is None:
                return httpx.Response(200, json={"error": "bad_refresh_token"})
            return httpx.Response(200, json=self._issue(user_id))
        user_id = self.codes.pop(form.get("code", ""), None)
        if user_id is None:
            return httpx.Response(200, json={"error": "bad_verification_code"})
        return httpx.Response(200, json=self._issue(user_id))

    def _page(self, request: httpx.Request, items: list[Any], key: str) -> httpx.Response:
        per_page, page = int(request.url.params["per_page"]), int(request.url.params["page"])
        chunk = items[(page - 1) * per_page : page * per_page]
        return httpx.Response(200, json={"total_count": len(items), key: chunk})

    def _rest(self, request: httpx.Request, user_id: int) -> httpx.Response:
        path, installations = request.url.path, self.installations.get(user_id, {})
        if path == "/user":
            return httpx.Response(200, json={"id": user_id, "login": self.logins[user_id]})
        if path == "/user/installations":
            return self._page(request, [{"id": i} for i in installations], "installations")
        if path.startswith("/user/installations/") and path.endswith("/repositories"):
            installation = installations.get(int(path.split("/")[3]))
            missing = installation is None
            return httpx.Response(404) if missing else self._page(request, installation or [], "repositories")
        if path.startswith("/repositories/"):
            wanted = int(path.rsplit("/", 1)[1])
            found = [item for items in installations.values() for item in items if item["id"] == wanted]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404)
        return httpx.Response(404)


class MemoryStore(MemoryConnectionStore):
    """The ``ConnectionStore`` over a dict (``connection_store_support``)."""

    async def save(
        self,
        user_id: uuid.UUID,
        user: GitHubUser,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime | None,
        now: datetime,
    ) -> None:
        self.rows[user_id] = StoredConnection(user_id, user.id, user.login, refresh_token, "active", now, now)
        self.refresh_expires[user_id] = refresh_expires_at
        self.events.append(("save", user_id))
