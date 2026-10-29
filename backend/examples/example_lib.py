from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

from logger.correlation import new_request_context
from logger.logger import ERROR, close, get_logger, log_event


async def _boxed[T](awaitable: Awaitable[T]) -> list[T]:
    return [await awaitable]


async def capture_failure[T](awaitable: Awaitable[T]) -> tuple[T | None, Exception | None]:
    """Await `awaitable`; return `(value, None)`, or `(None, error)` when it raised an `Exception`.

    Anything that is not an `Exception` (cancellation, `KeyboardInterrupt`, `SystemExit`)
    still propagates to the caller.
    """

    (outcome,) = await asyncio.gather(_boxed(awaitable), return_exceptions=True)
    if isinstance(outcome, Exception):
        return None, outcome
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome[0], None


def run_example[T](main: Callable[[], Awaitable[T]]) -> T | int:
    """Run one example's async entrypoint under a fresh request context.

    Binds a fresh `request_id` for the whole run via `new_request_context()`
    -- visible inside `main()` because `asyncio.run` copies the current
    `contextvars.Context` -- and always tears the logging pipeline down
    exactly once via `close()`, whether `main()` returns, raises, or this
    function returns early.

    Any exception `main()` raises is caught here (the outermost handler),
    logged as a single ERROR `example.failed` record with its traceback,
    and turned into a plain return value of `1` instead of propagating --
    so a script's entrypoint can simply do `raise SystemExit(run_example(main))`.

    Call this at most once per process: `close()` in the `finally` block
    shuts the logging pipeline down for good (per its own contract, it is
    never auto-restarted). A second call in the same process would run
    with a closed pipeline and silently lose its log records.
    """

    with new_request_context():
        try:
            result, error = asyncio.run(capture_failure(main()))
            if error is not None:
                log_event(get_logger(__name__), ERROR, "example.failed", exc_info=error)
                return 1
            return cast(T, result)
        finally:
            close()
