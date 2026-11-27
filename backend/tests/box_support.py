"""Shared fakes of the Box integration tests (no tests in here): a scripted Box behind ``httpx.MockTransport``
(``api.box.com`` OAuth and REST, and the download host), recorded-shape JSON fixtures, an in-memory ``ConnectionStore``
and the settings. Both streams (connection and importer) use this module. Every value is a fake; the canaries must never
reach a log line, an error or a response.

The fake refuses (and records in ``violations``) every request that is not a ``GET``, except the two ``POST`` calls of
OAuth: ``tests/test_box_client.py`` asserts that none was made (decision B3, the client is read-only).
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import parse_qs

import httpx
from cryptography.fernet import Fernet

from box.client import BoxClient, BoxUser
from box.connections import StoredConnection
from connection_store_support import MemoryConnectionStore
from settings import BoxSettings

CLIENT_ID = "box-client-1"
CLIENT_CANARY = "box-client-secret-canary-91a"
TOKEN_KEY = Fernet.generate_key().decode()
CODE_CANARY = "box-code-canary-3b7"
SHARED_LINK_CANARY = "https://app.box.com/s/sharedlinkcanary9d2"
NAME_CANARY = "Name Canary"
USER_ID = "11446498"
ENTERPRISE_ID = "12345"
CALLBACK = "http://localhost:3001/api/integrations/box/callback"
T0 = datetime(2026, 1, 1, tzinfo=UTC)

# Recorded shapes (field names and nesting as the Box reference documents them, values invented) ---------------------

USERS_ME_ENTERPRISE: Final[dict[str, Any]] = {
    "type": "user",
    "id": USER_ID,
    "name": NAME_CANARY,
    "login": "login-canary@example.test",
    "enterprise": {"type": "enterprise", "id": ENTERPRISE_ID, "name": "Acme Corp"},
}
USERS_ME_PERSONAL: Final[dict[str, Any]] = {
    "type": "user",
    "id": "22557609",
    "name": "Pat Personal",
    "login": "pat@example.test",
    "enterprise": None,
}
TOKEN_RESPONSE: Final[dict[str, Any]] = {
    "access_token": "access-1",
    "expires_in": 3599,
    "restricted_to": [],
    "refresh_token": "refresh-1",
    "token_type": "bearer",
}
INVALID_GRANT: Final[dict[str, Any]] = {"error": "invalid_grant", "error_description": "Invalid refresh token"}
RATE_LIMITED: Final[dict[str, Any]] = {
    "type": "error",
    "status": 429,
    "code": "rate_limit_exceeded",
    "message": "Request rate limit exceeded, please try again later",
    "request_id": "abc123",
}
FILE_INFO: Final[dict[str, Any]] = {
    "type": "file",
    "id": "3001",
    "etag": "1",
    "name": "Spec v2.pdf",
    "size": 4096,
    "extension": "pdf",
    "item_status": "active",
}
FOLDER_INFO: Final[dict[str, Any]] = {
    "type": "folder",
    "id": "2001",
    "etag": "0",
    "name": "Project",
    "item_status": "active",
}
WEB_LINK_ENTRY: Final[dict[str, Any]] = {"type": "web_link", "id": "4001", "etag": "0", "name": "Wiki"}
FOLDER_ITEMS_PAGE: Final[dict[str, Any]] = {
    "limit": 1000,
    "next_marker": None,
    "order": [{"by": "type", "direction": "ASC"}, {"by": "name", "direction": "ASC"}],
    "entries": [
        {"type": "folder", "id": "2002", "etag": "0", "name": "Sub", "item_status": "active"},
        FILE_INFO,
        {
            "type": "file",
            "id": "3002",
            "etag": "2",
            "name": "Notes.boxnote",
            "size": 812,
            "extension": "boxnote",
            "item_status": "active",
        },
        {"type": "file", "id": "3003", "etag": "0", "name": "logo.png", "size": 90, "extension": "png"},
        WEB_LINK_ENTRY,
    ],
}
SHARED_ITEM_FOLDER: Final[dict[str, Any]] = {
    **FOLDER_INFO,
    "shared_link": {"url": SHARED_LINK_CANARY, "access": "open", "is_password_enabled": False},
}


def box_env() -> dict[str, str]:
    return {
        "BOX_OAUTH_CLIENT_ID": CLIENT_ID,
        "BOX_OAUTH_CLIENT_SECRET": CLIENT_CANARY,
        "BOX_TOKEN_KEY": TOKEN_KEY,
        "PUBLIC_URL": "http://localhost:3001",
    }


def box_settings() -> BoxSettings:
    return BoxSettings(**box_env())


def user_of(shape: dict[str, Any] = USERS_ME_ENTERPRISE) -> BoxUser:
    enterprise = shape["enterprise"] or {}
    return BoxUser(shape["id"], shape["name"], enterprise.get("id"), enterprise.get("name"))


class FakeBox:
    """A scripted Box. Tokens are ``access-N`` and ``refresh-N``; a refresh rotates the refresh token (the old one
    dies, as on Box). ``items`` maps a folder id to its entries (``next_marker`` paging by ``marker`` = an offset),
    ``folders`` and ``files`` a folder or file id to its info, ``content`` a file id to its bytes, ``shared`` a shared
    link URL to the item it names. ``throttle`` makes the next N REST calls answer 429 with ``Retry-After: 1``."""

    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {CODE_CANARY: USERS_ME_ENTERPRISE}
        self.access: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = {"2001": copy.deepcopy(FOLDER_ITEMS_PAGE["entries"])}
        self.folders: dict[str, dict[str, Any]] = {"2001": FOLDER_INFO}
        self.files: dict[str, dict[str, Any]] = {"3001": FILE_INFO}
        self.content: dict[str, bytes] = {"3001": b"%PDF-1.7 fake"}
        self.representations: dict[str, str] = {"3002": "text of the note"}
        self.shared: dict[str, dict[str, Any]] = {SHARED_LINK_CANARY: SHARED_ITEM_FOLDER}
        self.page_size = 1000
        self.calls: list[tuple[str, str]] = []
        self.violations: list[tuple[str, str]] = []  # any request that is not a GET (or an OAuth POST)
        self.shared_headers: list[str] = []  # the ``boxapi`` header of each REST call that had one
        self.grant_error: dict[str, Any] | None = None  # the next token call answers 400 with it
        self.status: int | None = None  # every call answers this status (503: Box down)
        self.throttle = 0
        self.not_ready = 0  # the next N downloads answer 202 with Retry-After
        self.revoked: list[str] = []
        self.refreshes = 0
        self.expires_in = 3599
        self.gate: asyncio.Event | None = None  # a refresh waits for it
        self.entered = asyncio.Event()  # set when a refresh reaches the gate
        self._serial = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> BoxClient:
        async def no_sleep(_seconds: float) -> None:
            return None

        return BoxClient(CLIENT_ID, box_settings().client_secret, CALLBACK, transport=self.transport(), sleep=no_sleep)

    def _issue(self, user: dict[str, Any]) -> dict[str, Any]:
        self._serial += 1
        access, refresh = f"access-{self._serial}", f"refresh-{self._serial}"
        self.access[access], self.refresh[refresh] = user, user
        return {**TOKEN_RESPONSE, "access_token": access, "refresh_token": refresh, "expires_in": self.expires_in}

    def expire_access(self) -> None:
        """Box ends every access token early (a 401 next)."""

        self.access.clear()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        post = request.method == "POST" and request.url.host == "api.box.com"
        if request.method != "GET" and not (post and request.url.path in ("/oauth2/token", "/oauth2/revoke")):
            self.violations.append((request.method, request.url.path))
            return httpx.Response(405)
        if self.status is not None:
            return httpx.Response(self.status)
        if request.url.host == "dl.boxcloud.com":
            return self._download(request)
        if request.url.host != "api.box.com":
            return httpx.Response(404)
        if request.url.path == "/oauth2/token":
            return await self._token(request)
        return self._api(request)

    def _api(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/revoke":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.revoked.append(form.get("token", ""))
            return httpx.Response(200)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        user = self.access.get(token)
        if user is None:
            return httpx.Response(401, json={"code": "unauthorized", "message": "Unauthorized"})
        if "boxapi" in request.headers:
            self.shared_headers.append(request.headers["boxapi"])
        if self.throttle > 0:
            self.throttle -= 1
            return httpx.Response(429, json=RATE_LIMITED, headers={"Retry-After": "1"})
        return self._rest(request, user)

    async def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_CANARY:
            return httpx.Response(400, json={"error": "invalid_client", "error_description": "text canary from box"})
        if self.grant_error is not None:
            error, self.grant_error = self.grant_error, None
            return httpx.Response(400, json=error)
        if form.get("grant_type") == "refresh_token":
            if self.gate is not None:
                self.entered.set()
                await self.gate.wait()
            self.refreshes += 1
            user = self.refresh.pop(form.get("refresh_token", ""), None)
            if user is None:
                return httpx.Response(400, json=INVALID_GRANT)
            return httpx.Response(200, json=self._issue(user))
        user = self.codes.pop(form.get("code", ""), None)
        if user is None:
            return httpx.Response(400, json=INVALID_GRANT)
        return httpx.Response(200, json=self._issue(user))

    def _download(self, request: httpx.Request) -> httpx.Response:
        file_id = request.url.path.rsplit("/", 1)[1]
        if "Authorization" in request.headers:  # the download host gets a signed URL, never the token
            self.violations.append(("TOKEN-TO-DOWNLOAD-HOST", request.url.path))
        return httpx.Response(200, content=self.content[file_id])

    def _item(self, kind: str, item_id: str) -> dict[str, Any] | None:
        table = self.folders if kind == "folders" else self.files
        return table.get(item_id)

    def _rest(self, request: httpx.Request, user: dict[str, Any]) -> httpx.Response:
        parts = request.url.path.removeprefix("/2.0/").split("/")
        if parts == ["users", "me"]:
            return httpx.Response(200, json=user)
        if parts == ["shared_items"]:
            link = request.headers.get("boxapi", "").removeprefix("shared_link=")
            found = self.shared.get(link)
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"code": "not_found"})
        if len(parts) == 3 and parts[0] == "folders" and parts[2] == "items":
            return self._items_page(parts[1], request)
        if len(parts) == 3 and parts[0] == "files" and parts[2] == "content":
            return self._file_content(parts[1])
        if len(parts) == 2 and parts[0] in ("folders", "files"):
            found = self._item(parts[0], parts[1])
            return httpx.Response(200, json=found) if found else httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(404)

    def _items_page(self, folder_id: str, request: httpx.Request) -> httpx.Response:
        entries = self.items.get(folder_id)
        if entries is None:
            return httpx.Response(404, json={"code": "not_found"})
        start = int(request.url.params.get("marker", "0"))
        chunk = entries[start : start + self.page_size]
        more = start + self.page_size < len(entries)
        return httpx.Response(
            200,
            json={**FOLDER_ITEMS_PAGE, "entries": chunk, "next_marker": str(start + self.page_size) if more else None},
        )

    def _file_content(self, file_id: str) -> httpx.Response:
        if file_id not in self.content:
            return httpx.Response(404, json={"code": "not_found"})
        if self.not_ready > 0:
            self.not_ready -= 1
            return httpx.Response(202, headers={"Retry-After": "1"})
        return httpx.Response(302, headers={"Location": f"https://dl.boxcloud.com/d/1/signed/{file_id}"})


class MemoryStore(MemoryConnectionStore):
    """The ``ConnectionStore`` over a dict (``connection_store_support``)."""

    async def save(
        self,
        user_id: uuid.UUID,
        user: BoxUser,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime,
        now: datetime,
    ) -> None:
        self.rows[user_id] = StoredConnection(
            user_id, user.id, user.name, user.enterprise_id, user.enterprise_name, refresh_token, "active", now, now
        )
        self.refresh_expires[user_id] = refresh_expires_at
        self.events.append(("save", user_id))


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
