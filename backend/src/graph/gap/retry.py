"""Retries of the writes that follow a committed answer, apply, revert or dismissal (answers.md "Applying answers").

Once the graph transaction of a write has committed, the calls after it (the event log, the deletion of a replaced
answer's amendment rows, the update build request) must not be lost to a passing failure: ``retried`` calls one of
them again after a short pause. A ``GraphError`` is a decision of the callee (a closed runner, a refused request), so
it is raised at once; any other error is retried, and the last one is raised. Logs carry the call's name and the error
type only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final

from graph.core.errors import GraphError
from logger.logger import WARNING, get_logger, log_event

logger = get_logger(__name__)

RETRY_DELAYS: Final[tuple[float, ...]] = (0.2, 1.0)  # pauses before the second and the third attempt


async def retried[T](call: Callable[..., Awaitable[T]], *args: object, **kwargs: object) -> T:
    """``await call(*args, **kwargs)``, called again after each pause of ``RETRY_DELAYS`` while it raises an error
    that is not a ``GraphError``."""

    attempt = 0
    while True:
        try:
            return await call(*args, **kwargs)
        except GraphError:
            raise
        except Exception as error:
            if attempt >= len(RETRY_DELAYS):
                raise
            delay = RETRY_DELAYS[attempt]
            attempt += 1
            log_event(
                logger,
                WARNING,
                "graph.gap.write_retried",
                call=getattr(call, "__name__", "call"),
                error_type=type(error).__name__,
                delay=delay,
            )
        await asyncio.sleep(delay)
