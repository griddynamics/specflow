"""The per-user access tokens of one process (docs/jira-sync/oauth.md "Tokens").

Each user's access token stays in memory until ``early_expiry`` seconds before the provider's ``expires_in``. A refresh
runs under a per-user ``asyncio.Lock``: callers that waited for the lock get the token the first one fetched. The
caller's ``rotate`` (the token call and the commit of the rotated refresh token) runs in its own task, which a
cancelled caller does not cancel (the provider already rotated the refresh token); ``aclose`` waits for it. A token
that the provider refused (``used``) is never handed out again.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol, override

from pydantic import SecretStr

EARLY_EXPIRY_SECONDS: Final = 60


class Grant(Protocol):
    """What a refresh answers; ``expires_in`` 0 or less means "use once"."""

    @property
    def access_token(self) -> SecretStr: ...

    @property
    def expires_in(self) -> int: ...


Rotate = Callable[[], Awaitable[Grant]]


@dataclass
class _Token:
    value: str
    good_until: float  # monotonic seconds


class UserTokens:
    """Cached access tokens per user id and one refresh at a time per user."""

    def __init__(
        self, *, monotonic: Callable[[], float] = time.monotonic, early_expiry: int = EARLY_EXPIRY_SECONDS
    ) -> None:
        self._monotonic = monotonic
        self._early_expiry = early_expiry
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._tokens: dict[uuid.UUID, _Token] = {}
        self._rotations: set[asyncio.Future[str]] = set()  # in flight; ``aclose`` awaits them

    @override
    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def lock(self, user_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def remember(self, user_id: uuid.UUID, grant: Grant) -> str:
        value = grant.access_token.get_secret_value()
        self._tokens[user_id] = _Token(value, self._monotonic() + grant.expires_in - self._early_expiry)
        return value

    def forget(self, user_id: uuid.UUID) -> None:
        self._tokens.pop(user_id, None)

    def _good(self, user_id: uuid.UUID, *, used: str | None) -> str | None:
        cached = self._tokens.get(user_id)
        if cached is not None and cached.value != used and cached.good_until > self._monotonic():
            return cached.value
        return None

    async def access_token(self, user_id: uuid.UUID, rotate: Rotate) -> str:
        """The cached access token while it is good, else a refreshed one."""

        return self._good(user_id, used=None) or await self.refresh(user_id, rotate, used=None)

    async def refresh(self, user_id: uuid.UUID, rotate: Rotate, *, used: str | None) -> str:
        """A new access token from ``rotate``; ``rotate``'s exception reaches every caller that waited for it."""

        async with self.lock(user_id):
            good = self._good(user_id, used=used)
            if good is not None:
                return good
            self._tokens.pop(user_id, None)
            rotation = asyncio.ensure_future(self._rotate_and_remember(user_id, rotate))
            self._rotations.add(rotation)
            rotation.add_done_callback(self._settled)
            return await asyncio.shield(rotation)

    async def aclose(self) -> None:
        await asyncio.gather(*self._rotations, return_exceptions=True)

    async def _rotate_and_remember(self, user_id: uuid.UUID, rotate: Rotate) -> str:
        return self.remember(user_id, await rotate())

    def _settled(self, rotation: asyncio.Future[str]) -> None:
        self._rotations.discard(rotation)
        if not rotation.cancelled():
            rotation.exception()  # retrieved: a caller that was cancelled meanwhile never reads it
