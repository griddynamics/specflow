"""The one transport to Box and the OAuth calls (docs/box/oauth.md): ``account.box.com`` (sign-in page), ``api.box.com``
(token, revoke, REST as the user) and the download host Box redirects to.

The REST part is read-only by construction (decision B3): ``BoxClient.get`` and ``BoxClient.stream`` send ``GET`` only,
and ``_once`` refuses any other method except the two ``POST`` calls of OAuth (``/oauth2/token``, ``/oauth2/revoke``).
The app's token can write (Box needs ``root_readwrite`` to download); this code cannot. ``box.content`` (the content
reads) and ``box.connections`` (the per-user tokens) build on it and add no HTTP of their own.

Every refusal is one closed ``BoxCode``; no token, code, ``state``, link, name or Box text goes into a log line, an
error or a ``repr``. The client ignores the proxy variables and ``.netrc`` (``trust_env=False``) and never follows a
redirect by itself (``stream`` follows the one download redirect, to a Box host, without the token).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, override
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import SecretStr

from box.codes import BoxCode, BoxError
from integrations.oauth_client import OAuthHttpClient

AUTHORIZE_URL: Final = "https://account.box.com/api/oauth2/authorize"
GRANT_URL: Final = "https://api.box.com/oauth2/token"
REVOKE_URL: Final = "https://api.box.com/oauth2/revoke"
API_URL: Final = "https://api.box.com/2.0"
SCOPE: Final = "root_readwrite"  # the only scope: Box needs it to download file content (decision B2)
MANAGE_URL: Final = "https://app.box.com/account/security"  # where a user lists the apps that access the account
TIMEOUT_SECONDS: Final = 30
REFRESH_TIMEOUT_SECONDS: Final = 10  # shorter: a refresh holds the row lock and a use waits on it
REFRESH_VALID_SECONDS: Final = 60 * 24 * 3600  # a refresh token lives 60 days (or one use)
WAIT_BUDGET_SECONDS: Final = 60  # the most one pass (one ``WaitBudget``) waits for throttling and ``202`` in all
MAX_ATTEMPTS: Final = 4  # tries of one read: the first and up to three after a 429, 5xx or 202
BACKOFF_SECONDS: Final = (1.0, 2.0, 4.0)  # waited when Box sends no ``Retry-After``
GRANT_DEAD: Final = "invalid_grant"  # Box's ``error`` for a used, expired, revoked or unknown code or refresh token
_REDIRECTS: Final = frozenset({301, 302, 303, 307, 308})
_OAUTH_POSTS: Final = frozenset({GRANT_URL, REVOKE_URL})
_DOWNLOAD_HOST_SUFFIXES: Final = (".boxcloud.com", ".box.com")  # where a download redirect may point
CONTENT_HOST: Final = "dl.boxcloud.com"  # the one other host that gets the bearer token (a Box Note's text)
_CONTENT_PATH: Final = "/api/2.0/internal_files/"


@dataclass(frozen=True)
class TokenGrant:
    """What the token endpoint answered; the tokens stay ``SecretStr``. Box always sends ``expires_in`` (about 3600)."""

    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int


@dataclass(frozen=True)
class BoxUser:
    """The account of a token (``GET /users/me``); kept out of every log. ``enterprise_id`` and ``enterprise_name`` are
    ``None`` for a personal account. The ``login`` (an e-mail) is never read."""

    id: str
    name: str
    enterprise_id: str | None
    enterprise_name: str | None

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}(...)"


class WaitBudget:
    """The seconds a pass may still wait for ``Retry-After`` answers, in all: one throttled import must not hold its
    slot for long. A wait that does not fit is not made (the read ends ``BoxCode.UNREACHABLE`` or ``NOT_READY``)."""

    def __init__(self, seconds: float = WAIT_BUDGET_SECONDS) -> None:
        self.left = seconds

    def take(self, wanted: float) -> float | None:
        """``wanted`` seconds when they fit in what is left (and spends them), else ``None``."""

        if wanted > self.left:
            return None
        self.left -= wanted
        return wanted


Response = httpx.Response  # the type of an answer, for ``box.content`` (HTTP stays in this module)


async def chunks_of(response: Response, size: int) -> AsyncIterator[bytes]:
    """The body of an open response in chunks of at most ``size`` bytes; a broken connection is ``UNREACHABLE``."""

    try:
        async for chunk in response.aiter_bytes(size):
            yield chunk
    except httpx.HTTPError:
        raise BoxError(BoxCode.UNREACHABLE) from None


def _positive(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def body_of(response: httpx.Response) -> dict[str, Any]:
    """The JSON object of a response; ``{}`` for anything else."""

    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after(response: httpx.Response) -> float | None:
    """The seconds a ``Retry-After`` header asks to wait; ``None`` when it is absent or not a number of seconds."""

    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


def _bearer(token: SecretStr) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


def _download_host_ok(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and not parts.username and host.endswith(_DOWNLOAD_HOST_SUFFIXES)


def content_url_ok(url: str) -> bool:
    """Whether the bearer token may go to ``url``: exactly ``https://dl.boxcloud.com/api/2.0/internal_files/...`` (the
    host Box documents for a representation's ``url_template``), no port, no user info, no ``..`` segment."""

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname == CONTENT_HOST
        and parts.netloc.lower() == CONTENT_HOST
        and port is None
        and parts.username is None
        and parts.password is None
        and parts.path.startswith(_CONTENT_PATH)
        and ".." not in parts.path.split("/")
    )


class BoxClient(OAuthHttpClient):
    """The OAuth web flow, the refresh, and the one read-only transport of the integration."""

    def authorize_url(self, state: str) -> str:
        """Box's sign-in page for the app. ``scope`` is sent, so a scope added later never widens a grant."""

        query = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "state": state,
            "scope": SCOPE,
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    # The one place a request is sent --------------------------------------------------------------------------------

    def _build(self, method: str, url: str, **kwargs: Any) -> httpx.Request:
        """The request, after the read-only guard: ``GET`` anywhere on Box, ``POST`` only to the two OAuth URLs."""

        if method == "POST" and url in _OAUTH_POSTS:
            return self._http.build_request(method, url, **kwargs)
        if method != "GET":
            raise RuntimeError("the Box client reads only")  # a programming error, never a runtime state
        return self._http.build_request(method, url, **kwargs)

    async def _once(self, method: str, url: str, *, stream: bool = False, **kwargs: Any) -> httpx.Response:
        request = self._build(method, url, **kwargs)
        try:
            return await self._http.send(request, stream=stream)
        except httpx.HTTPError:
            raise BoxError(BoxCode.UNREACHABLE) from None

    # OAuth ---------------------------------------------------------------------------------------------------------

    async def _grant(self, form: Mapping[str, str], seconds: float) -> TokenGrant:
        """A token call. Box answers a refusal with 400 and ``{"error": ...}``: ``invalid_grant`` (the code or the
        refresh token is unusable) is ``GRANT_INVALID``; any other refusal (a wrong secret) is ``REFUSED``; a 429 or
        5xx is ``UNREACHABLE``."""

        body = {"client_id": self._client_id, "client_secret": self._secret.get_secret_value(), **form}
        response = await self._once("POST", GRANT_URL, data=body, timeout=seconds)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
            raise BoxError(BoxCode.UNREACHABLE)
        answer = body_of(response)
        if not response.is_success or "error" in answer:
            raise BoxError(BoxCode.GRANT_INVALID if answer.get("error") == GRANT_DEAD else BoxCode.REFUSED)
        access, refresh = _text(answer.get("access_token")), _text(answer.get("refresh_token"))
        if access is None or refresh is None:
            raise BoxError(BoxCode.REFUSED)
        return TokenGrant(SecretStr(access), SecretStr(refresh), _positive(answer.get("expires_in")))

    async def exchange(self, code: str) -> TokenGrant:
        """The grant of an authorization code (valid 30 seconds); any refusal is ``CODE_INVALID``."""

        try:
            return await self._grant({"grant_type": "authorization_code", "code": code}, TIMEOUT_SECONDS)
        except BoxError as error:
            if error.code in (BoxCode.GRANT_INVALID, BoxCode.REFUSED):
                raise BoxError(BoxCode.CODE_INVALID) from None
            raise

    async def refresh(self, refresh_token: SecretStr) -> TokenGrant:
        """A new access token and the rotated refresh token (the old one is dead at once); a dead refresh token is
        ``GRANT_INVALID``."""

        form = {"grant_type": "refresh_token", "refresh_token": refresh_token.get_secret_value()}
        return await self._grant(form, REFRESH_TIMEOUT_SECONDS)

    async def revoke(self, token: SecretStr) -> bool:
        """Best effort: end the grant at Box (``POST /oauth2/revoke``). True when Box confirmed it; never raises."""

        form = {
            "client_id": self._client_id,
            "client_secret": self._secret.get_secret_value(),
            "token": token.get_secret_value(),
        }
        try:
            response = await self._once("POST", REVOKE_URL, data=form, timeout=REFRESH_TIMEOUT_SECONDS)
        except BoxError:
            return False
        return response.status_code == httpx.codes.OK

    # REST (read-only) -----------------------------------------------------------------------------------------------

    @staticmethod
    def _headers(token: SecretStr, shared_link: str | None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {**(extra or {}), **_bearer(token)}
        if shared_link is not None:
            headers["boxapi"] = (
                f"shared_link={shared_link}"  # the bearer secret of a shared item: never stored or logged
            )
        return headers

    async def _wait(self, response: httpx.Response, attempt: int, budget: WaitBudget, give_up: BoxCode) -> None:
        """Wait before the next try of a throttled, failed or not-ready read, or end it with ``give_up``."""

        if attempt >= MAX_ATTEMPTS - 1:
            raise BoxError(give_up)
        wanted = _retry_after(response)
        granted = budget.take(BACKOFF_SECONDS[attempt] if wanted is None else wanted)
        if granted is None:
            raise BoxError(give_up)
        await self._sleep(granted)

    async def pause(self, seconds: float, budget: WaitBudget, give_up: BoxCode) -> None:
        """Wait ``seconds`` out of ``budget`` (through the injectable sleep), or end the read with ``give_up`` when
        they do not fit."""

        granted = budget.take(seconds)
        if granted is None:
            raise BoxError(give_up)
        await self._sleep(granted)

    async def get(
        self,
        token: SecretStr,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        shared_link: str | None = None,
        budget: WaitBudget | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One ``GET`` below ``api.box.com/2.0`` as the user. A ``429``, a 5xx and a ``202`` wait (``Retry-After``, else
        1, 2, 4 seconds; at most ``MAX_ATTEMPTS`` tries and what is left of ``budget``) and ask again; when that
        runs out the read ends ``UNREACHABLE`` (``NOT_READY`` for a ``202``). A ``401`` is ``ACCESS_REFUSED`` (the
        caller refreshes once). Any other answer (404, 403, 200) is returned for the caller to read."""

        budget = budget or WaitBudget()
        url, sent = f"{API_URL}{path}", self._headers(token, shared_link, headers)
        for attempt in range(MAX_ATTEMPTS):
            response = await self._once("GET", url, params=params, headers=sent)
            if response.status_code == httpx.codes.UNAUTHORIZED:
                raise BoxError(BoxCode.ACCESS_REFUSED)
            if response.status_code == httpx.codes.ACCEPTED:
                await self._wait(response, attempt, budget, BoxCode.NOT_READY)
            elif response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
                await self._wait(response, attempt, budget, BoxCode.UNREACHABLE)
            else:
                return response
        raise BoxError(BoxCode.UNREACHABLE)  # unreachable: ``_wait`` raises on the last attempt

    @asynccontextmanager
    async def stream(
        self,
        token: SecretStr,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        shared_link: str | None = None,
        budget: WaitBudget | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """A ``GET`` whose body the caller reads in chunks (a file download): the open ``200`` response. Retries as
        ``get`` does. The one redirect Box answers (``302`` to the download host) is followed without the token, only
        to an https host of ``boxcloud.com`` or ``box.com``. A ``403`` or ``404`` is ``NO_ACCESS``; any other answer
        that is not ``200`` is ``REFUSED``. The response is closed on exit."""

        async with self._open(
            f"{API_URL}{path}", self._headers(token, shared_link), params, budget, refresh_on_401=True
        ) as response:
            yield response

    @asynccontextmanager
    async def stream_content(
        self, token: SecretStr, url: str, *, budget: WaitBudget | None = None
    ) -> AsyncIterator[httpx.Response]:
        """``stream`` for the content URL of a representation: ``url`` goes out WITH the token, and only when it is
        ``https://dl.boxcloud.com/api/2.0/internal_files/...`` (``content_url_ok``; anything else is ``REFUSED``
        before a request is made). A ``401`` is ``REFUSED``, not ``ACCESS_REFUSED``: the host is not the token's, so
        the caller must not refresh the token for it."""

        if not content_url_ok(url):
            raise BoxError(BoxCode.REFUSED)
        async with self._open(url, self._headers(token, None), None, budget, refresh_on_401=False) as response:
            yield response

    @asynccontextmanager
    async def _open(
        self,
        url: str,
        headers: dict[str, str],
        params: Mapping[str, Any] | None,
        budget: WaitBudget | None,
        *,
        refresh_on_401: bool,
    ) -> AsyncIterator[httpx.Response]:
        budget = budget or WaitBudget()
        redirected = False
        for attempt in range(MAX_ATTEMPTS):
            response = await self._once("GET", url, stream=True, params=None if redirected else params, headers=headers)
            status = response.status_code
            if status in _REDIRECTS and not redirected:
                location = response.headers.get("location", "")
                await response.aclose()
                if not _download_host_ok(location):
                    raise BoxError(BoxCode.REFUSED)
                url, headers, redirected = location, {}, True  # a signed URL: no token goes to the download host
                continue
            if status == httpx.codes.OK:
                try:
                    yield response
                finally:
                    await response.aclose()
                return
            await response.aclose()
            if status == httpx.codes.UNAUTHORIZED and not redirected:
                raise BoxError(BoxCode.ACCESS_REFUSED if refresh_on_401 else BoxCode.REFUSED)
            if status in (httpx.codes.FORBIDDEN, httpx.codes.NOT_FOUND):
                raise BoxError(BoxCode.NO_ACCESS)
            if status == httpx.codes.ACCEPTED:
                await self._wait(response, attempt, budget, BoxCode.NOT_READY)
            elif status == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
                await self._wait(response, attempt, budget, BoxCode.UNREACHABLE)
            else:
                raise BoxError(BoxCode.REFUSED)
        raise BoxError(BoxCode.UNREACHABLE)

    async def user(self, token: SecretStr) -> BoxUser:
        """The account of the token (``GET /users/me``): id, name and enterprise (null for a personal account)."""

        response = await self.get(token, "/users/me", {"fields": "id,name,enterprise"})
        answer = body_of(response)
        user_id = answer.get("id")
        name = _text(answer.get("name"))
        if not response.is_success or not isinstance(user_id, str) or not user_id or name is None:
            raise BoxError(BoxCode.REFUSED)
        enterprise = answer.get("enterprise")
        enterprise = enterprise if isinstance(enterprise, dict) else {}
        enterprise_id = _text(enterprise.get("id"))
        return BoxUser(
            id=user_id,
            name=name,
            enterprise_id=enterprise_id,
            enterprise_name=_text(enterprise.get("name")) if enterprise_id is not None else None,
        )
