"""The request budget of Notion (docs/notion/oauth.md "Limits").

Notion limits each connection (one ``bot_id``) to 180 requests a minute on a Free or Plus workspace. One
``RateLimiter`` per bot id lives in the api process and import and export share it, so one job cannot spend the budget
of the others; ``NOTION_REQUESTS_PER_MINUTE`` (default 150) is its rate. A ``429`` answer's ``Retry-After`` is reported
with ``hold``. ``WaitBudget`` caps what one pass waits in all (60 s, as GitHub Projects).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Final

from notion.reasons import NotionCode, NotionError

PASS_WAIT_SECONDS: Final = 60.0
WINDOW_SECONDS: Final = 60.0


class RateLimiter:
    """A sliding window of ``per_minute`` requests; ``acquire`` waits for a free place.

    ``clock`` and ``sleep`` are for tests. Waiters are served in order: one holds the lock while it waits."""

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._per_minute = max(1, per_minute)
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()  # when the requests of the last window left
        self._held_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, budget: WaitBudget | None = None) -> None:
        """Wait for a request place; the wait counts against ``budget``, which raises ``NotionError`` with
        ``notion_rate_limited`` when it is spent."""

        async with self._lock:
            while True:
                now = self._clock()
                while self._starts and self._starts[0] <= now - WINDOW_SECONDS:
                    self._starts.popleft()
                wait = max(0.0, self._held_until - now)
                if len(self._starts) >= self._per_minute:
                    wait = max(wait, self._starts[0] + WINDOW_SECONDS - now)
                if wait <= 0:
                    self._starts.append(now)
                    return
                if budget is not None:
                    budget.spend(wait)
                await self._sleep(wait)

    def hold(self, seconds: float) -> None:
        """Notion answered 429 with ``Retry-After``: no request leaves before ``seconds`` have passed."""

        self._held_until = max(self._held_until, self._clock() + max(0.0, seconds))


class WaitBudget:
    """Seconds a pass may still wait in all."""

    def __init__(self, seconds: float = PASS_WAIT_SECONDS) -> None:
        self.remaining = seconds

    def spend(self, seconds: float) -> None:
        """Take ``seconds`` from the budget; ``NotionError`` ``notion_rate_limited`` when it does not cover them."""

        if seconds > self.remaining:
            raise NotionError(NotionCode.RATE_LIMITED)
        self.remaining -= seconds


class RateLimiters:
    """The limiters of the process, one per ``bot_id``."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        self._by_bot: dict[str, RateLimiter] = {}

    def for_bot(self, bot_id: str) -> RateLimiter:
        """The limiter of ``bot_id``, made on first use."""

        limiter = self._by_bot.get(bot_id)
        if limiter is None:
            limiter = self._by_bot[bot_id] = RateLimiter(self._per_minute)
        return limiter
