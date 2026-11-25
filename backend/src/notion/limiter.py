"""The request budget of Notion (docs/notion/oauth.md "Limits"). Stream A fills this file.

Notion limits each connection (one ``bot_id``) to 180 requests a minute on a Free or Plus workspace. One
``RateLimiter`` per bot id lives in the api process and import and export share it, so one job cannot spend the budget
of the others; ``NOTION_REQUESTS_PER_MINUTE`` (default 150) is its rate. A ``429`` answer's ``Retry-After`` is reported
with ``hold``. ``WaitBudget`` caps what one pass waits in all (60 s, as GitHub Projects).
"""

from __future__ import annotations

from typing import Final

PASS_WAIT_SECONDS: Final = 60.0


class RateLimiter:
    """A sliding window of ``per_minute`` requests; ``acquire`` waits for a free place."""

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute

    async def acquire(self, budget: WaitBudget | None = None) -> None:
        """Wait for a request place; the wait counts against ``budget``, which raises ``NotionError`` with
        ``notion_rate_limited`` when it is spent."""

        raise NotImplementedError("stream A fills RateLimiter.acquire")

    def hold(self, seconds: float) -> None:
        """Notion answered 429 with ``Retry-After``: no request leaves before ``seconds`` have passed."""

        raise NotImplementedError("stream A fills RateLimiter.hold")


class WaitBudget:
    """Seconds a pass may still wait in all."""

    def __init__(self, seconds: float = PASS_WAIT_SECONDS) -> None:
        self.remaining = seconds

    def spend(self, seconds: float) -> None:
        """Take ``seconds`` from the budget; ``NotionError`` ``notion_rate_limited`` when it does not cover them."""

        raise NotImplementedError("stream A fills WaitBudget.spend")


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
