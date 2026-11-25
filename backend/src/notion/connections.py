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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

from pydantic import SecretStr

from integrations.cipher import TokenCipher
from integrations.state import StateSigner
from integrations.tokens import UserTokens
from logger.logger import get_logger
from notion.gateway import NotionGateway
from notion.limiter import RateLimiters
from settings import NotionSettings

logger = get_logger("notion.connections")

ACTIVE: Final = "active"
EXPIRED: Final = "expired"
AUTHORIZE: Final = "authorize"
AUTHORIZE_URL: Final = "https://api.notion.com/v1/oauth/authorize"
OAUTH_EXCHANGE_URL: Final = "https://api.notion.com/v1/oauth/token"
REVOKE_URL: Final = "https://api.notion.com/v1/oauth/revoke"
_STATE_CONTEXT: Final = b"specflow notion oauth state"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TokenGrant:
    """What the token endpoint answered; the tokens stay ``SecretStr``. ``expires_in`` 0 = use once, not cached."""

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


class NotionOAuthClient:
    """The OAuth endpoints of the public connection (HTTP Basic with the client id and secret). Stream C1 fills it."""

    def __init__(self, client_id: str, client_secret: SecretStr, redirect_uri: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def authorize_url(self, state: str) -> str:
        """Notion's consent page (``owner=user``), with ``state``."""

        raise NotImplementedError("C1 fills NotionOAuthClient.authorize_url")

    async def exchange(self, code: str) -> TokenGrant:
        """``POST /v1/oauth/token`` with ``authorization_code``; ``NotionError`` ``notion_code_invalid`` or
        ``notion_unreachable``."""

        raise NotImplementedError("C1 fills NotionOAuthClient.exchange")

    async def refresh(self, refresh_token: str) -> TokenGrant:
        """``POST /v1/oauth/token`` with ``refresh_token``; the refresh token rotates."""

        raise NotImplementedError("C1 fills NotionOAuthClient.refresh")

    async def revoke(self, token: SecretStr) -> bool:
        """``POST /v1/oauth/revoke``, best effort."""

        raise NotImplementedError("C1 fills NotionOAuthClient.revoke")

    async def aclose(self) -> None:
        """Close the HTTP client (C1 adds it with the other methods; none is open yet)."""


class NotionConnections:
    """Connect, disconnect, read and use the users' Notion connections."""

    def __init__(
        self,
        settings: NotionSettings,
        *,
        store: StoreTransaction,
        oauth: NotionOAuthClient | None = None,
        limiters: RateLimiters | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._oauth = oauth or NotionOAuthClient(settings.client_id, settings.client_secret, settings.callback_url)
        self._cipher = TokenCipher(settings.token_key)
        self._states = StateSigner(settings.token_key, _STATE_CONTEXT, clock=clock)
        self._limiters = limiters or RateLimiters(settings.requests_per_minute)
        self._store = store
        self._now = now
        self._tokens = UserTokens(monotonic=monotonic)

    # The connect flow ----------------------------------------------------------------------------------------------

    def authorize_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """Notion's consent page for the connection, with a ``state`` bound to the user and ``return_to`` (10 min)."""

        raise NotImplementedError("C1 fills NotionConnections.authorize_url")

    async def connect(self, user_id: uuid.UUID, *, code: str, state: str) -> tuple[ConnectionView, str]:
        """Check ``state``, exchange ``code`` and store the connection (replacing one the user had); the connection and
        ``return_to``. ``NotionError`` names what failed."""

        raise NotImplementedError("C1 fills NotionConnections.connect")

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection, drop its access token and revoke the grant on Notion (best effort)."""

        raise NotImplementedError("C1 fills NotionConnections.disconnect")

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        async with self._store() as store:
            row = await store.get(user_id)
        return None if row is None else ConnectionView.of(row)

    # Use -----------------------------------------------------------------------------------------------------------

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else a refreshed one; ``NotionError`` with
        ``notion_not_connected``, ``notion_reconnect_required``, ``notion_unreachable`` or ``notion_auth_failed``. The
        row is read first, every time (another process may have deleted or replaced the connection)."""

        raise NotImplementedError("C1 fills NotionConnections.access_token")

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token; ``used`` is a token Notion refused, never handed out again."""

        raise NotImplementedError("C1 fills NotionConnections.refresh")

    async def gateway(self, user_id: uuid.UUID) -> NotionGateway:
        """The ``NotionGateway`` that acts as ``user_id``: a ``401`` refreshes the token once; the requests go through
        the limiter of the connection's bot (shared by import and export). A fresh ``WaitBudget`` per call, so per
        import or per pass."""

        raise NotImplementedError("C1 fills NotionConnections.gateway")

    async def aclose(self) -> None:
        await self._tokens.aclose()
        await self._oauth.aclose()
