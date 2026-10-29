"""Progress-sampling helper.

Sampling rule:
`step = max(every, ceil(total * fraction))`, with a `None` total falling
back to `step = every`. `should_log` is `True` iff `force`, `done >=
total`, or `done % step == 0` (always fires on completion, even off-step).
Bad constructor args raise `ValueError`.
"""

from __future__ import annotations

import math


class ProgressSampler:
    """Decide when to emit a sampled progress log line."""

    def __init__(self, total: int | None, *, every: int = 25, fraction: float = 0.10) -> None:
        if total is not None and total < 0:
            raise ValueError(f"total must be >= 0, got {total}")
        if every < 1:
            raise ValueError(f"every must be >= 1, got {every}")
        if not (0 < fraction <= 1):
            raise ValueError(f"fraction must be in (0, 1], got {fraction}")
        self.total = total
        self.step = every if total is None else max(every, math.ceil(total * fraction))

    def should_log(self, done: int, *, force: bool = False) -> bool:
        """Return whether a progress line should be logged at `done`."""

        if force:
            return True
        if self.total is not None and done >= self.total:
            return True  # always fire on completion
        return done % self.step == 0
