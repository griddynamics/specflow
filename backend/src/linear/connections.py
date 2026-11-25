"""The per-member Linear connections (plans/LINEAR/LINEAR-PLAN.md section 5, stream S1).

One ``LinearConnections`` per process, held on ``Runtime.linear``. It signs and checks ``state`` (and derives the PKCE
verifier from it, so nothing is stored between the consent and the callback), connects a member (code exchange, the
``viewer`` read), keeps each member's access token in memory until 60 s before it expires and refreshes under the shared
per-user lock (``integrations.tokens``) and the row's ``FOR UPDATE`` lock: the rotated pair is committed before the new
access token is used, even when the caller is cancelled meanwhile. A process that finds a still-good access token in the
row (another replica or the worker refreshed it) uses it and does not rotate again. ``invalid_grant`` or a pair that no
longer decrypts marks the connection ``expired``; it is never refreshed again until the member reconnects. The rows
come and go through the ``ConnectionStore`` port (``db.linear`` implements it). Logs carry the user id and codes only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal, Protocol

from integrations.cipher import CipherError, TokenCipher
from integrations.state import StateInvalidError, StateSigner
from integrations.tokens import EARLY_EXPIRY_SECONDS, UserTokens
from linear.errors import LinearError, LinearInvalidError, LinearUnauthorizedError, LinearUnavailableError
from linear.gateway import LinearGateway
from linear.http import HttpLinearGateway
from linear.models import Viewer
from linear.oauth import LinearOAuthClient, TokenGrant
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import LinearSettings

logger = get_logger("linear.connections")

ACTIVE: Final = "active"
EXPIRED: Final = "expired"
_STATE_CONTEXT: Final = b"specflow linear oauth state"
_PKCE_CONTEXT: Final = b"specflow linear oauth pkce"


class ConnectFailure(StrEnum):
    """Why a connect was refused; the values are the API codes."""

    STATE_INVALID = "linear_state_invalid"
    CODE_INVALID = "linear_code_invalid"
    UNREACHABLE = "linear_unreachable"
    AUTH_FAILED = "linear_auth_failed"


class LinearConnectError(Exception):
    """The connect was refused; ``code`` is the closed reason and ``str(e)`` its value (no Linear text,
    code or state)."""

    def __init__(self, code: ConnectFailure) -> None:
        super().__init__(code.value)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class StoredConnection:
    """A ``linear_connections`` row as the port hands it over; both tokens are Fernet ciphertext."""

    user_id: uuid.UUID
    linear_user_id: str
    linear_user_name: str
    organization_id: str
    organization_name: str
    organization_url_key: str
    scopes: str
    access_token: bytes
    refresh_token: bytes
    expires_at: datetime
    status: str
    connected_at: datetime
    last_used_at: datetime


class ConnectionStore(Protocol):
    """The connection rows inside one transaction (``db.linear.ConnectionRows``)."""

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None: ...

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        """The row locked ``FOR UPDATE`` until the transaction ends."""

    async def save(
        self,
        user_id: uuid.UUID,
        viewer: Viewer,
        *,
        scopes: str,
        tokens: tuple[bytes, bytes],
        expires_at: datetime,
        now: datetime,
    ) -> None:
        """Create or replace the member's connection, ``active``; ``tokens`` is (access, refresh) ciphertext."""

    async def rotate(
        self, user_id: uuid.UUID, *, tokens: tuple[bytes, bytes], expires_at: datetime, now: datetime
    ) -> None: ...

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None: ...

    async def delete(self, user_id: uuid.UUID) -> bool: ...


StoreTransaction = Callable[[], AbstractAsyncContextManager[ConnectionStore]]


@dataclass(frozen=True)
class ConnectionView:
    """What the API shows of a connection: never a token or the Linear user id of the API answer."""

    status: Literal["active", "expired"]
    linear_user_id: str
    user_name: str
    organization_id: str
    organization_name: str
    organization_url_key: str
    connected_at: datetime
    last_used_at: datetime

    @classmethod
    def of(cls, row: StoredConnection) -> ConnectionView:
        status: Literal["active", "expired"] = ACTIVE if row.status == ACTIVE else EXPIRED
        return cls(
            status,
            row.linear_user_id,
            row.linear_user_name,
            row.organization_id,
            row.organization_name,
            row.organization_url_key,
            row.connected_at,
            row.last_used_at,
        )


class LinearConnections:
    """Connect, disconnect, read and use the members' Linear connections."""

    def __init__(
        self,
        settings: LinearSettings,
        *,
        store: StoreTransaction,
        client: LinearOAuthClient | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client or LinearOAuthClient(settings.client_id, settings.client_secret, settings.callback_url)
        self._cipher = TokenCipher(settings.token_key)
        self._states = StateSigner(settings.token_key, _STATE_CONTEXT, clock=clock)
        key = settings.token_key.get_secret_value().encode("ascii")
        self._pkce_key = hmac.new(key, _PKCE_CONTEXT, hashlib.sha256).digest()
        self._store = store
        self._now = now
        self._tokens = UserTokens(monotonic=monotonic)
        # The grant each cached access token came from: (Linear user id, connected_at) of the row. Another process can
        # disconnect or reconnect the member; ``access_token`` compares the row with this before it serves the cache.
        self._grants: dict[uuid.UUID, tuple[str, datetime]] = {}

    # The connect flow ----------------------------------------------------------------------------------------------

    def _verifier(self, state: str) -> str:
        """The PKCE verifier of a ``state``: an HMAC only this server can compute, so it is not stored."""

        return _b64(hmac.new(self._pkce_key, state.encode("ascii"), hashlib.sha256).digest())

    def authorize_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """Linear's consent page for the app, with a ``state`` bound to the member and ``return_to`` (10 minutes) and
        the PKCE challenge of that state."""

        state = self._states.sign(str(user_id), return_to)
        challenge = _b64(hashlib.sha256(self._verifier(state).encode("ascii")).digest())
        return self._client.authorize_url(state, challenge)

    async def connect(self, user_id: uuid.UUID, *, code: str, state: str) -> tuple[ConnectionView, str]:
        """Check ``state``, exchange ``code`` (with the PKCE verifier), read the viewer and store the connection
        (replacing one the member had); the connection and ``return_to``. ``LinearConnectError`` names what failed."""

        try:
            return_to = self._states.verify(state, str(user_id))
        except StateInvalidError:
            raise LinearConnectError(ConnectFailure.STATE_INVALID) from None
        try:
            grant = await self._client.exchange(code, self._verifier(state))
            viewer = await self._client.viewer(grant.access_token)
        except LinearUnavailableError:
            raise LinearConnectError(ConnectFailure.UNREACHABLE) from None
        except (LinearUnauthorizedError, LinearInvalidError):
            raise LinearConnectError(ConnectFailure.CODE_INVALID) from None
        except LinearError:
            raise LinearConnectError(ConnectFailure.AUTH_FAILED) from None
        now = self._now()
        async with self._tokens.lock(user_id), self._store() as store:
            await store.save(
                user_id,
                viewer,
                scopes=grant.scope,
                tokens=self._seal(grant),
                expires_at=now + timedelta(seconds=grant.expires_in),
                now=now,
            )
        self._grants[user_id] = (viewer.id, now)
        self._tokens.remember(user_id, grant)
        log_event(logger, INFO, "linear.connected", user_id=str(user_id))
        view = ConnectionView(
            ACTIVE,
            viewer.id,
            viewer.name,
            viewer.organization.id,
            viewer.organization.name,
            viewer.organization.url_key,
            now,
            now,
        )
        return view, return_to

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection, drop the cached token and revoke the pair at Linear (best effort)."""

        async with self._tokens.lock(user_id):
            async with self._store() as store:
                row = await store.lock(user_id)
                deleted = row is not None and await store.delete(user_id)
            self._forget(user_id)
        revoked = 0
        if row is not None:
            revoked = await self._revoke(row)
        log_event(logger, INFO, "linear.disconnected", user_id=str(user_id), deleted=deleted, revoked=revoked)

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        async with self._store() as store:
            row = await store.get(user_id)
        return None if row is None else ConnectionView.of(row)

    # Use -----------------------------------------------------------------------------------------------------------

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else the row's (another process refreshed it) or a refreshed one.
        ``LinearUnauthorizedError`` when the member has no active connection (reconnect), ``LinearUnavailableError``
        when Linear cannot be reached.

        The row is read first, every time: a connection that another process deleted, expired or replaced (a new
        connect) is not served from this process's cache."""

        async with self._store() as store:
            row = await store.get(user_id)
        if row is None or row.status != ACTIVE:
            self._forget(user_id)
            raise LinearUnauthorizedError
        if self._grants.get(user_id) != (row.linear_user_id, row.connected_at):
            self._forget(user_id)  # the cached token, if any, belongs to an older grant
        return await self._tokens.access_token(user_id, lambda: self._rotate(user_id, used=None))

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token; ``used`` is a token Linear refused, never handed out again."""

        return await self._tokens.refresh(user_id, lambda: self._rotate(user_id, used=used), used=used)

    def token_provider(self, user_id: uuid.UUID) -> Callable[[], Awaitable[str]]:
        """What ``HttpLinearGateway`` awaits before every call: a fresh access token of the member."""

        async def token() -> str:
            return await self.access_token(user_id)

        return token

    def gateway(self, user_id: uuid.UUID) -> LinearGateway:
        """A ``LinearGateway`` that acts as the member (their token, refreshed on demand)."""

        return HttpLinearGateway(self.token_provider(user_id))

    async def aclose(self) -> None:
        await self._tokens.aclose()
        await self._client.aclose()

    # Inside --------------------------------------------------------------------------------------------------------

    def _forget(self, user_id: uuid.UUID) -> None:
        self._tokens.forget(user_id)
        self._grants.pop(user_id, None)

    def _seal(self, grant: TokenGrant) -> tuple[bytes, bytes]:
        return self._cipher.encrypt(grant.access_token), self._cipher.encrypt(grant.refresh_token)

    async def _revoke(self, row: StoredConnection) -> int:
        """Revoke the refresh and the access token at Linear; how many it confirmed. A token that no longer
        decrypts, or that Linear refuses or cannot be reached for, is skipped."""

        revoked = 0
        for ciphertext, hint in ((row.refresh_token, "refresh_token"), (row.access_token, "access_token")):
            try:
                await self._client.revoke(self._cipher.decrypt(ciphertext), hint=hint)
            except (CipherError, LinearError):
                continue
            revoked += 1
        return revoked

    async def _rotate(self, user_id: uuid.UUID, *, used: str | None) -> TokenGrant:
        """The grant to serve: the row's access token when it is still good and not ``used``, else one refresh with the
        row locked ``FOR UPDATE``; the transaction commits the rotated pair (or the ``expired`` status) before this
        returns."""

        async with self._store() as store:
            row = await store.lock(user_id)
            step = await self._step(store, user_id, row, used)
        if isinstance(step, LinearError):
            raise step
        if row is not None:
            self._grants[user_id] = (row.linear_user_id, row.connected_at)
        return step

    async def _step(
        self, store: ConnectionStore, user_id: uuid.UUID, row: StoredConnection | None, used: str | None
    ) -> TokenGrant | LinearError:
        if row is None or row.status != ACTIVE:
            return LinearUnauthorizedError()
        now = self._now()
        try:
            access, refresh = self._cipher.decrypt(row.access_token), self._cipher.decrypt(row.refresh_token)
        except CipherError:  # a rotated LINEAR_TOKEN_KEY: every member reconnects
            return await self._expired(store, user_id, LinearUnauthorizedError())
        left = int((row.expires_at - now).total_seconds())
        if left > EARLY_EXPIRY_SECONDS and access.get_secret_value() != used:
            return TokenGrant(access, refresh, left, row.scopes)  # another process refreshed it meanwhile
        try:
            grant = await self._client.refresh(refresh)
        except LinearUnauthorizedError as error:  # invalid_grant: the pair is dead
            return await self._expired(store, user_id, error)
        except LinearError as error:  # unreachable, or the app's credentials refused: the connection stays active
            log_event(logger, WARNING, "linear.refresh_failed", user_id=str(user_id), code=error.code.value)
            return LinearUnavailableError()
        await store.rotate(
            user_id, tokens=self._seal(grant), expires_at=now + timedelta(seconds=grant.expires_in), now=now
        )
        log_event(logger, INFO, "linear.refreshed", user_id=str(user_id))
        return grant

    async def _expired(self, store: ConnectionStore, user_id: uuid.UUID, error: LinearError) -> LinearError:
        await store.expire(user_id, now=self._now())
        log_event(logger, WARNING, "linear.expired", user_id=str(user_id))
        return error
