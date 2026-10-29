from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from llm.result import AgentResult

Failure = AgentResult | Exception


class FailureDisposition(StrEnum):
    """Whether a failed attempt may be retried."""

    RETRY = "retry"
    TERMINAL = "terminal"


ErrorClassifier = Callable[
    [Failure],
    FailureDisposition | Awaitable[FailureDisposition],
]


@dataclass(frozen=True, slots=True)
class RetryPlan:
    """Delay and updated crash streak for one retry."""

    delay: float
    crash_streak: int
    saved_progress: bool


@dataclass(frozen=True, slots=True, init=False)
class CrashBackoffPolicy:
    """Plan retries from consecutive no-progress failures."""

    delays: tuple[float, ...]

    def __init__(
        self,
        delays: Sequence[float] = (60, 180, 300, 600, 1800, 3600),
    ) -> None:
        values: list[float] = []
        for delay in delays:
            if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                raise TypeError("crash backoff delays must be numbers")
            if not math.isfinite(delay):
                raise ValueError("crash backoff delays must be finite")
            if delay < 0:
                raise ValueError("crash backoff delays must not be negative")
            values.append(float(delay))
        if not values:
            raise ValueError("crash backoff delays must not be empty")
        object.__setattr__(self, "delays", tuple(values))

    def next_retry(
        self,
        *,
        crash_streak: int,
        saved_progress: bool,
    ) -> RetryPlan | None:
        if isinstance(crash_streak, bool) or not isinstance(crash_streak, int):
            raise TypeError("crash_streak must be an integer")
        if crash_streak < 0:
            raise ValueError("crash_streak must not be negative")

        next_streak = 0 if saved_progress else crash_streak + 1
        if next_streak > len(self.delays):
            return None
        delay_index = 0 if saved_progress else next_streak - 1
        return RetryPlan(
            delay=self.delays[delay_index],
            crash_streak=next_streak,
            saved_progress=saved_progress,
        )
