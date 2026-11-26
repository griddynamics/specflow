"""The per-user Box connections (docs/box/oauth.md "Connect flow", "Tokens").

One ``BoxConnections`` per process (the api holds it on ``Runtime.box``). It signs and checks ``state``, connects a user
(code exchange, ``GET /users/me``), keeps each user's access token in memory until 60 s before it expires, and
refreshes under a per-user lock and the row's ``FOR UPDATE`` lock: the rotated refresh token is committed before the new
access token is used (Box disables the old one at once, there is no grace period; decision B5). ``invalid_grant`` or a
refresh token that no longer decrypts marks the connection ``expired``; it is never refreshed again until the user
reconnects. The rows come and go through the ``ConnectionStore`` port (``db.box`` implements it). Logs carry the user
id and codes only; the stored name is a display name, the Box ``login`` (an e-mail) is not stored.

The dataclasses and the port are frozen by W0; the tests are ``tests/test_box_connections.py``. A copy of
``github.connections`` with the Box differences of docs/box/oauth.md.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal, Protocol

from pydantic import SecretStr

from box.client import REFRESH_VALID_SECONDS, BoxClient, BoxUser, TokenGrant
from box.codes import BoxCode, BoxError
from integrations.cipher import CipherError, TokenCipher
from integrations.state import StateInvalidError, StateSigner
from integrations.tokens import UserTokens
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import BoxSettings

logger = get_logger("box.connections")

ACTIVE: Final = "active"
EXPIRED: Final = "expired"
AUTHORIZE: Final = "authorize"
_STATE_CONTEXT: Final = b"specflow box oauth state"
PERSONAL_PREFIX: Final = "user:"  # workspace id of a personal account: ``user:<box user id>`` (decision B6)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StoredConnection:
    """A ``box_connections`` row as the port hands it over; the refresh token is Fernet ciphertext."""

    user_id: uuid.UUID
    box_user_id: str
    name: str
    enterprise_id: str | None
    enterprise_name: str | None
    refresh_token: bytes
    status: str
    connected_at: datetime
    last_used_at: datetime


class ConnectionStore(Protocol):
    """The connection rows inside one transaction (``db.box.ConnectionRows``)."""

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None: ...

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        """The row locked ``FOR UPDATE`` until the transaction ends."""

    async def save(
        self,
        user_id: uuid.UUID,
        user: BoxUser,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime,
        now: datetime,
    ) -> None:
        """Create or replace the user's connection, ``active``."""

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime, now: datetime
    ) -> None: ...

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None: ...

    async def delete(self, user_id: uuid.UUID) -> bool: ...


StoreTransaction = Callable[[], AbstractAsyncContextManager[ConnectionStore]]


@dataclass(frozen=True)
class ConnectionView:
    """What the API shows of a connection: never a token or the Box user id. ``workspace_id`` and ``workspace_name`` are
    the project integration's workspace (decision B6): the enterprise id and name, or ``user:<box user id>`` and the
    user's name for a personal account."""

    status: Literal["active", "expired"]
    name: str
    enterprise_name: str | None
    workspace_id: str
    workspace_name: str
    connected_at: datetime
    last_used_at: datetime

    @classmethod
    def of(cls, row: StoredConnection) -> ConnectionView:
        status: Literal["active", "expired"] = "expired" if row.status == EXPIRED else "active"
        if row.enterprise_id is not None:
            workspace_id, workspace_name = row.enterprise_id, row.enterprise_name or row.name
        else:
            workspace_id, workspace_name = f"{PERSONAL_PREFIX}{row.box_user_id}", row.name
        return cls(
            status, row.name, row.enterprise_name, workspace_id, workspace_name, row.connected_at, row.last_used_at
        )


def _refused(code: BoxCode) -> BoxCode:
    """The code of a refused refresh: a dead grant needs a reconnect; a server or network problem is
    ``box_unreachable``; anything else (a wrong client secret) is ``box_auth_failed``."""

    if code is BoxCode.GRANT_INVALID:
        return BoxCode.RECONNECT_REQUIRED
    if code is BoxCode.UNREACHABLE:
        return BoxCode.UNREACHABLE
    return BoxCode.AUTH_FAILED


class BoxConnections:
    """Connect, disconnect, read and use the users' Box connections."""

    def __init__(
        self,
        settings: BoxSettings,
        *,
        store: StoreTransaction,
        client: BoxClient | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client or BoxClient(settings.client_id, settings.client_secret, settings.callback_url)
        self._cipher = TokenCipher(settings.token_key)
        self._states = StateSigner(settings.token_key, _STATE_CONTEXT, clock=clock)
        self._store = store
        self._now = now
        self._tokens = UserTokens(monotonic=monotonic)
        # The grant each cached access token came from: (Box user id, connected_at) of the row. Another process can
        # disconnect or reconnect the user; ``access_token`` compares the row with this before it serves the cache.
        self._grants: dict[uuid.UUID, tuple[str, datetime]] = {}

    @property
    def client(self) -> BoxClient:
        """The Box client (the shared transport), for ``box.content.BoxContent``."""

        return self._client

    # The connect flow ----------------------------------------------------------------------------------------------

    def authorize_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """Box's sign-in page for the app, with a ``state`` bound to the user and ``return_to`` (10 minutes)."""

        return self._client.authorize_url(self._states.sign(str(user_id), return_to, AUTHORIZE))

    async def connect(self, user_id: uuid.UUID, *, code: str, state: str) -> tuple[ConnectionView, str]:
        """Check ``state``, exchange ``code``, read the account and store the connection (replacing one the user
        had); the connection and ``return_to``. ``BoxError`` names what failed."""

        try:
            return_to = self._states.verify(state, str(user_id), AUTHORIZE)
        except StateInvalidError:
            raise BoxError(BoxCode.STATE_INVALID) from None
        grant = await self._client.exchange(code)
        try:
            user = await self._client.user(grant.access_token)
        except BoxError as error:
            unreachable = error.code is BoxCode.UNREACHABLE
            raise BoxError(BoxCode.UNREACHABLE if unreachable else BoxCode.CODE_INVALID) from None
        now = self._now()
        async with self._tokens.lock(user_id), self._store() as store:
            await store.save(
                user_id,
                user,
                refresh_token=self._cipher.encrypt(grant.refresh_token),
                refresh_expires_at=self._refresh_expiry(now),
                now=now,
            )
        self._grants[user_id] = (user.id, now)
        self._tokens.remember(user_id, grant)
        log_event(logger, INFO, "box.connected", user_id=str(user_id))
        stored = StoredConnection(
            user_id, user.id, user.name, user.enterprise_id, user.enterprise_name, b"", ACTIVE, now, now
        )
        return ConnectionView.of(stored), return_to

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection, drop its access token and revoke the grant at Box (best effort)."""

        try:
            token: str | None = await self.access_token(user_id)  # for the revoke; a dead connection has none
        except BoxError:
            token = None
        async with self._tokens.lock(user_id):
            async with self._store() as store:
                deleted = await store.delete(user_id)
            self._forget(user_id)
        revoked = await self._client.revoke(SecretStr(token)) if token is not None else False
        log_event(logger, INFO, "box.disconnected", user_id=str(user_id), deleted=deleted, revoked=revoked)

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        async with self._store() as store:
            row = await store.get(user_id)
        return None if row is None else ConnectionView.of(row)

    # Use -----------------------------------------------------------------------------------------------------------

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else a refreshed one; ``BoxError`` with ``box_not_connected``,
        ``box_reconnect_required``, ``box_unreachable`` or ``box_auth_failed``.

        The row is read first, every time: a connection that another process deleted, expired or replaced (a new
        connect) is not served from this process's cache."""

        async with self._store() as store:
            row = await store.get(user_id)
        if row is None:
            self._forget(user_id)
            raise BoxError(BoxCode.NOT_CONNECTED)
        if row.status != ACTIVE:
            self._forget(user_id)
            raise BoxError(BoxCode.RECONNECT_REQUIRED)
        if self._grants.get(user_id) != (row.box_user_id, row.connected_at):
            self._forget(user_id)  # the cached token, if any, belongs to an older grant: refresh from this row
        return await self._tokens.access_token(user_id, lambda: self._rotate_or_raise(user_id))

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token; ``used`` is a token Box refused, never handed out again."""

        return await self._tokens.refresh(user_id, lambda: self._rotate_or_raise(user_id), used=used)

    async def call[T](self, user_id: uuid.UUID, request: Callable[[SecretStr], Awaitable[T]]) -> T:
        """``request`` with the user's access token; ``box_token_refused`` (a 401) refreshes once and asks once more."""

        token = await self.access_token(user_id)
        try:
            return await request(SecretStr(token))
        except BoxError as error:
            if error.code is not BoxCode.ACCESS_REFUSED:
                raise
        try:
            return await request(SecretStr(await self.refresh(user_id, used=token)))
        except BoxError as error:
            if error.code is BoxCode.ACCESS_REFUSED:
                raise BoxError(BoxCode.AUTH_FAILED) from None  # a fresh token was refused too: a setup problem
            raise

    async def aclose(self) -> None:
        await self._tokens.aclose()
        await self._client.aclose()

    # Inside --------------------------------------------------------------------------------------------------------

    def _forget(self, user_id: uuid.UUID) -> None:
        self._tokens.forget(user_id)
        self._grants.pop(user_id, None)

    @staticmethod
    def _refresh_expiry(now: datetime) -> datetime:
        return now + timedelta(seconds=REFRESH_VALID_SECONDS)

    async def _rotate_or_raise(self, user_id: uuid.UUID) -> TokenGrant:
        """One refresh with the row locked ``FOR UPDATE``; the transaction commits the rotated refresh token (or the
        ``expired`` status) before this returns. The row is read inside the lock: another process may have rotated
        the token meanwhile, and Box refresh tokens are single use."""

        grant: TokenGrant | None = None
        refused = BoxCode.AUTH_FAILED
        expired = False
        async with self._store() as store:
            row = await store.lock(user_id)
            if row is None:
                refused = BoxCode.NOT_CONNECTED
            elif row.status != ACTIVE:
                refused = BoxCode.RECONNECT_REQUIRED
            else:
                try:
                    grant = await self._client.refresh(self._cipher.decrypt(row.refresh_token))
                except CipherError:
                    refused = BoxCode.RECONNECT_REQUIRED  # a rotated BOX_TOKEN_KEY: every user reconnects
                except BoxError as error:
                    refused = _refused(error.code)
                if grant is not None:
                    self._grants[user_id] = (row.box_user_id, row.connected_at)
                    now = self._now()
                    await store.rotate(
                        user_id,
                        refresh_token=self._cipher.encrypt(grant.refresh_token),
                        refresh_expires_at=self._refresh_expiry(now),
                        now=now,
                    )
                elif refused is BoxCode.RECONNECT_REQUIRED:
                    await store.expire(user_id, now=self._now())
                    expired = True
        if grant is not None:
            log_event(logger, INFO, "box.refreshed", user_id=str(user_id))
            return grant
        if expired:
            log_event(logger, WARNING, "box.expired", user_id=str(user_id), code=refused.value)
        raise BoxError(refused)


# The grant a refresh answers (``box.client.TokenGrant``) is re-exported so S1's tests import one module.
__all__ = [
    "ACTIVE",
    "EXPIRED",
    "BoxConnections",
    "ConnectionStore",
    "ConnectionView",
    "StoreTransaction",
    "StoredConnection",
    "TokenGrant",
]
