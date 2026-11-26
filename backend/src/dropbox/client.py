"""The one transport to Dropbox and the OAuth calls (docs/dropbox/oauth.md): ``www.dropbox.com`` (the consent page, in
the browser only), ``api.dropboxapi.com`` (token, revoke, RPC calls as the user) and ``content.dropboxapi.com`` (file
bytes).

Read-only by construction (decision DB3): every Dropbox API call is a ``POST``, so the guard is an endpoint
ALLOWLIST in ``_build``, the one place a request is made: the token URL, ``auth/token/revoke``, the five read RPC
endpoints on the api host and ``files/download``/``files/export`` on the content host. Any other URL, host or method
raises ``RuntimeError`` before anything is sent (``tests/test_dropbox_client.py``). The app's scopes are read scopes
too (``SCOPE``, sent on every authorize so a scope added in the App Console never widens a grant).

Every refusal is one closed ``DropboxCode``; no token, code, ``state``, link, path, name or Dropbox text goes into a
log line, an error or a ``repr``. The client ignores the proxy variables and ``.netrc`` (``trust_env=False``) and
never follows a redirect. ``dropbox.content`` (the content reads) and ``dropbox.connections`` (the per-user tokens)
build on it and add no HTTP of their own.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, override
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from dropbox.codes import DropboxCode, DropboxError
from integrations.oauth_client import OAuthHttpClient
from integrations.pacing import BACKOFF_SECONDS, WAIT_BUDGET_SECONDS, WaitBudget, backoff

AUTHORIZE_URL: Final = "https://www.dropbox.com/oauth2/authorize"
GRANT_URL: Final = "https://api.dropboxapi.com/oauth2/token"
API_URL: Final = "https://api.dropboxapi.com/2"
CONTENT_URL: Final = "https://content.dropboxapi.com/2"
# The four read scopes of the app (decision DB2), sent as ``scope=`` on every authorize.
SCOPE: Final = "account_info.read files.metadata.read files.content.read sharing.read"
MANAGE_URL: Final = "https://www.dropbox.com/account/connected_apps"  # where a user lists the apps of the account
TIMEOUT_SECONDS: Final = 30
REFRESH_TIMEOUT_SECONDS: Final = 10
MAX_ATTEMPTS: Final = 4  # tries of one call: the first and up to three after a 429 or a 5xx
GRANT_DEAD: Final = "invalid_grant"  # the ``error`` of a used, expired, revoked or unknown code or refresh token
DOWNLOAD_CHUNK: Final = 1024 * 1024  # bytes per chunk of a download stream

# The allowlist (decision DB3): every endpoint this code may call, by host.
REVOKE: Final = "auth/token/revoke"
GET_CURRENT_ACCOUNT: Final = "users/get_current_account"
GET_METADATA: Final = "files/get_metadata"
LIST_FOLDER: Final = "files/list_folder"
LIST_FOLDER_CONTINUE: Final = "files/list_folder/continue"
GET_SHARED_LINK_METADATA: Final = "sharing/get_shared_link_metadata"
DOWNLOAD: Final = "files/download"
EXPORT: Final = "files/export"
RPC_ENDPOINTS: Final = frozenset(
    {REVOKE, GET_CURRENT_ACCOUNT, GET_METADATA, LIST_FOLDER, LIST_FOLDER_CONTINUE, GET_SHARED_LINK_METADATA}
)
CONTENT_ENDPOINTS: Final = frozenset({DOWNLOAD, EXPORT})
ALLOWED_URLS: Final = frozenset(
    {GRANT_URL}
    | {f"{API_URL}/{endpoint}" for endpoint in RPC_ENDPOINTS}
    | {f"{CONTENT_URL}/{endpoint}" for endpoint in CONTENT_ENDPOINTS}
)

_REFRESHABLE_401: Final = frozenset({"expired_access_token", "invalid_access_token"})
_TAG: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_NOT_FOUND_TAGS: Final = frozenset({"not_found", "shared_link_not_found"})
_RESTRICTED_TAGS: Final = frozenset(
    {"restricted_content", "no_permission", "shared_link_access_denied", "access_denied", "email_not_verified"}
)
_UNSUPPORTED_TAGS: Final = frozenset(
    {"unsupported_file", "non_exportable", "invalid_export_format", "unsupported_link_type", "unsupported_extension"}
)

__all__ = [
    "ALLOWED_URLS",
    "BACKOFF_SECONDS",
    "WAIT_BUDGET_SECONDS",
    "DropboxAccount",
    "DropboxApiError",
    "DropboxClient",
    "Opened",
    "TokenGrant",
    "WaitBudget",
    "api_arg",
]


@dataclass(frozen=True)
class TokenGrant:
    """What the token endpoint answered; the tokens stay ``SecretStr``. ``refresh_token`` is set by the code exchange
    (``token_access_type=offline``) and ``None`` after a refresh: Dropbox refresh tokens do not rotate (DB4)."""

    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_in: int


@dataclass(frozen=True)
class DropboxAccount:
    """The account of a token (``users/get_current_account``); kept out of every log. ``team_id`` and ``team_name``
    are ``None`` for a personal account; ``root_namespace_id`` is the namespace of the ``Dropbox-API-Path-Root``
    header (DB6, read per check and per import, never stored). The e-mail address is never read."""

    account_id: str
    name: str
    team_id: str | None
    team_name: str | None
    root_namespace_id: str | None

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}(...)"


class DropboxApiError(DropboxError):
    """A refused call with the tags of Dropbox's ``error_summary`` (``path/not_found/..`` -> ``("path",
    "not_found")``): closed identifiers only, never a path or a message. ``str(e)`` is still the code."""

    def __init__(self, code: DropboxCode, tags: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.tags = tags


def api_arg(arg: Mapping[str, Any]) -> str:
    """The ``Dropbox-API-Arg`` header value: compact JSON where every character outside printable ASCII (and 0x7F) is
    ``\\uXXXX``-escaped, as the Dropbox HTTP header rule asks (docs.dropboxapi.com, request-response formats)."""

    return json.dumps(arg, ensure_ascii=True, separators=(",", ":")).replace("\x7f", "\\u007f")


def path_root_header(root_namespace_id: str) -> str:
    """The ``Dropbox-API-Path-Root`` value of the member's root namespace (DB6)."""

    return json.dumps({".tag": "root", "root": root_namespace_id}, separators=(",", ":"))


def body_of(response: httpx.Response) -> dict[str, Any]:
    """The JSON object of a response; ``{}`` for anything else."""

    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _positive(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _tags(answer: Mapping[str, Any]) -> tuple[str, ...]:
    """The identifiers of an ``error_summary`` (``path/not_found/...``); anything that is not a tag is dropped."""

    summary = answer.get("error_summary")
    if not isinstance(summary, str):
        error = answer.get("error")
        tag = error.get(".tag") if isinstance(error, dict) else None
        return (tag,) if isinstance(tag, str) and _TAG.fullmatch(tag) else ()
    return tuple(part for part in summary.split("/") if _TAG.fullmatch(part))


def _conflict_code(tags: tuple[str, ...]) -> DropboxCode:
    """The closed code of a ``409`` by its tags."""

    found = set(tags)
    if found & _NOT_FOUND_TAGS:
        return DropboxCode.NOT_FOUND
    if found & _RESTRICTED_TAGS:
        return DropboxCode.RESTRICTED
    if found & _UNSUPPORTED_TAGS:
        return DropboxCode.UNSUPPORTED
    return DropboxCode.REFUSED


def _refusal(response: httpx.Response) -> DropboxApiError:
    """The closed code of an answer that is not a success, a 429 or a 5xx (docs/dropbox/codes.md "Inside only")."""

    tags = _tags(body_of(response))
    status = response.status_code
    if status == httpx.codes.UNAUTHORIZED:
        code = DropboxCode.ACCESS_REFUSED if set(tags) & _REFRESHABLE_401 else DropboxCode.AUTH_FAILED
    elif status == httpx.codes.UNPROCESSABLE_ENTITY and "invalid_root" in tags:
        code = DropboxCode.INVALID_ROOT
    elif status == httpx.codes.CONFLICT:
        code = _conflict_code(tags)
    elif status == httpx.codes.FORBIDDEN:
        code = DropboxCode.RESTRICTED
    else:
        code = DropboxCode.REFUSED
    return DropboxApiError(code, tags)


@dataclass(frozen=True)
class Opened:
    """An open content answer (``files/download``, ``files/export``): ``result`` is the ``Dropbox-API-Result`` JSON
    (the file's metadata; ``export_metadata`` for an export), the bytes come from ``chunks``."""

    result: dict[str, Any]
    response: httpx.Response

    async def chunks(self, size: int = DOWNLOAD_CHUNK) -> AsyncIterator[bytes]:
        """The body in chunks of at most ``size`` bytes; a broken connection is ``UNREACHABLE``."""

        try:
            async for chunk in self.response.aiter_bytes(size):
                yield chunk
        except httpx.HTTPError:
            raise DropboxError(DropboxCode.UNREACHABLE) from None


class DropboxClient(OAuthHttpClient):
    """The OAuth web flow, the refresh, the revoke and the one allowlisted transport of the integration."""

    def authorize_url(self, state: str) -> str:
        """Dropbox's consent page for the app: code flow, offline access (a refresh token), the four read scopes."""

        query = {
            "client_id": self._client_id,
            "response_type": "code",
            "token_access_type": "offline",
            "scope": SCOPE,
            "redirect_uri": self._redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    # The one place a request is built ------------------------------------------------------------------------------

    def _build(self, method: str, url: str, **kwargs: Any) -> httpx.Request:
        """The request, after the allowlist (DB3): ``POST`` to one of ``ALLOWED_URLS`` only."""

        if method != "POST" or url not in ALLOWED_URLS:
            raise RuntimeError("the Dropbox client calls only its allowlisted read endpoints")  # a programming error
        return self._http.build_request(method, url, **kwargs)

    async def _once(self, url: str, *, stream: bool = False, **kwargs: Any) -> httpx.Response:
        request = self._build("POST", url, **kwargs)
        try:
            return await self._http.send(request, stream=stream)
        except httpx.HTTPError:
            raise DropboxError(DropboxCode.UNREACHABLE) from None

    # OAuth ---------------------------------------------------------------------------------------------------------

    async def _grant(self, form: Mapping[str, str], seconds: float) -> TokenGrant:
        """A token call. Dropbox answers a refusal with 400 and ``{"error": ...}``: ``invalid_grant`` is
        ``GRANT_INVALID``; any other refusal (a wrong secret) is ``REFUSED``; a 429 or 5xx is ``UNREACHABLE``."""

        body = {"client_id": self._client_id, "client_secret": self._secret.get_secret_value(), **form}
        response = await self._once(GRANT_URL, data=body, timeout=seconds)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
            raise DropboxError(DropboxCode.UNREACHABLE)
        answer = body_of(response)
        if not response.is_success or "error" in answer:
            raise DropboxError(DropboxCode.GRANT_INVALID if answer.get("error") == GRANT_DEAD else DropboxCode.REFUSED)
        access = _text(answer.get("access_token"))
        if access is None:
            raise DropboxError(DropboxCode.REFUSED)
        refresh = _text(answer.get("refresh_token"))
        return TokenGrant(
            SecretStr(access), None if refresh is None else SecretStr(refresh), _positive(answer.get("expires_in"))
        )

    async def exchange(self, code: str) -> TokenGrant:
        """The grant of an authorization code; it must carry a refresh token (offline access). Any refusal is
        ``CODE_INVALID``."""

        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri}
        try:
            grant = await self._grant(form, TIMEOUT_SECONDS)
        except DropboxError as error:
            if error.code in (DropboxCode.GRANT_INVALID, DropboxCode.REFUSED):
                raise DropboxError(DropboxCode.CODE_INVALID) from None
            raise
        if grant.refresh_token is None:
            raise DropboxError(DropboxCode.CODE_INVALID)
        return grant

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        """A new access token; the refresh token stays the same (``TokenGrant.refresh_token`` is ``None``). A dead
        refresh token is ``GRANT_INVALID``."""

        form = {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()}
        grant = await self._grant(form, REFRESH_TIMEOUT_SECONDS)
        return TokenGrant(grant.access_token, None, grant.expires_in)

    async def revoke(self, token: SecretStr) -> bool:
        """Best effort: ``auth/token/revoke`` ends the access token, its refresh token and every other access token of
        it. True when Dropbox confirmed it; never raises."""

        try:
            response = await self._once(
                f"{API_URL}/{REVOKE}", headers=self._headers(token, None), timeout=REFRESH_TIMEOUT_SECONDS
            )
        except DropboxError:
            return False
        return response.status_code == httpx.codes.OK

    # Calls as the user (allowlisted) --------------------------------------------------------------------------------

    @staticmethod
    def _headers(token: SecretStr, path_root: str | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        if path_root is not None:
            headers["Dropbox-API-Path-Root"] = path_root_header(path_root)
        return headers

    async def _wait(self, response: httpx.Response, attempt: int, budget: WaitBudget) -> None:
        """Wait before the next try of a throttled or failed call, or end it ``UNREACHABLE``."""

        if attempt >= MAX_ATTEMPTS - 1:
            raise DropboxError(DropboxCode.UNREACHABLE)
        granted = budget.take(backoff(response, attempt))
        if granted is None:
            raise DropboxError(DropboxCode.UNREACHABLE)
        await self._sleep(granted)

    async def rpc(
        self,
        token: SecretStr,
        endpoint: str,
        body: Mapping[str, Any] | None = None,
        *,
        path_root: str | None = None,
        budget: WaitBudget | None = None,
    ) -> dict[str, Any]:
        """One RPC call on ``api.dropboxapi.com/2/<endpoint>`` (one of ``RPC_ENDPOINTS``) as the user; the JSON answer.
        ``path_root`` is the member's root namespace id (``Dropbox-API-Path-Root``, DB6). A ``429`` or 5xx waits
        (``Retry-After``, else 1, 2, 4 s; at most ``MAX_ATTEMPTS`` tries and what is left of ``budget``) and asks
        again, else ``UNREACHABLE``. Any other refusal is a ``DropboxApiError``: a refreshable ``401`` is
        ``ACCESS_REFUSED`` (the caller refreshes once), ``422 invalid_root`` is ``INVALID_ROOT``, a ``409`` is
        ``NOT_FOUND``, ``RESTRICTED``, ``UNSUPPORTED`` or ``REFUSED`` by its tags."""

        if endpoint not in RPC_ENDPOINTS or endpoint == REVOKE:
            raise RuntimeError("not an allowlisted Dropbox read endpoint")
        budget = budget or WaitBudget()
        url, headers = f"{API_URL}/{endpoint}", self._headers(token, path_root)
        for attempt in range(MAX_ATTEMPTS):
            response = await self._once(url, json=body, headers=headers)
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
                await self._wait(response, attempt, budget)
                continue
            if not response.is_success:
                raise _refusal(response)
            return body_of(response)
        raise DropboxError(DropboxCode.UNREACHABLE)  # unreachable: ``_wait`` raises on the last attempt

    @asynccontextmanager
    async def download(
        self,
        token: SecretStr,
        arg: Mapping[str, Any],
        *,
        path_root: str | None = None,
        budget: WaitBudget | None = None,
    ) -> AsyncIterator[Opened]:
        """``files/download`` with ``arg`` (``{"path": "id:..."}``) in the ``Dropbox-API-Arg`` header: the open answer,
        closed on exit. Retries and refusals as ``rpc``."""

        async with self._content(token, DOWNLOAD, arg, path_root, budget) as opened:
            yield opened

    @asynccontextmanager
    async def export(
        self,
        token: SecretStr,
        arg: Mapping[str, Any],
        *,
        path_root: str | None = None,
        budget: WaitBudget | None = None,
    ) -> AsyncIterator[Opened]:
        """``files/export`` with ``arg`` (``{"path": "id:...", "export_format": "markdown"}``): as ``download``."""

        async with self._content(token, EXPORT, arg, path_root, budget) as opened:
            yield opened

    @asynccontextmanager
    async def _content(
        self,
        token: SecretStr,
        endpoint: str,
        arg: Mapping[str, Any],
        path_root: str | None,
        budget: WaitBudget | None,
    ) -> AsyncIterator[Opened]:
        budget = budget or WaitBudget()
        url = f"{CONTENT_URL}/{endpoint}"
        headers = {**self._headers(token, path_root), "Dropbox-API-Arg": api_arg(arg)}
        for attempt in range(MAX_ATTEMPTS):
            response = await self._once(url, stream=True, headers=headers)
            if response.status_code == httpx.codes.OK:
                try:
                    result = json.loads(response.headers.get("dropbox-api-result", "{}"))
                except ValueError:
                    result = {}
                try:
                    yield Opened(result if isinstance(result, dict) else {}, response)
                finally:
                    await response.aclose()
                return
            await response.aread()
            await response.aclose()
            if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
                await self._wait(response, attempt, budget)
                continue
            raise _refusal(response)
        raise DropboxError(DropboxCode.UNREACHABLE)

    async def get_current_account(self, token: SecretStr) -> DropboxAccount:
        """The account of the token: id, display name, team (absent for a personal account) and the root namespace
        id. The e-mail address in the answer is never read."""

        answer = await self.rpc(token, GET_CURRENT_ACCOUNT)
        account_id = _text(answer.get("account_id"))
        names = answer.get("name")
        name = _text(names.get("display_name")) if isinstance(names, dict) else None
        if account_id is None or name is None:
            raise DropboxError(DropboxCode.REFUSED)
        team = answer.get("team")
        team = team if isinstance(team, dict) else {}
        team_id = _text(team.get("id"))
        root = answer.get("root_info")
        root = root if isinstance(root, dict) else {}
        return DropboxAccount(
            account_id=account_id,
            name=name,
            team_id=team_id,
            team_name=_text(team.get("name")) if team_id is not None else None,
            root_namespace_id=_text(root.get("root_namespace_id")),
        )
