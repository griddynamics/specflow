"""Waiting for a throttled provider, shared by the OAuth integration clients (``box.client``, ``dropbox.client``;
decision DB18 in plans/DROPBOX/DROPBOX-PLAN.md): the wait budget of one pass and the ``Retry-After`` header.

A client asks again after a ``429`` or a 5xx: it waits the seconds of ``Retry-After``, else ``BACKOFF_SECONDS`` by
attempt, and only while the pass's ``WaitBudget`` holds them. ``github.client`` keeps its own budget (not widened).
No I/O beyond reading a header, no log.
"""

from __future__ import annotations

from typing import Final

import httpx

WAIT_BUDGET_SECONDS: Final = 60  # the most one pass (one ``WaitBudget``) waits for throttling in all
BACKOFF_SECONDS: Final = (1.0, 2.0, 4.0)  # waited by attempt when the provider sends no ``Retry-After``


class WaitBudget:
    """The seconds a pass may still wait for ``Retry-After`` answers, in all: one throttled import must not hold its
    slot for long. A wait that does not fit is not made (the read ends with the client's ``unreachable`` code)."""

    def __init__(self, seconds: float = WAIT_BUDGET_SECONDS) -> None:
        self.left = seconds

    def take(self, wanted: float) -> float | None:
        """``wanted`` seconds when they fit in what is left (and spends them), else ``None``."""

        if wanted > self.left:
            return None
        self.left -= wanted
        return wanted


def retry_after(response: httpx.Response) -> float | None:
    """The seconds a ``Retry-After`` header asks to wait; ``None`` when it is absent or not a number of seconds."""

    try:
        return max(0.0, float(response.headers["retry-after"]))
    except (KeyError, ValueError):
        return None


def backoff(response: httpx.Response, attempt: int) -> float:
    """The seconds to wait before the next try: ``Retry-After``, else ``BACKOFF_SECONDS[attempt]`` (the last value for
    a later attempt)."""

    wanted = retry_after(response)
    return BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)] if wanted is None else wanted
