"""Shared fakes of the Dropbox integration tests (no tests in here): a scripted Dropbox behind ``httpx.MockTransport``
(``api.dropboxapi.com`` OAuth and RPC, ``content.dropboxapi.com`` downloads and exports), JSON fixtures in the shapes
recorded live on 2026-11-25 (plans/DROPBOX/research/explore-findings.md) or, where the sample had none, in the shapes
the reference documents (plans/DROPBOX/research/dropbox-docs.md), an in-memory ``ConnectionStore`` and the settings.
Every stream (connection, importer) uses this module. Every value is a fake; the canaries must never reach a log line,
an error or a response.

The fake answers only the allowlisted endpoints (decision DB3) and records any other request in ``violations``
(method, host, path); ``dropbox.client`` refuses such a call before sending, so the tests assert ``violations == []``.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import parse_qs

import httpx
from cryptography.fernet import Fernet

from connection_store_support import MemoryConnectionStore
from dropbox.client import DropboxAccount, DropboxClient
from dropbox.connections import StoredConnection
from settings import DropboxSettings

CLIENT_ID = "dropbox-app-key-1"
CLIENT_CANARY = "dropbox-app-secret-canary-5e3"
TOKEN_KEY = Fernet.generate_key().decode()
CODE_CANARY = "dropbox-code-canary-8a1"
SHARED_LINK_CANARY = "https://www.dropbox.com/scl/fo/sharedcanary7b4/AAAkeycanary?rlkey=rlkeycanary&st=st1&dl=0"
FILE_LINK_CANARY = "https://www.dropbox.com/scl/fi/filecanary2c9/Spec.pdf?rlkey=rlkeyfilecanary&dl=0"
FOREIGN_LINK_CANARY = "https://www.dropbox.com/scl/fo/foreigncanary1d0/AAAforeign?rlkey=rlkeyforeign&dl=0"
NAME_CANARY = "Name Canary"
EMAIL_CANARY = "email-canary@example.test"
PATH_CANARY = "/graphico lab"
ACCOUNT_ID = "dbid:AAH4f99T0taONIb-OurWxbNQ6ywGRopQngc"
TEAM_ID = "dbtid:AAA0aBcDeFgHiJkLmNoPqRsTuVwXyZ0123"
ROOT_NAMESPACE = "3235641"
TEAM_ROOT_NAMESPACE = "8102936"
CALLBACK = "http://localhost:3001/api/integrations/dropbox/callback"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
API_HOST = "api.dropboxapi.com"
CONTENT_HOST = "content.dropboxapi.com"
ALLOWED: Final = frozenset(
    {
        (API_HOST, "/oauth2/token"),
        (API_HOST, "/2/auth/token/revoke"),
        (API_HOST, "/2/users/get_current_account"),
        (API_HOST, "/2/files/get_metadata"),
        (API_HOST, "/2/files/list_folder"),
        (API_HOST, "/2/files/list_folder/continue"),
        (API_HOST, "/2/sharing/get_shared_link_metadata"),
        (CONTENT_HOST, "/2/files/download"),
        (CONTENT_HOST, "/2/files/export"),
    }
)

# Shapes ----------------------------------------------------------------------------------------------------------
# Field names and nesting as recorded live (account, shared link, listing, download) or as the reference documents them
# (team account, 401, 409, 422, 429). Values are invented.

TOKEN_RESPONSE: Final[dict[str, Any]] = {
    "access_token": "sl.access-1",
    "expires_in": 14400,
    "token_type": "bearer",
    "scope": "account_info.read files.content.read files.metadata.read sharing.read",
    "refresh_token": "refresh-1",
    "account_id": ACCOUNT_ID,
    "uid": "12345",
}
REFRESH_RESPONSE: Final[dict[str, Any]] = {"access_token": "sl.access-2", "expires_in": 14400, "token_type": "bearer"}
INVALID_GRANT: Final[dict[str, Any]] = {"error": "invalid_grant", "error_description": "refresh token is invalid"}
NAME: Final[dict[str, Any]] = {
    "given_name": "Name",
    "surname": "Canary",
    "familiar_name": "Name",
    "display_name": NAME_CANARY,
    "abbreviated_name": "NC",
}
ACCOUNT_PERSONAL: Final[dict[str, Any]] = {  # recorded: account_type basic, root_info user, no team
    "account_id": ACCOUNT_ID,
    "name": NAME,
    "email": EMAIL_CANARY,
    "email_verified": True,
    "disabled": False,
    "country": "PL",
    "locale": "en",
    "referral_link": "https://www.dropbox.com/referrals/canary",
    "is_paired": False,
    "account_type": {".tag": "basic"},
    "root_info": {".tag": "user", "root_namespace_id": ROOT_NAMESPACE, "home_namespace_id": ROOT_NAMESPACE},
}
ACCOUNT_TEAM: Final[dict[str, Any]] = {  # documented (users/get_current_account): a Business member of a team space
    **ACCOUNT_PERSONAL,
    "account_id": "dbid:AACteammember0000000000000000000000",
    "account_type": {".tag": "business"},
    "team": {
        "id": TEAM_ID,
        "name": "Acme Team",
        "sharing_policies": {"shared_folder_member_policy": {".tag": "team"}},
        "office_addin_policy": {".tag": "disabled"},
    },
    "team_member_id": "dbmid:AAAteammember",
    "root_info": {
        ".tag": "team",
        "root_namespace_id": TEAM_ROOT_NAMESPACE,
        "home_namespace_id": "8102937",
        "home_path": "/Name Canary",
    },
}
FOLDER: Final[dict[str, Any]] = {
    ".tag": "folder",
    "name": "Graphico Lab",
    "path_lower": PATH_CANARY,
    "path_display": "/Graphico Lab",
    "id": "id:a4ayc_80_OEAAAAAAAAAXw",
}
SUBFOLDER: Final[dict[str, Any]] = {
    ".tag": "folder",
    "name": "Sub",
    "path_lower": f"{PATH_CANARY}/sub",
    "path_display": "/Graphico Lab/Sub",
    "id": "id:a4ayc_80_OEAAAAAAAAAYw",
}


def file_entry(
    file_id: str, name: str, size: int = 4096, *, folder: str = "/Graphico Lab", **extra: Any
) -> dict[str, Any]:
    """A ``files/list_folder`` file entry (recorded shape: id, rev, content_hash, server_modified, is_downloadable)."""

    return {
        ".tag": "file",
        "name": name,
        "path_lower": f"{folder}/{name}".lower(),
        "path_display": f"{folder}/{name}",
        "id": file_id,
        "client_modified": "2026-11-20T10:00:00Z",
        "server_modified": "2026-11-20T10:00:01Z",
        "rev": "015f0a1b2c3d4e5f00000001",
        "size": size,
        "is_downloadable": True,
        "content_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        **extra,
    }


PDF_ENTRY: Final = file_entry("id:a4ayc_80_OEAAAAAAAAAPd", "Spec.pdf", 592_000)
HTML_ENTRY: Final = file_entry("id:a4ayc_80_OEAAAAAAAAAHt", "Page.html", 89_000)
PNG_ENTRY: Final = file_entry("id:a4ayc_80_OEAAAAAAAAAPn", "logo.png", 332_000)
NESTED_ENTRY: Final = file_entry("id:a4ayc_80_OEAAAAAAAAANs", "Notes.md", 120, folder="/Graphico Lab/Sub")
# A Paper doc as a file (``paper_as_files``): not downloadable, exportable. The export format list is a PLACEHOLDER:
# the sample had no Paper doc. Task L0 replaces ``PAPER_EXPORT_INFO`` and ``PAPER_EXPORT`` with a live recording;
# until then the importer treats Paper as skipped (DB10) and no test may assert on these values.
PAPER_EXPORT_INFO: Final[dict[str, Any] | None] = None  # L0: the recorded ``export_info`` of a ``.paper`` entry
# L0: {"result": <the Dropbox-API-Result>, "content_type": ..., "body": <the first bytes>}
PAPER_EXPORT: Final[dict[str, Any] | None] = None
PAPER_ENTRY: Final = file_entry(
    "id:a4ayc_80_OEAAAAAAAAAPp",
    "Plan.paper",
    1_024,
    is_downloadable=False,
    **({"export_info": PAPER_EXPORT_INFO} if PAPER_EXPORT_INFO is not None else {}),
)
SHARED_FOLDER_META: Final[dict[str, Any]] = {  # recorded: the owner's own folder link, ``path_lower`` present
    ".tag": "folder",
    "url": SHARED_LINK_CANARY,
    "id": FOLDER["id"],
    "name": "Graphico Lab",
    "path_lower": PATH_CANARY,
    "link_permissions": {"can_revoke": True, "resolved_visibility": {".tag": "public"}},
}
SHARED_FILE_META: Final[dict[str, Any]] = {
    **{k: v for k, v in PDF_ENTRY.items() if k not in ("is_downloadable", "content_hash", "path_display")},
    "url": FILE_LINK_CANARY,
    "link_permissions": {"can_revoke": True, "resolved_visibility": {".tag": "public"}},
}
SHARED_FOREIGN_META: Final[dict[str, Any]] = {  # documented: a link of someone else's folder has no ``path_lower``
    ".tag": "folder",
    "url": FOREIGN_LINK_CANARY,
    "id": "id:foreign0000000000000000",
    "name": "Their Folder",
    "link_permissions": {"can_revoke": False, "resolved_visibility": {".tag": "public"}},
}


def error_body(summary: str, error: dict[str, Any]) -> dict[str, Any]:
    return {"error_summary": summary, "error": error}


EXPIRED_TOKEN: Final = error_body("expired_access_token/..", {".tag": "expired_access_token"})  # recorded shape
PATH_NOT_FOUND: Final = error_body("path/not_found/..", {".tag": "path", "path": {".tag": "not_found"}})
RESTRICTED: Final = error_body("path/restricted_content/..", {".tag": "path", "path": {".tag": "restricted_content"}})
LINK_NOT_FOUND: Final = error_body("shared_link_not_found/..", {".tag": "shared_link_not_found"})
LINK_DENIED: Final = error_body("shared_link_access_denied/..", {".tag": "shared_link_access_denied"})
UNSUPPORTED_FILE: Final = error_body("unsupported_file/..", {".tag": "unsupported_file"})
INVALID_ROOT: Final = error_body(
    "invalid_root/..",
    {
        ".tag": "invalid_root",
        "invalid_root": {".tag": "team", "root_namespace_id": TEAM_ROOT_NAMESPACE, "home_namespace_id": "8102937"},
    },
)
TOO_MANY_REQUESTS: Final = error_body(
    "too_many_requests/..", {"reason": {".tag": "too_many_requests"}, "retry_after": 1}
)


def dropbox_env() -> dict[str, str]:
    return {
        "DROPBOX_OAUTH_CLIENT_ID": CLIENT_ID,
        "DROPBOX_OAUTH_CLIENT_SECRET": CLIENT_CANARY,
        "DROPBOX_TOKEN_KEY": TOKEN_KEY,
        "PUBLIC_URL": "http://localhost:3001",
    }


def dropbox_settings() -> DropboxSettings:
    return DropboxSettings(**dropbox_env())


def account_of(shape: dict[str, Any] = ACCOUNT_PERSONAL) -> DropboxAccount:
    team = shape.get("team") or {}
    return DropboxAccount(
        shape["account_id"],
        shape["name"]["display_name"],
        team.get("id"),
        team.get("name"),
        shape["root_info"]["root_namespace_id"],
    )


class FakeDropbox:
    """A scripted Dropbox. Codes give ``sl.access-N`` + ``refresh-N``; a refresh gives a new access token and keeps the
    refresh token (no rotation, as on Dropbox). ``shared`` maps a shared link URL to its metadata, ``metadata`` a path
    (lower case) or an ``id:`` to its metadata, ``trees`` a folder ``path_lower`` to the entries of a recursive listing
    (paged by ``page_size`` with ``continue``), ``content`` an ``id:`` to its bytes, ``exports`` an ``id:`` to its
    export (only from a live recording, task L0).

    Knobs: ``throttle`` (the next N calls answer 429; ``retry_after`` is the header value, ``None`` for none),
    ``status`` (every call answers it), ``invalid_root`` (the next N calls that send a Path-Root answer 422),
    ``grant_error`` (the next token call answers 400 with it), ``not_found``/``restricted`` (ids or paths answered 409).
    Records: ``calls`` (host, path), ``violations``, ``path_roots`` (the Path-Root header of each call that had one),
    ``api_args`` (each raw ``Dropbox-API-Arg``), ``bodies`` (each RPC body), ``revoked`` (each revoked token)."""

    def __init__(self) -> None:
        self.codes: dict[str, dict[str, Any]] = {CODE_CANARY: ACCOUNT_PERSONAL}
        self.access: dict[str, dict[str, Any]] = {}
        self.refresh: dict[str, dict[str, Any]] = {}
        self.shared: dict[str, dict[str, Any]] = {
            SHARED_LINK_CANARY: SHARED_FOLDER_META,
            FILE_LINK_CANARY: SHARED_FILE_META,
            FOREIGN_LINK_CANARY: SHARED_FOREIGN_META,
        }
        self.metadata: dict[str, dict[str, Any]] = {
            PATH_CANARY: FOLDER,
            FOLDER["id"]: FOLDER,
            PDF_ENTRY["path_lower"]: PDF_ENTRY,
            PDF_ENTRY["id"]: PDF_ENTRY,
        }
        self.trees: dict[str, list[dict[str, Any]]] = {
            PATH_CANARY: copy.deepcopy([FOLDER, SUBFOLDER, PDF_ENTRY, HTML_ENTRY, PNG_ENTRY, NESTED_ENTRY])
        }
        self.content: dict[str, bytes] = {
            PDF_ENTRY["id"]: b"%PDF-1.7 fake",
            HTML_ENTRY["id"]: b"<html><body>fake</body></html>",
            NESTED_ENTRY["id"]: b"# notes",
        }
        self.exports: dict[str, dict[str, Any]] = {}  # L0 fills it from a live recording (``PAPER_EXPORT``)
        self.not_found: set[str] = set()
        self.restricted: set[str] = set()
        self.page_size = 2000
        self.calls: list[tuple[str, str]] = []
        self.violations: list[tuple[str, str, str]] = []
        self.path_roots: list[str] = []
        self.api_args: list[str] = []
        self.bodies: list[tuple[str, Any]] = []
        self.revoked: list[str] = []
        self.grant_error: dict[str, Any] | None = None
        self.status: int | None = None
        self.throttle = 0
        self.retry_after: str | None = "1"
        self.invalid_root = 0
        self.refreshes = 0
        self.expires_in = 14400
        self._serial = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self) -> DropboxClient:
        async def no_sleep(_seconds: float) -> None:
            return None

        return DropboxClient(
            CLIENT_ID, dropbox_settings().client_secret, CALLBACK, transport=self.transport(), sleep=no_sleep
        )

    def expire_access(self) -> None:
        """Dropbox ends every access token early (a 401 ``expired_access_token`` next)."""

        self.access.clear()

    def _issue(self, account: dict[str, Any], *, with_refresh: bool) -> dict[str, Any]:
        self._serial += 1
        access = f"sl.access-{self._serial}"
        self.access[access] = account
        if not with_refresh:
            return {**REFRESH_RESPONSE, "access_token": access, "expires_in": self.expires_in}
        refresh = f"refresh-{self._serial}"
        self.refresh[refresh] = account
        answer = {**TOKEN_RESPONSE, "access_token": access, "refresh_token": refresh, "expires_in": self.expires_in}
        return {**answer, "account_id": account["account_id"]}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        self.calls.append((host, path))
        if request.method != "POST" or (host, path) not in ALLOWED:
            self.violations.append((request.method, host, path))
            return httpx.Response(405)
        if self.status is not None:
            return httpx.Response(self.status)
        if path == "/oauth2/token":
            return self._token(request)
        return self._as_user(request)

    def _as_user(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        account = self.access.get(token)
        if account is None:
            return httpx.Response(401, json=EXPIRED_TOKEN)
        if request.url.path == "/2/auth/token/revoke":
            self.revoked.append(token)
            self.access.pop(token, None)
            return httpx.Response(200, json=None)
        refused = self._paced(request)
        if refused is not None:
            return refused
        if request.url.host == CONTENT_HOST:
            return self._content(request)
        return self._rpc(request.url.path, json.loads(request.content) if request.content else None, account)

    def _paced(self, request: httpx.Request) -> httpx.Response | None:
        """A 429 while ``throttle`` lasts; a 422 ``invalid_root`` while ``invalid_root`` lasts (Path-Root calls)."""

        if self.throttle > 0:
            self.throttle -= 1
            headers = {} if self.retry_after is None else {"Retry-After": self.retry_after}
            return httpx.Response(429, json=TOO_MANY_REQUESTS, headers=headers)
        root = request.headers.get("Dropbox-API-Path-Root")
        if root is not None:
            self.path_roots.append(root)
            if self.invalid_root > 0:
                self.invalid_root -= 1
                return httpx.Response(422, json=INVALID_ROOT)
        return None

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_CANARY:
            return httpx.Response(400, json={"error": "invalid_client", "error_description": "text canary from dbx"})
        if self.grant_error is not None:
            error, self.grant_error = self.grant_error, None
            return httpx.Response(400, json=error)
        refreshing = form.get("grant_type") == "refresh_token"
        if refreshing:
            self.refreshes += 1
            account = self.refresh.get(form.get("refresh_token", ""))  # not popped: Dropbox refresh tokens stay
        elif form.get("redirect_uri") != CALLBACK:  # the exchange must name the authorize redirect URI
            account = None
        else:
            account = self.codes.pop(form.get("code", ""), None)
        if account is None:
            return httpx.Response(400, json=INVALID_GRANT)
        return httpx.Response(200, json=self._issue(account, with_refresh=not refreshing))

    def _refused(self, key: str) -> httpx.Response | None:
        if key in self.not_found:
            return httpx.Response(409, json=PATH_NOT_FOUND)
        if key in self.restricted:
            return httpx.Response(409, json=RESTRICTED)
        return None

    def _rpc(self, path: str, body: Any, account: dict[str, Any]) -> httpx.Response:
        self.bodies.append((path, body))
        args = body if isinstance(body, dict) else {}
        if path == "/2/users/get_current_account":
            return httpx.Response(200, json=account)
        if path == "/2/sharing/get_shared_link_metadata":
            found = self.shared.get(args.get("url", ""))
            return httpx.Response(200, json=found) if found else httpx.Response(409, json=LINK_NOT_FOUND)
        if path == "/2/files/get_metadata":
            return self._metadata(str(args.get("path", "")))
        if path == "/2/files/list_folder":
            return self._list(str(args.get("path", "")).lower())
        _, key, offset = str(args.get("cursor", "")).split("|")  # files/list_folder/continue
        return self._page(key, int(offset))

    def _metadata(self, path: str) -> httpx.Response:
        key = path if path.startswith("id:") else path.lower()
        found = self.metadata.get(key)
        refused = self._refused(key) or (None if found else httpx.Response(409, json=PATH_NOT_FOUND))
        return refused or httpx.Response(200, json=found)

    def _list(self, key: str) -> httpx.Response:
        missing = None if key in self.trees else httpx.Response(409, json=PATH_NOT_FOUND)
        return self._refused(key) or missing or self._page(key, 0)

    def _page(self, key: str, offset: int) -> httpx.Response:
        entries = self.trees[key][offset : offset + self.page_size]
        more = offset + self.page_size < len(self.trees[key])
        cursor = f"cursor|{key}|{offset + self.page_size}"
        return httpx.Response(200, json={"entries": entries, "cursor": cursor, "has_more": more})

    def _content(self, request: httpx.Request) -> httpx.Response:
        raw = request.headers.get("Dropbox-API-Arg", "{}")
        self.api_args.append(raw)
        file_id = str(json.loads(raw).get("path", ""))
        refused = self._refused(file_id)
        if refused is not None:
            return refused
        if request.url.path == "/2/files/export":
            return self._export(file_id)
        if file_id not in self.content:
            entry = self._entry(file_id)
            not_downloadable = entry is not None and entry.get("is_downloadable") is False
            return httpx.Response(409, json=UNSUPPORTED_FILE if not_downloadable else PATH_NOT_FOUND)
        entry = self._entry(file_id) or {"id": file_id}
        headers = {
            "Dropbox-API-Result": json.dumps({k: v for k, v in entry.items() if k != ".tag"}, ensure_ascii=True),
            "Content-Type": "application/octet-stream",  # recorded: always octet-stream
        }
        return httpx.Response(200, content=self.content[file_id], headers=headers)

    def _export(self, file_id: str) -> httpx.Response:
        export = self.exports.get(file_id)
        if export is None:
            return httpx.Response(409, json=error_body("non_exportable/..", {".tag": "non_exportable"}))
        headers = {"Dropbox-API-Result": json.dumps(export["result"]), "Content-Type": export["content_type"]}
        return httpx.Response(200, content=export["body"], headers=headers)

    def _entry(self, file_id: str) -> dict[str, Any] | None:
        return next((e for e in self._all_entries() if e.get("id") == file_id), None)

    def _all_entries(self) -> list[dict[str, Any]]:
        return [entry for entries in self.trees.values() for entry in entries]


class MemoryStore(MemoryConnectionStore):
    """The ``ConnectionStore`` over a dict (``connection_store_support``); no ``lock``/``rotate`` is used (DB4)."""

    async def save(self, user_id: uuid.UUID, account: DropboxAccount, *, refresh_token: bytes, now: datetime) -> None:
        self.rows[user_id] = StoredConnection(
            user_id,
            account.account_id,
            account.name,
            account.team_id,
            account.team_name,
            refresh_token,
            "active",
            now,
            now,
        )
        self.events.append(("save", user_id))

    async def touch(self, user_id: uuid.UUID, *, now: datetime) -> None:
        self.rows[user_id] = replace(self.rows[user_id], last_used_at=now)
        self.events.append(("touch", user_id))


def json_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True)
