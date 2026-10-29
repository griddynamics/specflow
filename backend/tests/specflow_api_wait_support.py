"""Wait for a condition, not for a duration (no tests in here)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Final

POLL_SECONDS: Final = 0.01
DEFAULT_WAIT_SECONDS: Final = 5.0


async def wait_until(condition: Callable[[], bool], seconds: float = DEFAULT_WAIT_SECONDS) -> None:
    """Poll ``condition`` until it holds; fail when ``seconds`` pass first."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not condition():
        assert loop.time() < deadline, "condition not reached"
        await asyncio.sleep(POLL_SECONDS)
