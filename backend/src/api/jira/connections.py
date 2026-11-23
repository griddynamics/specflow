"""The per-user Atlassian connections of the ``oauth`` mode (docs/jira-sync/oauth.md "Connect flow", "Tokens").

One ``AtlassianConnections`` per process, held on ``Runtime.atlassian``. It signs and checks ``state``, connects a
user (code exchange, the site's cloud, ``/myself``), keeps each user's access token in memory until 60 s before it
expires, and refreshes under a per-user ``asyncio.Lock`` and the row's ``FOR UPDATE`` lock: the rotated refresh token
is committed before the new access token is used, even when the caller is cancelled meanwhile. ``invalid_grant``
marks the connection ``expired``; it is never refreshed again until the user reconnects. Logs carry the user id and
codes only.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import SecretStr

from db import atlassian as atlassian_db
from db.models import AtlassianConnection
from jira.gateway import JiraError, JiraGateway
from jira.http import HttpJiraGateway, TokenSource
from jira.oauth import AtlassianOAuthClient, OAuthCode, OAuthError, StateSigner, TokenCipher, TokenGrant
from jira.reasons import Reason
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import AtlassianOAuthSettings

logger = get_logger("api.jira.connections")

EARLY_EXPIRY_SECONDS: Final = 60  # an access token is dropped this long before Atlassian's ``expires_in``

GatewayFactory = Callable[[str, TokenSource], JiraGateway]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ConnectionView:
    """What the API shows of a connection: never a token or the Atlassian account id."""

    status: Literal["active", "expired"]
    account_name: str
    cloud_id: str
    connected_at: datetime
    last_used_at: datetime

    @classmethod
    def of(cls, row: AtlassianConnection) -> ConnectionView:
        status: Literal["active", "expired"] = "expired" if row.status == atlassian_db.EXPIRED else "active"
        return cls(status, row.account_name, row.cloud_id, row.connected_at, row.last_used_at)


@dataclass
class _Token:
    value: str
    good_until: float  # monotonic seconds


class _UserTokens:
    """The ``TokenSource`` of one user's gateway."""

    def __init__(self, connections: AtlassianConnections, user_id: uuid.UUID) -> None:
        self._connections = connections
        self._user_id = user_id

    async def access_token(self) -> str:
        return await self._connections.access_token(self._user_id)

    async def refreshed(self, used: str) -> str:
        return await self._connections.refresh(self._user_id, used=used)


class AtlassianConnections:
    """Connect, disconnect, read and use the users' Atlassian connections."""

    def __init__(
        self,
        settings: AtlassianOAuthSettings,
        *,
        transaction: Callable[[], AbstractAsyncContextManager[Any]],
        client: AtlassianOAuthClient | None = None,
        gateway_factory: GatewayFactory = HttpJiraGateway.oauth,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client or AtlassianOAuthClient(settings.client_id, settings.client_secret, settings.callback_url)
        self._cipher = TokenCipher(settings.token_key)
        self._states = StateSigner(settings.token_key, clock=clock)
        self._site_url = settings.site_url
        self._transaction = transaction
        self._gateway_factory = gateway_factory
        self._now = now
        self._monotonic = monotonic
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._tokens: dict[uuid.UUID, _Token] = {}
        self._gateways: dict[tuple[uuid.UUID, str], JiraGateway] = {}  # closed only by ``aclose``: a pass may hold one
        self._rotations: set[asyncio.Future[tuple[str | None, Reason]]] = set()  # in flight; ``aclose`` awaits them

    # The connect flow ----------------------------------------------------------------------------------------------

    def authorize_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """The consent URL of Atlassian with a ``state`` bound to the user and ``return_to`` (10 minutes)."""

        return self._client.authorize_url(self._states.sign(str(user_id), return_to))

    async def connect(self, user_id: uuid.UUID, *, code: str, state: str) -> tuple[ConnectionView, str]:
        """Check ``state``, exchange ``code``, find the site's cloud and the account, store the connection (replacing
        one the user had); the connection and ``return_to``. ``OAuthError`` names what failed."""

        return_to = self._states.verify(state, str(user_id))
        grant = await self._client.exchange(code)
        cloud_id = await self._client.cloud_id(grant.access_token, self._site_url)
        account = await self._client.account(grant.access_token, cloud_id)
        now = self._now()
        async with self._lock(user_id), self._transaction() as session:
            await atlassian_db.upsert_connection(
                session,
                user_id,
                cloud_id=cloud_id,
                account_id=account.account_id,
                account_name=account.name,
                scopes=grant.scope,
                refresh_token=self._cipher.encrypt(grant.refresh_token),
                now=now,
            )
        self._remember(user_id, grant)
        log_event(logger, INFO, "api.atlassian.connected", user_id=str(user_id))
        view = ConnectionView("active", account.name, cloud_id, now, now)
        return view, return_to

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection and drop its access token (Atlassian has no revoke endpoint for 3LO)."""

        async with self._lock(user_id):
            async with self._transaction() as session:
                deleted = await atlassian_db.delete_connection(session, user_id)
            self._tokens.pop(user_id, None)
        log_event(logger, INFO, "api.atlassian.disconnected", user_id=str(user_id), deleted=deleted)

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        async with self._transaction() as session:
            row = await atlassian_db.get_connection(session, user_id)
        return None if row is None else ConnectionView.of(row)

    # Use -----------------------------------------------------------------------------------------------------------

    async def gateway(self, user_id: uuid.UUID | None) -> JiraGateway:
        """The Jira gateway acting as ``user_id``; ``jira_not_connected`` without a connection,
        ``jira_reconnect_required`` for an expired one."""

        view = await self.get(user_id) if user_id is not None else None
        if user_id is None or view is None:
            raise JiraError(Reason.NOT_CONNECTED)
        if view.status != "active":
            raise JiraError(Reason.RECONNECT_REQUIRED)
        key = (user_id, view.cloud_id)
        if key not in self._gateways:
            self._gateways[key] = self._gateway_factory(view.cloud_id, _UserTokens(self, user_id))
        return self._gateways[key]

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else a refreshed one."""

        cached = self._tokens.get(user_id)
        if cached is not None and cached.good_until > self._monotonic():
            return cached.value
        return await self.refresh(user_id, used=None)

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token. Callers that wait for the lock while another refreshes get that one (one token call);
        ``used`` is the token Jira refused, never handed out again."""

        async with self._lock(user_id):
            cached = self._tokens.get(user_id)
            if cached is not None and cached.value != used and cached.good_until > self._monotonic():
                return cached.value
            self._tokens.pop(user_id, None)
            # Its own task: a cancelled caller cannot roll back a rotation Atlassian already made; ``aclose`` waits.
            rotation = asyncio.ensure_future(self._rotate_and_remember(user_id))
            self._rotations.add(rotation)
            rotation.add_done_callback(self._rotations.discard)
            token, refused = await asyncio.shield(rotation)
            if token is None:
                raise JiraError(refused)
            return token

    async def aclose(self) -> None:
        await asyncio.gather(*self._rotations, return_exceptions=True)
        gateways, self._gateways = list(self._gateways.values()), {}
        for gateway in gateways:
            await gateway.aclose()
        await self._client.aclose()

    # Inside --------------------------------------------------------------------------------------------------------

    def _lock(self, user_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def _remember(self, user_id: uuid.UUID, grant: TokenGrant) -> str:
        value = grant.access_token.get_secret_value()
        self._tokens[user_id] = _Token(value, self._monotonic() + grant.expires_in - EARLY_EXPIRY_SECONDS)
        return value

    async def _rotate_and_remember(self, user_id: uuid.UUID) -> tuple[str | None, Reason]:
        grant, refused = await self._rotate(user_id)
        return (None if grant is None else self._remember(user_id, grant)), refused

    async def _rotate(self, user_id: uuid.UUID) -> tuple[TokenGrant | None, Reason]:
        """One refresh with the row locked ``FOR UPDATE``; the transaction commits the rotated refresh token (or the
        ``expired`` status) before this returns. The refusal reason when there is no grant."""

        grant: TokenGrant | None = None
        refused = Reason.UNEXPECTED
        expired = False  # this refresh found the grant dead (an earlier one may have)
        async with self._transaction() as session:
            row = await atlassian_db.lock_connection(session, user_id)
            if row is None:
                refused = Reason.NOT_CONNECTED
            elif row.status != atlassian_db.ACTIVE:
                refused = Reason.RECONNECT_REQUIRED
            else:
                try:
                    grant = await self._client.refresh(self._decrypt(row.refresh_token))
                except OAuthError as error:
                    refused = _refused(error.code)
                    if refused is Reason.RECONNECT_REQUIRED:
                        await atlassian_db.mark_expired(session, user_id, now=self._now())
                        expired = True
                else:
                    now = self._now()
                    await atlassian_db.rotate_refresh_token(
                        session, user_id, refresh_token=self._cipher.encrypt(grant.refresh_token), now=now
                    )
        if grant is not None:
            log_event(logger, INFO, "api.atlassian.refreshed", user_id=str(user_id))
        elif expired:
            log_event(logger, WARNING, "api.atlassian.expired", user_id=str(user_id), code=refused.value)
        return grant, refused

    def _decrypt(self, ciphertext: bytes) -> SecretStr:
        return self._cipher.decrypt(bytes(ciphertext))


def _refused(code: OAuthCode) -> Reason:
    """The sync reason of a refused refresh: a dead grant needs a reconnect; a server or network problem is
    ``jira_unreachable``; anything else (a wrong client secret) is ``jira_auth_failed``."""

    if code is OAuthCode.GRANT_INVALID:
        return Reason.RECONNECT_REQUIRED
    if code is OAuthCode.UNREACHABLE:
        return Reason.UNREACHABLE
    return Reason.AUTH_FAILED
