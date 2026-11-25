"""The per-user Notion connections (docs/notion/oauth.md "Connect flow", "Tokens"). Stream C1 fills the methods marked
``C1``; ``get`` and the types are final.

One ``NotionConnections`` per process (the api holds it on ``Runtime.notion``). It signs and checks ``state``,
connects a user (code exchange at ``POST /v1/oauth/token``, which also answers the workspace and the bot), keeps each
user's access token in memory, and refreshes under a per-user lock and the row's ``FOR UPDATE`` lock: the rotated
refresh token is committed before the new access token is used. ``invalid_grant`` or a refresh token that no longer
decrypts marks the connection ``expired``. The rows come and go through the ``ConnectionStore`` port (``db.notion``
implements it). ``gateway`` hands out the ``NotionGateway`` that acts as one member, on the rate limiter of the
connection's bot. Logs carry the user id and codes only.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Protocol, override
from urllib.parse import urlencode

import httpx
from pydantic import SecretStr

from integrations.cipher import CipherError, TokenCipher
from integrations.state import StateInvalidError, StateSigner
from integrations.tokens import UserTokens
from logger.logger import INFO, WARNING, get_logger, log_event
from notion.gateway import NOTION_VERSION, PAGE_SIZE, NotionGateway
from notion.http import HttpNotionGateway, TokenSource
from notion.limiter import RateLimiters, WaitBudget
from notion.model import NotionDatabase, NotionDataSource, NotionPage, PageMarkdown, RowsPage
from notion.reasons import NotionCode, NotionError
from settings import NotionSettings

logger = get_logger("notion.connections")

ACTIVE: Final = "active"
EXPIRED: Final = "expired"
AUTHORIZE: Final = "authorize"
AUTHORIZE_URL: Final = "https://api.notion.com/v1/oauth/authorize"
OAUTH_EXCHANGE_URL: Final = "https://api.notion.com/v1/oauth/token"
REVOKE_URL: Final = "https://api.notion.com/v1/oauth/revoke"
_STATE_CONTEXT: Final = b"specflow notion oauth state"
TIMEOUT_SECONDS: Final = 30
REFRESH_TIMEOUT_SECONDS: Final = 10  # shorter: a refresh holds the row lock
_INVALID_GRANT: Final = "invalid_grant"  # the ``error`` of a used, expired or unknown code or refresh token


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TokenGrant:
    """What the token endpoint answered; the tokens stay ``SecretStr``. ``expires_in`` 0 = use once, not cached; an
    answer without it is stored as ``NO_EXPIRY_SECONDS``. An empty ``refresh_token`` = not refreshable. A refresh
    answer may leave the bot, workspace id and name empty: the stored row keeps them."""

    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int
    bot_id: str
    workspace_id: str
    workspace_name: str


@dataclass(frozen=True)
class StoredConnection:
    """A ``notion_connections`` row as the port hands it over; the refresh token is Fernet ciphertext."""

    user_id: uuid.UUID
    workspace_id: str
    workspace_name: str
    bot_id: str
    refresh_token: bytes
    status: str
    connected_at: datetime
    last_used_at: datetime


class ConnectionStore(Protocol):
    """The connection rows inside one transaction (``db.notion.ConnectionRows``)."""

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None: ...

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        """The row locked ``FOR UPDATE`` until the transaction ends."""

    async def save(
        self,
        user_id: uuid.UUID,
        grant: TokenGrant,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime | None,
        now: datetime,
    ) -> None:
        """Create or replace the user's connection, ``active``."""

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None: ...

    async def delete(self, user_id: uuid.UUID) -> bool: ...

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime | None, now: datetime
    ) -> None: ...


StoreTransaction = Callable[[], AbstractAsyncContextManager[ConnectionStore]]


@dataclass(frozen=True)
class ConnectionView:
    """What the API shows of a connection: never a token or a bot id."""

    status: Literal["active", "expired"]
    workspace_id: str
    workspace_name: str
    connected_at: datetime
    last_used_at: datetime

    @classmethod
    def of(cls, row: StoredConnection) -> ConnectionView:
        status: Literal["active", "expired"] = "expired" if row.status == EXPIRED else "active"
        return cls(status, row.workspace_id, row.workspace_name, row.connected_at, row.last_used_at)


def _object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# A token answer without ``expires_in`` never expires: it is cached for this long (``integrations.tokens`` has no "no
# expiry", and it is shared), and replaced only after a ``401`` through ``refresh(used=...)``.
NO_EXPIRY_SECONDS: Final = 10 * 365 * 24 * 3600


def _seconds(answer: Mapping[str, Any]) -> int:
    """The lifetime of an access token: ``expires_in`` when given (0 or less, or not a number, means use once), else
    "never expires"."""

    if answer.get("expires_in") is None:
        return NO_EXPIRY_SECONDS
    value = answer["expires_in"]
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class NotionOAuthClient:
    """The OAuth endpoints of the public connection (HTTP Basic with the client id and secret). A refusal is one closed
    ``NotionCode``; no token, code, ``state``, workspace name or Notion text goes into a log line, an error or a repr.
    The client ignores the proxy variables and ``.netrc`` (``trust_env=False``)."""

    def __init__(
        self,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._http = httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=transport, trust_env=False, follow_redirects=False
        )

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def authorize_url(self, state: str) -> str:
        """Notion's consent page (``owner=user``), with ``state``."""

        query = {
            "client_id": self._client_id,
            "response_type": "code",
            "owner": "user",
            "redirect_uri": self._redirect_uri,
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    async def _post(self, url: str, body: Mapping[str, str], seconds: float) -> httpx.Response:
        """One call as the client; a transport error, ``429`` or ``5xx`` is ``UNREACHABLE``."""

        auth = (self._client_id, self._client_secret.get_secret_value())
        headers = {"Notion-Version": NOTION_VERSION, "Accept": "application/json"}
        try:
            response = await self._http.post(url, json=body, auth=auth, headers=headers, timeout=seconds)
        except httpx.HTTPError:
            raise NotionError(NotionCode.UNREACHABLE) from None
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS or response.is_server_error:
            raise NotionError(NotionCode.UNREACHABLE)
        return response

    async def _grant(self, body: Mapping[str, str], seconds: float, *, identity: bool) -> TokenGrant:
        """A token call. ``invalid_grant`` (the code or refresh token is dead) is ``GRANT_INVALID``; any other refusal
        or an answer without what the flow needs (a wrong client secret, no access token) is ``REFUSED``. A grant with
        no refresh token is accepted: the connection is then not refreshable (an empty refresh token). The
        ``identity`` fields (bot and workspace) are required of a code exchange only."""

        response = await self._post(OAUTH_EXCHANGE_URL, body, seconds)
        answer = _object(response)
        if not response.is_success:
            dead = answer.get("error") == _INVALID_GRANT
            raise NotionError(NotionCode.GRANT_INVALID if dead else NotionCode.REFUSED)
        access, refresh = _text(answer.get("access_token")), _text(answer.get("refresh_token"))
        bot_id, workspace_id = _text(answer.get("bot_id")), _text(answer.get("workspace_id"))
        if not access or (identity and not (bot_id and workspace_id)):
            raise NotionError(NotionCode.REFUSED)
        return TokenGrant(
            access_token=SecretStr(access),
            refresh_token=SecretStr(refresh),
            expires_in=_seconds(answer),
            bot_id=bot_id,
            workspace_id=workspace_id,
            workspace_name=_text(answer.get("workspace_name")),
        )

    async def exchange(self, code: str) -> TokenGrant:
        """``POST /v1/oauth/token`` with ``authorization_code``; any refusal (a used, expired or unknown code) is
        ``notion_code_invalid``, an outage ``notion_unreachable``."""

        body = {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri}
        try:
            return await self._grant(body, TIMEOUT_SECONDS, identity=True)
        except NotionError as error:
            if error.code in (NotionCode.GRANT_INVALID, NotionCode.REFUSED):
                raise NotionError(NotionCode.CODE_INVALID) from None
            raise

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """``POST /v1/oauth/token`` with ``refresh_token``; the refresh token rotates. A dead one is
        ``notion_grant_invalid``, any other refusal ``notion_refused``."""

        body = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        return await self._grant(body, REFRESH_TIMEOUT_SECONDS, identity=False)

    async def revoke(self, token: SecretStr) -> bool:
        """``POST /v1/oauth/revoke``, best effort: true when Notion confirmed it; never raises."""

        try:
            response = await self._post(REVOKE_URL, {"token": token.get_secret_value()}, TIMEOUT_SECONDS)
        except NotionError:
            return False
        return response.is_success

    async def aclose(self) -> None:
        await self._http.aclose()


def _after_refusal(code: NotionCode) -> NotionCode:
    """The code of a refused refresh: a dead grant needs a reconnect; an outage stays an outage; anything else (a
    wrong client secret) is ``notion_auth_failed``."""

    if code is NotionCode.GRANT_INVALID:
        return NotionCode.RECONNECT_REQUIRED
    return code if code is NotionCode.UNREACHABLE else NotionCode.AUTH_FAILED


@dataclass
class _Attempt:
    """What one gateway call has been handed: the token its requests used, and a refreshed one for the repeat."""

    handed: str | None = None
    fresh: str | None = None


class RefreshingNotionGateway:
    """The ``NotionGateway`` of one member for one pass or import: every call goes to the inner gateway (which never
    retries a ``401``); a call that raises ``notion_auth_failed`` gets one refreshed token and is repeated once. A
    ``401`` means the call never ran, so the repeat is safe for writes. The token source of the inner gateway records
    the token it handed out in the attempt of the call that asked (a context variable, so concurrent calls on one
    wrapper each refresh with their own token); that token is what the refresh is told is dead, and the refreshed
    token serves the repeat even when it is not cached. A wrapper is made for one pass or import, which runs its calls
    in sequence; concurrent use is safe but not what it is for."""

    def __init__(
        self, connections: NotionConnections, user_id: uuid.UUID, build: Callable[[TokenSource], NotionGateway]
    ) -> None:
        self._connections = connections
        self._user_id = user_id
        self._attempt: ContextVar[_Attempt | None] = ContextVar("notion_attempt", default=None)
        self._inner = build(self._token)

    async def _token(self) -> str:
        attempt = self._attempt.get()
        if attempt is not None and attempt.fresh is not None:
            token, attempt.fresh = attempt.fresh, None
        else:
            token = await self._connections.access_token(self._user_id)
        if attempt is not None:
            attempt.handed = token
        return token

    async def _run[T](self, call: Callable[[NotionGateway], Awaitable[T]]) -> T:
        attempt = _Attempt()
        marker = self._attempt.set(attempt)
        try:
            try:
                return await call(self._inner)
            except NotionError as error:
                if error.code is not NotionCode.AUTH_FAILED:
                    raise
            attempt.fresh = await self._connections.refresh(self._user_id, used=attempt.handed)
            return await call(self._inner)
        finally:
            self._attempt.reset(marker)

    async def page(self, page_id: str) -> NotionPage:
        return await self._run(lambda g: g.page(page_id))

    async def database(self, database_id: str) -> NotionDatabase:
        return await self._run(lambda g: g.database(database_id))

    async def data_source(self, data_source_id: str) -> NotionDataSource:
        return await self._run(lambda g: g.data_source(data_source_id))

    async def page_markdown(self, page_id: str) -> PageMarkdown:
        return await self._run(lambda g: g.page_markdown(page_id))

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = PAGE_SIZE,
        where: Mapping[str, Any] | None = None,
    ) -> RowsPage:
        return await self._run(
            lambda g: g.query_data_source(data_source_id, start_cursor=start_cursor, page_size=page_size, where=where)
        )

    async def create_database(self, parent_page_id: str, title: str, properties: Mapping[str, Any]) -> NotionDatabase:
        return await self._run(lambda g: g.create_database(parent_page_id, title, properties))

    async def update_data_source(self, data_source_id: str, properties: Mapping[str, Any]) -> NotionDataSource:
        return await self._run(lambda g: g.update_data_source(data_source_id, properties))

    async def create_page(
        self, data_source_id: str, *, properties: Mapping[str, Any], markdown: str = ""
    ) -> NotionPage:
        return await self._run(lambda g: g.create_page(data_source_id, properties=properties, markdown=markdown))

    async def update_page(self, page_id: str, *, properties: Mapping[str, Any]) -> NotionPage:
        return await self._run(lambda g: g.update_page(page_id, properties=properties))

    async def replace_markdown(self, page_id: str, markdown: str) -> None:
        await self._run(lambda g: g.replace_markdown(page_id, markdown))

    async def create_comment(self, page_id: str, markdown: str) -> None:
        await self._run(lambda g: g.create_comment(page_id, markdown))


class NotionConnections:
    """Connect, disconnect, read and use the users' Notion connections."""

    def __init__(
        self,
        settings: NotionSettings,
        *,
        store: StoreTransaction,
        oauth: NotionOAuthClient | None = None,
        limiters: RateLimiters | None = None,
        http: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._oauth = oauth or NotionOAuthClient(settings.client_id, settings.client_secret, settings.callback_url)
        self._cipher = TokenCipher(settings.token_key)
        self._states = StateSigner(settings.token_key, _STATE_CONTEXT, clock=clock)
        self._limiters = limiters or RateLimiters(settings.requests_per_minute)
        # One connection pool for every gateway this process hands out: the gateways are not closed one by one.
        self._http = http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=False, follow_redirects=False)
        self._store = store
        self._now = now
        self._tokens = UserTokens(monotonic=monotonic)
        # The grant each cached access token came from: (bot id, connected_at) of the row. Another replica can
        # disconnect or reconnect the user; ``access_token`` compares the row with this before it serves the cache.
        self._grants: dict[uuid.UUID, tuple[str, datetime]] = {}

    # The connect flow ----------------------------------------------------------------------------------------------

    def authorize_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """Notion's consent page for the connection, with a ``state`` bound to the user and ``return_to`` (10 min)."""

        return self._oauth.authorize_url(self._states.sign(str(user_id), return_to, AUTHORIZE))

    async def connect(self, user_id: uuid.UUID, *, code: str, state: str) -> tuple[ConnectionView, str]:
        """Check ``state``, exchange ``code`` and store the connection (replacing one the user had); the connection and
        ``return_to``. ``NotionError`` names what failed."""

        try:
            return_to = self._states.verify(state, str(user_id), AUTHORIZE)
        except StateInvalidError:
            raise NotionError(NotionCode.STATE_INVALID) from None
        grant = await self._oauth.exchange(code)  # Notion is called with no transaction open
        now = self._now()
        async with self._tokens.lock(user_id), self._store() as store:
            await store.save(
                user_id,
                grant,
                refresh_token=self._cipher.encrypt(grant.refresh_token),
                refresh_expires_at=None,
                now=now,
            )
        self._grants[user_id] = (grant.bot_id, now)
        self._tokens.remember(user_id, grant)
        log_event(logger, INFO, "notion.connected", user_id=str(user_id))
        return ConnectionView("active", grant.workspace_id, grant.workspace_name, now, now), return_to

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection, drop its access token and revoke the grant on Notion (best effort): the revoke needs
        an access token that is cached and still good; without one it is skipped and the row is deleted all the same."""

        token = await self._cached_token(user_id)  # never refreshes just to revoke
        async with self._tokens.lock(user_id):
            async with self._store() as store:
                deleted = await store.delete(user_id)
            self._forget(user_id)
        revoked = token is not None and await self._oauth.revoke(SecretStr(token))
        log_event(logger, INFO, "notion.disconnected", user_id=str(user_id), deleted=deleted, revoked=revoked)

    async def _cached_token(self, user_id: uuid.UUID) -> str | None:
        """The cached access token of the user's current grant while it is good; never a refresh."""

        try:
            row = await self._active(user_id)
        except NotionError:
            return None
        if self._grants.get(user_id) != (row.bot_id, row.connected_at):
            return None

        async def no_refresh() -> TokenGrant:
            raise NotionError(NotionCode.UNREACHABLE)

        try:
            return await self._tokens.access_token(user_id, no_refresh)
        except NotionError:
            return None

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        async with self._store() as store:
            row = await store.get(user_id)
        return None if row is None else ConnectionView.of(row)

    # Use -----------------------------------------------------------------------------------------------------------

    async def _active(self, user_id: uuid.UUID) -> StoredConnection:
        """The user's row, read fresh: ``notion_not_connected`` without one, ``notion_reconnect_required`` when it
        expired; a cached token of a connection that is gone is dropped."""

        async with self._store() as store:
            row = await store.get(user_id)
        if row is None:
            self._forget(user_id)
            raise NotionError(NotionCode.NOT_CONNECTED)
        if row.status != ACTIVE:
            self._forget(user_id)
            raise NotionError(NotionCode.RECONNECT_REQUIRED)
        return row

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else a refreshed one; ``NotionError`` with
        ``notion_not_connected``, ``notion_reconnect_required``, ``notion_unreachable`` or ``notion_auth_failed``. The
        row is read first, every time (another process may have deleted or replaced the connection)."""

        row = await self._active(user_id)
        if self._grants.get(user_id) != (row.bot_id, row.connected_at):
            self._forget(user_id)  # the cached token, if any, belongs to an older grant: refresh from this row
        return await self._tokens.access_token(user_id, lambda: self._rotate(user_id))

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token; ``used`` is a token Notion refused, never handed out again."""

        return await self._tokens.refresh(user_id, lambda: self._rotate(user_id), used=used)

    async def gateway(self, user_id: uuid.UUID) -> NotionGateway:
        """The ``NotionGateway`` that acts as ``user_id``. It is ``HttpNotionGateway`` (stream A, which never retries a
        ``401``) inside a refreshing wrapper defined in this file: the wrapper's token source (the ``token`` callable
        of ``HttpNotionGateway``, called once per request) is ``access_token(user_id)`` and remembers the token it
        handed out; when a call raises ``notion_auth_failed`` the wrapper calls ``refresh(user_id, used=<that token>)``
        once and repeats the call once. A ``401`` means the call never ran, so the repeat is safe for writes too.
        Requests go through the limiter of the connection's bot (shared by import and export), with a fresh
        ``WaitBudget`` per call, so per import or per pass."""

        row = await self._active(user_id)
        limiter = self._limiters.for_bot(row.bot_id)

        def build(token: TokenSource) -> NotionGateway:
            return HttpNotionGateway(token, limiter, budget=WaitBudget(), client=self._http)

        return RefreshingNotionGateway(self, user_id, build)

    async def aclose(self) -> None:
        await self._tokens.aclose()
        await self._oauth.aclose()
        await self._http.aclose()

    # Inside --------------------------------------------------------------------------------------------------------

    def _forget(self, user_id: uuid.UUID) -> None:
        self._tokens.forget(user_id)
        self._grants.pop(user_id, None)

    async def _refresh_row(self, row: StoredConnection) -> TokenGrant:
        """One refresh of the row's grant. A grant with no refresh token (Notion gave none), or one that no longer
        decrypts (a rotated ``NOTION_TOKEN_KEY``), cannot be refreshed: ``notion_reconnect_required`` without calling
        Notion."""

        try:
            refresh_token = self._cipher.decrypt(row.refresh_token).get_secret_value()
        except CipherError:
            raise NotionError(NotionCode.RECONNECT_REQUIRED) from None
        if not refresh_token:
            raise NotionError(NotionCode.RECONNECT_REQUIRED)
        try:
            return await self._oauth.refresh(refresh_token)
        except NotionError as error:
            raise NotionError(_after_refusal(error.code)) from None

    async def _rotate(self, user_id: uuid.UUID) -> TokenGrant:
        """One refresh with the row locked ``FOR UPDATE``; the transaction commits the rotated refresh token (or the
        ``expired`` status) before this returns."""

        grant: TokenGrant | None = None
        refused = NotionCode.AUTH_FAILED
        expired = False
        async with self._store() as store:
            row = await store.lock(user_id)
            if row is None:
                refused = NotionCode.NOT_CONNECTED
            elif row.status != ACTIVE:
                refused = NotionCode.RECONNECT_REQUIRED
            else:
                try:
                    grant = await self._refresh_row(row)
                except NotionError as error:
                    refused = error.code
                if grant is not None:
                    self._grants[user_id] = (row.bot_id, row.connected_at)
                    await store.rotate(
                        user_id,
                        refresh_token=self._cipher.encrypt(grant.refresh_token),
                        refresh_expires_at=None,
                        now=self._now(),
                    )
                elif refused is NotionCode.RECONNECT_REQUIRED:
                    await store.expire(user_id, now=self._now())
                    expired = True
        if grant is not None:
            log_event(logger, INFO, "notion.refreshed", user_id=str(user_id))
            return grant
        if expired:  # after the commit: the log never says "expired" for a status that was rolled back
            log_event(logger, WARNING, "notion.expired", user_id=str(user_id), code=refused.value)
        raise NotionError(refused)
