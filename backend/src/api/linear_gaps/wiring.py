"""Runtime hook of stream S5: register the Linear gap channel. FOUNDATION STUB: a no-op until S5 fills it."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any


def add_linear_gaps(runtime: Any, stack: AsyncExitStack) -> None:
    """S5: when ``runtime.linear`` and ``runtime.gaps`` are set, build the poster, assign the channel to
    ``runtime.linear_gaps``, ``runtime.gaps.add_notice(...)`` it and push its ``aclose`` on ``stack``."""

    del runtime, stack
