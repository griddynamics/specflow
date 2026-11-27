"""The per-user Dropbox connections (docs/dropbox/oauth.md "Connect flow", "Tokens").

One ``DropboxConnections`` per process (the api holds it on ``Runtime.dropbox``). It signs and checks ``state``,
connects a user (code exchange with offline access, ``users/get_current_account``), keeps each user's access token in
memory until 60 s before it expires (``integrations.tokens``) and refreshes it. **No rotation** (DB4): a Dropbox refresh
token does not change and does not expire, so there is no ``refresh_expires_at`` and no ``FOR UPDATE`` rotate step; a
refresh only reads the row. A refused refresh (``invalid_grant``) or a refresh token that no longer decrypts
(``CipherError``) marks the connection ``expired``; it is never refreshed again until the user reconnects. The rows come
and go through the ``ConnectionStore`` port (``db.dropbox`` implements it). Logs carry the user id and codes only; the
stored name is a display name, the e-mail address is never read or stored.

The dataclasses and the port are frozen by W0; the tests are ``tests/test_dropbox_connections.py``. A copy of
``box.connections`` without the rotation.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol

from pydantic import SecretStr

from dropbox.client import DropboxAccount, DropboxClient, TokenGrant
from dropbox.codes import DropboxCode, DropboxError
from integrations.cipher import CipherError, TokenCipher
from integrations.state import StateInvalidError, StateSigner
from integrations.tokens import UserTokens
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import DropboxSettings

logger = get_logger("dropbox.connections")

ACTIVE: Final = "active"
EXPIRED: Final = "expired"
AUTHORIZE: Final = "authorize"
STATE_CONTEXT: Final = b"specflow dropbox oauth state"
PERSONAL_PREFIX: Final = "user:"  # workspace id of a personal account: ``user:<account_id>`` (decision DB5)
TEAM_PREFIX: Final = "team:"  # workspace id of a Business member: ``team:<team_id>`` (decision DB5)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StoredConnection:
    """A ``dropbox_connections`` row as the port hands it over; the refresh token is Fernet ciphertext."""

    user_id: uuid.UUID
    account_id: str
    name: str
    team_id: str | None
    team_name: str | None
    refresh_token: bytes
    status: str
    connected_at: datetime
    last_used_at: datetime


class ConnectionStore(Protocol):
    """The connection rows inside one transaction (``db.dropbox.ConnectionRows``)."""

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None: ...

    async def save(self, user_id: uuid.UUID, account: DropboxAccount, *, refresh_token: bytes, now: datetime) -> None:
        """Create or replace the user's connection, ``active`` (name, team; never the e-mail)."""

    async def touch(self, user_id: uuid.UUID, *, now: datetime) -> None:
        """Set ``last_used_at`` (after a refresh)."""

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None: ...

    async def delete(self, user_id: uuid.UUID) -> bool: ...


StoreTransaction = Callable[[], AbstractAsyncContextManager[ConnectionStore]]


@dataclass(frozen=True)
class ConnectionView:
    """What the API shows of a connection: never a token or the Dropbox account id. ``workspace_id`` and
    ``workspace_name`` are the project integration's workspace (decision DB5): ``team:<team_id>`` and the team name for
    a Business member, else ``user:<account_id>`` and the display name."""

    status: Literal["active", "expired"]
    name: str
    team_name: str | None
    workspace_id: str
    workspace_name: str
    connected_at: datetime
    last_used_at: datetime

    @classmethod
    def of(cls, row: StoredConnection) -> ConnectionView:
        status: Literal["active", "expired"] = "expired" if row.status == EXPIRED else "active"
        if row.team_id is not None:
            workspace_id, workspace_name = f"{TEAM_PREFIX}{row.team_id}", row.team_name or row.name
        else:
            workspace_id, workspace_name = f"{PERSONAL_PREFIX}{row.account_id}", row.name
        return cls(status, row.name, row.team_name, workspace_id, workspace_name, row.connected_at, row.last_used_at)


def _refused(code: DropboxCode) -> DropboxCode:
    """The code of a refused refresh: a dead grant needs a reconnect; a server or network problem is
    ``dropbox_unreachable``; anything else (a wrong app secret) is ``dropbox_auth_failed``."""

    if code is DropboxCode.GRANT_INVALID:
        return DropboxCode.RECONNECT_REQUIRED
    if code is DropboxCode.UNREACHABLE:
        return DropboxCode.UNREACHABLE
    return DropboxCode.AUTH_FAILED


class DropboxConnections:
    """Connect, disconnect, read and use the users' Dropbox connections."""

    def __init__(
        self,
        settings: DropboxSettings,
        *,
        store: StoreTransaction,
        client: DropboxClient | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Build the client (``settings.callback_url`` as redirect URI) unless one is given, the ``TokenCipher`` and the
        ``StateSigner`` (context ``STATE_CONTEXT``) of ``settings.token_key``, and the in-memory ``UserTokens``."""

        self._store = store
        self._client = client or DropboxClient(settings.client_id, settings.client_secret, settings.callback_url)
        self._cipher = TokenCipher(settings.token_key)
        self._states = StateSigner(settings.token_key, STATE_CONTEXT, clock=clock)
        self._now = now
        self._tokens = UserTokens(monotonic=monotonic)
        # The grant each cached access token came from: (account id, connected_at) of the row. Another process can
        # disconnect or reconnect the user; ``access_token`` compares the row with this before it serves the cache.
        self._grants: dict[uuid.UUID, tuple[str, datetime]] = {}

    @property
    def client(self) -> DropboxClient:
        """The Dropbox client (the shared transport), for ``dropbox.content.DropboxContent``."""

        return self._client

    def authorize_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """Dropbox's consent page for the app, with a ``state`` bound to the user, purpose ``AUTHORIZE`` and
        ``return_to`` (10 minutes)."""

        return self._client.authorize_url(self._states.sign(str(user_id), return_to, AUTHORIZE))

    async def connect(self, user_id: uuid.UUID, *, code: str, state: str) -> tuple[ConnectionView, str]:
        """Check ``state`` (``dropbox_state_invalid``), exchange ``code`` (``dropbox_code_invalid``, ``unreachable``),
        read the account (``get_current_account``) and store the connection with the encrypted refresh token
        (replacing one the user had); remember the access token; the view and ``return_to``."""

        try:
            return_to = self._states.verify(state, str(user_id), AUTHORIZE)
        except StateInvalidError:
            raise DropboxError(DropboxCode.STATE_INVALID) from None
        grant = await self._client.exchange(code)
        if grant.refresh_token is None:
            raise DropboxError(DropboxCode.CODE_INVALID)
        try:
            account = await self._client.get_current_account(grant.access_token)
        except DropboxError as error:
            unreachable = error.code is DropboxCode.UNREACHABLE
            raise DropboxError(DropboxCode.UNREACHABLE if unreachable else DropboxCode.CODE_INVALID) from None
        now = self._now()
        async with self._tokens.lock(user_id), self._store() as store:
            await store.save(user_id, account, refresh_token=self._cipher.encrypt(grant.refresh_token), now=now)
        self._grants[user_id] = (account.account_id, now)
        self._tokens.remember(user_id, grant)
        log_event(logger, INFO, "dropbox.connected", user_id=str(user_id))
        stored = StoredConnection(
            user_id, account.account_id, account.name, account.team_id, account.team_name, b"", ACTIVE, now, now
        )
        return ConnectionView.of(stored), return_to

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection, drop its cached access token and revoke the grant at Dropbox (best effort,
        ``client.revoke`` with a good access token, refreshed first when none is cached; a dead connection skips it)."""

        try:
            token: str | None = await self.access_token(user_id)  # for the revoke; a dead connection has none
        except DropboxError:
            token = None
        async with self._tokens.lock(user_id):
            async with self._store() as store:
                deleted = await store.delete(user_id)
            self._forget(user_id)
        revoked = await self._client.revoke(SecretStr(token)) if token is not None else False
        log_event(logger, INFO, "dropbox.disconnected", user_id=str(user_id), deleted=deleted, revoked=revoked)

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        """The user's connection as the API shows it; ``None`` when there is none."""

        async with self._store() as store:
            row = await store.get(user_id)
        return None if row is None else ConnectionView.of(row)

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else a refreshed one. The row is read first, every time: no row is
        ``dropbox_not_connected``, a row not ``active`` is ``dropbox_reconnect_required``, a row of another grant
        (account id or ``connected_at``) drops the cache. A refresh refused with ``invalid_grant`` or a refresh token
        that does not decrypt expires the row (``dropbox_reconnect_required``); ``unreachable`` keeps it active."""

        async with self._store() as store:
            row = await store.get(user_id)
        if row is None:
            self._forget(user_id)
            raise DropboxError(DropboxCode.NOT_CONNECTED)
        if row.status != ACTIVE:
            self._forget(user_id)
            raise DropboxError(DropboxCode.RECONNECT_REQUIRED)
        if self._grants.get(user_id) != (row.account_id, row.connected_at):
            self._forget(user_id)  # the cached token, if any, belongs to an older grant: refresh from this row
        return await self._tokens.access_token(user_id, lambda: self._refresh_or_raise(user_id))

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token; ``used`` is a token Dropbox refused (a 401), never handed out again."""

        return await self._tokens.refresh(user_id, lambda: self._refresh_or_raise(user_id), used=used)

    async def call[T](self, user_id: uuid.UUID, request: Callable[[SecretStr], Awaitable[T]]) -> T:
        """``request`` with the user's access token; ``dropbox_token_refused`` (a refreshable 401) refreshes once and
        asks once more; a second refusal is ``dropbox_auth_failed``."""

        token = await self.access_token(user_id)
        try:
            return await request(SecretStr(token))
        except DropboxError as error:
            if error.code is not DropboxCode.ACCESS_REFUSED:
                raise
        try:
            return await request(SecretStr(await self.refresh(user_id, used=token)))
        except DropboxError as error:
            if error.code is DropboxCode.ACCESS_REFUSED:
                raise DropboxError(DropboxCode.AUTH_FAILED) from None  # a fresh token was refused too: a setup problem
            raise

    async def aclose(self) -> None:
        """End the token tasks and the HTTP client."""

        await self._tokens.aclose()
        await self._client.aclose()

    def _forget(self, user_id: uuid.UUID) -> None:
        self._tokens.forget(user_id)
        self._grants.pop(user_id, None)

    async def _refresh_or_raise(self, user_id: uuid.UUID) -> TokenGrant:
        """One refresh: the row is read in one transaction (the refresh token never rotates, so there is no lock and no
        new token to write), the token endpoint is called outside any transaction, then a second transaction touches
        the row, or commits ``expired`` for a dead refresh token or one that does not decrypt before this raises."""

        async with self._store() as store:
            row = await store.get(user_id)
        if row is None:
            raise DropboxError(DropboxCode.NOT_CONNECTED)
        if row.status != ACTIVE:
            raise DropboxError(DropboxCode.RECONNECT_REQUIRED)
        try:
            grant = await self._client.refresh(self._cipher.decrypt(row.refresh_token))
        except CipherError:
            refused = DropboxCode.RECONNECT_REQUIRED  # a rotated DROPBOX_TOKEN_KEY: every user reconnects
        except DropboxError as error:
            refused = _refused(error.code)
        else:
            self._grants[user_id] = (row.account_id, row.connected_at)
            async with self._store() as store:
                await store.touch(user_id, now=self._now())
            log_event(logger, INFO, "dropbox.refreshed", user_id=str(user_id))
            return grant
        if refused is DropboxCode.RECONNECT_REQUIRED:
            async with self._store() as store:
                await store.expire(user_id, now=self._now())
            log_event(logger, WARNING, "dropbox.expired", user_id=str(user_id), code=refused.value)
        raise DropboxError(refused)


__all__ = [
    "ACTIVE",
    "EXPIRED",
    "ConnectionStore",
    "ConnectionView",
    "DropboxConnections",
    "StoreTransaction",
    "StoredConnection",
    "TokenGrant",
]
