"""The per-user GitHub connections (docs/github/oauth.md "Connect flow", "Tokens").

One ``GitHubConnections`` per process: the api holds one on ``Runtime.github``, the agents worker one of its own. It
signs and checks ``state`` (purpose ``authorize`` for the sign-in, ``install`` for the app installation), connects a
user (code exchange, ``GET /user``), keeps each user's access token in memory until 60 s before it expires, and
refreshes under a per-user lock and the row's ``FOR UPDATE`` lock: the rotated refresh token is committed before the
new access token is used (GitHub disables the old one at once). ``bad_refresh_token`` or a refresh token that no longer
decrypts marks the connection ``expired``; it is never refreshed again until the user reconnects. The rows come and go
through the ``ConnectionStore`` port (``db.github`` implements it). Logs carry the user id and codes only.
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

from github.client import GitHubClient, GitHubUser, Repository, TokenGrant, install_url
from github.codes import GitHubCode, GitHubError
from integrations.cipher import CipherError, TokenCipher
from integrations.state import StateInvalidError, StateSigner
from integrations.tokens import UserTokens
from logger.logger import INFO, WARNING, get_logger, log_event
from settings import GitHubSettings

logger = get_logger("github.connections")

ACTIVE: Final = "active"
EXPIRED: Final = "expired"
AUTHORIZE: Final = "authorize"
INSTALL: Final = "install"
_STATE_CONTEXT: Final = b"specflow github oauth state"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StoredConnection:
    """A ``github_connections`` row as the port hands it over; the refresh token is Fernet ciphertext."""

    user_id: uuid.UUID
    github_user_id: int
    login: str
    refresh_token: bytes
    status: str
    connected_at: datetime
    last_used_at: datetime


class ConnectionStore(Protocol):
    """The connection rows inside one transaction (``db.github.ConnectionRows``)."""

    async def get(self, user_id: uuid.UUID) -> StoredConnection | None: ...

    async def lock(self, user_id: uuid.UUID) -> StoredConnection | None:
        """The row locked ``FOR UPDATE`` until the transaction ends."""

    async def save(
        self,
        user_id: uuid.UUID,
        user: GitHubUser,
        *,
        refresh_token: bytes,
        refresh_expires_at: datetime | None,
        now: datetime,
    ) -> None:
        """Create or replace the user's connection, ``active``."""

    async def rotate(
        self, user_id: uuid.UUID, *, refresh_token: bytes, refresh_expires_at: datetime | None, now: datetime
    ) -> None: ...

    async def expire(self, user_id: uuid.UUID, *, now: datetime) -> None: ...

    async def delete(self, user_id: uuid.UUID) -> bool: ...


StoreTransaction = Callable[[], AbstractAsyncContextManager[ConnectionStore]]


@dataclass(frozen=True)
class ConnectionView:
    """What the API shows of a connection: never a token or the GitHub user id."""

    status: Literal["active", "expired"]
    login: str
    connected_at: datetime
    last_used_at: datetime

    @classmethod
    def of(cls, row: StoredConnection) -> ConnectionView:
        status: Literal["active", "expired"] = "expired" if row.status == EXPIRED else "active"
        return cls(status, row.login, row.connected_at, row.last_used_at)


def _refused(code: GitHubCode) -> GitHubCode:
    """The code of a refused refresh: a dead grant needs a reconnect; a server or network problem is
    ``github_unreachable``; anything else (a wrong client secret) is ``github_auth_failed``."""

    if code is GitHubCode.GRANT_INVALID:
        return GitHubCode.RECONNECT_REQUIRED
    if code is GitHubCode.UNREACHABLE:
        return GitHubCode.UNREACHABLE
    return GitHubCode.AUTH_FAILED


class GitHubConnections:
    """Connect, disconnect, read and use the users' GitHub connections."""

    def __init__(
        self,
        settings: GitHubSettings,
        *,
        store: StoreTransaction,
        client: GitHubClient | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client or GitHubClient(settings.client_id, settings.client_secret, settings.callback_url)
        self._cipher = TokenCipher(settings.token_key)
        self._states = StateSigner(settings.token_key, _STATE_CONTEXT, clock=clock)
        self._slug = settings.slug
        self._store = store
        self._now = now
        self._tokens = UserTokens(monotonic=monotonic)
        # The grant each cached access token came from: (GitHub user id, connected_at) of the row. Another process
        # (an api replica, the agents worker) can disconnect or reconnect the user; ``access_token`` compares the
        # row with this before it hands out a cached token.
        self._grants: dict[uuid.UUID, tuple[int, datetime]] = {}

    # The connect flow ----------------------------------------------------------------------------------------------

    def authorize_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """GitHub's sign-in page for the app, with a ``state`` bound to the user and ``return_to`` (10 minutes)."""

        return self._client.authorize_url(self._states.sign(str(user_id), return_to, AUTHORIZE))

    def install_url(self, user_id: uuid.UUID, return_to: str) -> str:
        """The app's install page; GitHub hands the ``state`` to the setup URL after the install or an update."""

        return install_url(self._slug, self._states.sign(str(user_id), return_to, INSTALL))

    def check_setup(self, user_id: uuid.UUID, state: str) -> str:
        """The ``return_to`` of an install ``state`` of this user; else ``STATE_INVALID``. The ``installation_id``
        GitHub adds to the setup URL is never read: anyone can forge it."""

        return self._verify(state, user_id, INSTALL)

    def _verify(self, state: str, user_id: uuid.UUID, purpose: str) -> str:
        try:
            return self._states.verify(state, str(user_id), purpose)
        except StateInvalidError:
            raise GitHubError(GitHubCode.STATE_INVALID) from None

    async def connect(self, user_id: uuid.UUID, *, code: str, state: str) -> tuple[ConnectionView, str]:
        """Check ``state``, exchange ``code``, read the account and store the connection (replacing one the user
        had); the connection and ``return_to``. ``GitHubError`` names what failed."""

        return_to = self._verify(state, user_id, AUTHORIZE)
        grant = await self._client.exchange(code)
        try:
            user = await self._client.user(grant.access_token)
        except GitHubError as error:
            unreachable = error.code is GitHubCode.UNREACHABLE
            raise GitHubError(GitHubCode.UNREACHABLE if unreachable else GitHubCode.CODE_INVALID) from None
        now = self._now()
        async with self._tokens.lock(user_id), self._store() as store:
            await store.save(
                user_id,
                user,
                refresh_token=self._cipher.encrypt(grant.refresh_token),
                refresh_expires_at=self._refresh_expiry(grant, now),
                now=now,
            )
        self._grants[user_id] = (user.id, now)
        self._tokens.remember(user_id, grant)
        log_event(logger, INFO, "github.connected", user_id=str(user_id))
        return ConnectionView("active", user.login, now, now), return_to

    async def disconnect(self, user_id: uuid.UUID) -> None:
        """Delete the connection, drop its access token and remove the grant on GitHub (best effort)."""

        try:
            token: str | None = await self.access_token(user_id)  # for the revoke; a dead connection has none
        except GitHubError:
            token = None
        async with self._tokens.lock(user_id):
            async with self._store() as store:
                deleted = await store.delete(user_id)
            self._forget(user_id)
        revoked = await self._client.revoke(SecretStr(token)) if token is not None else False
        log_event(logger, INFO, "github.disconnected", user_id=str(user_id), deleted=deleted, revoked=revoked)

    async def get(self, user_id: uuid.UUID) -> ConnectionView | None:
        async with self._store() as store:
            row = await store.get(user_id)
        return None if row is None else ConnectionView.of(row)

    # Use -----------------------------------------------------------------------------------------------------------

    async def access_token(self, user_id: uuid.UUID) -> str:
        """The cached access token while it is good, else a refreshed one; ``GitHubError`` with
        ``github_not_connected``, ``github_reconnect_required``, ``github_unreachable`` or ``github_auth_failed``.

        The row is read first, every time: a connection that another process deleted, expired or replaced (a new
        connect) is not served from this process's cache."""

        async with self._store() as store:
            row = await store.get(user_id)
        if row is None:
            self._forget(user_id)
            raise GitHubError(GitHubCode.NOT_CONNECTED)
        if row.status != ACTIVE:
            self._forget(user_id)
            raise GitHubError(GitHubCode.RECONNECT_REQUIRED)
        if self._grants.get(user_id) != (row.github_user_id, row.connected_at):
            self._forget(user_id)  # the cached token, if any, belongs to an older grant: refresh from this row
        return await self._tokens.access_token(user_id, lambda: self._rotate_or_raise(user_id))

    async def refresh(self, user_id: uuid.UUID, *, used: str | None) -> str:
        """A new access token; ``used`` is a token GitHub refused, never handed out again."""

        return await self._tokens.refresh(user_id, lambda: self._rotate_or_raise(user_id), used=used)

    async def call[T](self, user_id: uuid.UUID, request: Callable[[SecretStr], Awaitable[T]]) -> T:
        """``request`` with the user's access token; a ``401`` refreshes once and asks once more."""

        token = await self.access_token(user_id)
        try:
            return await request(SecretStr(token))
        except GitHubError as error:
            if error.code is not GitHubCode.AUTH_FAILED:
                raise
        return await request(SecretStr(await self.refresh(user_id, used=token)))

    async def repositories(self, user_id: uuid.UUID) -> tuple[list[Repository], bool]:
        """The repositories the user can read through the app; true when the list was cut."""

        return await self.call(user_id, self._client.repositories)

    async def repository(self, user_id: uuid.UUID, repository_id: int) -> Repository:
        """One repository the user can read through the app; else ``github_repo_not_accessible``."""

        return await self.call(user_id, lambda token: self._client.repository(token, repository_id))

    async def aclose(self) -> None:
        await self._tokens.aclose()
        await self._client.aclose()

    # Inside --------------------------------------------------------------------------------------------------------

    def _forget(self, user_id: uuid.UUID) -> None:
        self._tokens.forget(user_id)
        self._grants.pop(user_id, None)

    @staticmethod
    def _refresh_expiry(grant: TokenGrant, now: datetime) -> datetime | None:
        return now + timedelta(seconds=grant.refresh_expires_in) if grant.refresh_expires_in else None

    async def _rotate_or_raise(self, user_id: uuid.UUID) -> TokenGrant:
        """One refresh with the row locked ``FOR UPDATE``; the transaction commits the rotated refresh token (or the
        ``expired`` status) before this returns."""

        grant: TokenGrant | None = None
        refused = GitHubCode.AUTH_FAILED
        expired = False
        async with self._store() as store:
            row = await store.lock(user_id)
            if row is None:
                refused = GitHubCode.NOT_CONNECTED
            elif row.status != ACTIVE:
                refused = GitHubCode.RECONNECT_REQUIRED
            else:
                try:
                    grant = await self._client.refresh(self._cipher.decrypt(row.refresh_token))
                except CipherError:
                    refused = GitHubCode.RECONNECT_REQUIRED  # a rotated GITHUB_TOKEN_KEY: every user reconnects
                except GitHubError as error:
                    refused = _refused(error.code)
                if grant is not None:
                    self._grants[user_id] = (row.github_user_id, row.connected_at)
                    now = self._now()
                    await store.rotate(
                        user_id,
                        refresh_token=self._cipher.encrypt(grant.refresh_token),
                        refresh_expires_at=self._refresh_expiry(grant, now),
                        now=now,
                    )
                elif refused is GitHubCode.RECONNECT_REQUIRED:
                    await store.expire(user_id, now=self._now())
                    expired = True
        if grant is not None:
            log_event(logger, INFO, "github.refreshed", user_id=str(user_id))
            return grant
        if expired:
            log_event(logger, WARNING, "github.expired", user_id=str(user_id), code=refused.value)
        raise GitHubError(refused)
