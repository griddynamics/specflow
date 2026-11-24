"""The per-user Atlassian connections of the ``oauth`` mode (docs/jira-sync/oauth.md "Connect flow", "Tokens").

One ``AtlassianConnections`` per process, held on ``Runtime.atlassian``. It signs and checks ``state``, connects a
user (code exchange, the site's cloud, ``/myself``), keeps each user's access token in memory until 60 s before it
expires, and refreshes under a per-user ``asyncio.Lock`` and the row's ``FOR UPDATE`` lock: the rotated refresh token
is committed before the new access token is used, even when the caller is cancelled meanwhile. ``invalid_grant``
marks the connection ``expired``; it is never refreshed again until the user reconnects. Logs carry the user id and
codes only.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import SecretStr

from db import atlassian as atlassian_db
from db.models import AtlassianConnection
from integrations.tokens import UserTokens
from jira.gateway import JiraError, JiraGateway
from jira.http import HttpJiraGateway, TokenSource
from jira.oauth import AtlassianOAuthClient, OAuthCode, OAuthError, StateSigner, TokenCipher, TokenGrant
from jira.reasons import Reason
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import AtlassianOAuthSettings

logger = get_logger("api.jira.connections")

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
        self._tokens = UserTokens(monotonic=monotonic)
        self._gateways: dict[tuple[uuid.UUID, str], JiraGateway] = {}  # closed only by ``aclose``: a pass may hold one

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
        async with self._tokens.lock(user_id), self._transaction() as session:
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
        self._tokens.remember(user_id, grant)
        log_event(logger, INFO, "api.atlassian.connected", user_id=str(user_id))
        view = ConnectionView("active", account.name, cloud_id, now, now)
        return view, return_to

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection and drop its access token (Atlassian has no revoke endpoint for 3LO)."""

        async with self._tokens.lock(user_id):
            async with self._transaction() as session:
                deleted = await atlassian_db.delete_connection(session, user_id)
            self._tokens.forget(user_id)
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

    @property
    def site_url(self) -> str:
        """The one Atlassian site of this deployment (``ATLASSIAN_SITE_URL``)."""

        return self._site_url

    def token_source(self, user_id: uuid.UUID) -> TokenSource:
        """The tokens of ``user_id`` for a reader of its own (an importer): refreshed once on a ``401``."""

        return _UserTokens(self, user_id)

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else a refreshed one."""

        return await self._tokens.access_token(user_id, lambda: self._rotate_or_raise(user_id))

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token. Callers that wait for the lock while another refreshes get that one (one token call);
        ``used`` is the token Jira refused, never handed out again."""

        return await self._tokens.refresh(user_id, lambda: self._rotate_or_raise(user_id), used=used)

    async def aclose(self) -> None:
        await self._tokens.aclose()
        gateways, self._gateways = list(self._gateways.values()), {}
        for gateway in gateways:
            await gateway.aclose()
        await self._client.aclose()

    # Inside --------------------------------------------------------------------------------------------------------

    async def _rotate_or_raise(self, user_id: uuid.UUID) -> TokenGrant:
        grant, refused = await self._rotate(user_id)
        if grant is None:
            raise JiraError(refused)
        return grant

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
