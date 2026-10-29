"""Correlation id propagation via `contextvars`.

Four ids (`request_id`, `run_id`, `session_id`, `agent_id`) travel through
`asyncio` tasks and `contextvars`-aware thread helpers without being
threaded through every function signature. The ID unification and
contextvars propagation rules apply.

`CorrelationFilter` is the one piece of this module that runs inside the
logging machinery itself, on every record, so it is held to a stricter
standard than the rest: the crash-safety rules note that `Handler.handle()`/`Logger.handle()` call
`self.filter(record)` with **no** try/except of their own -- the one stdlib
layer with no safety net. A raising filter would propagate straight into
the caller of `logger.info(...)`, so `CorrelationFilter.filter` wraps
everything in `try/except`, always returns `True`, and never mutates
shared state.
"""

from __future__ import annotations

import contextvars
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, override

CORRELATION_KEYS: tuple[str, ...] = ("request_id", "run_id", "session_id", "agent_id")

# A blank `LogRecord`'s own `__dict__` (its standard stdlib attributes) --
# but it is missing `message` and `asctime`, which only appear after
# `getMessage()`/formatting. `Logger.makeRecord` still rejects both if
# passed via `extra=`, so they are added explicitly. Load-bearing for
# `logger.logger.log_event`'s reserved-key renaming: any field whose name
# collides with a stdlib `LogRecord` attribute (or `message`/`asctime`)
# would otherwise raise `KeyError`/`TypeError` deep inside `makeRecord`.
RESERVED_ATTRS: frozenset[str] = frozenset(logging.LogRecord("x", 0, "f", 1, None, None, None).__dict__) | {
    "message",
    "asctime",
}

_UNSET: Any = object()

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("central_logger_request_id", default=None)
_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("central_logger_run_id", default=None)
_session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("central_logger_session_id", default=None)
_agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("central_logger_agent_id", default=None)

_VARS: dict[str, contextvars.ContextVar[str | None]] = {
    "request_id": _request_id_var,
    "run_id": _run_id_var,
    "session_id": _session_id_var,
    "agent_id": _agent_id_var,
}


def new_id() -> str:
    """Return a fresh 32-lowercase-hex-char id (`uuid4().hex`)."""

    return uuid.uuid4().hex


def current_request_id() -> str | None:
    return _request_id_var.get()


def current_run_id() -> str | None:
    return _run_id_var.get()


def current_session_id() -> str | None:
    return _session_id_var.get()


def current_agent_id() -> str | None:
    return _agent_id_var.get()


def current_correlation() -> dict[str, str]:
    """Every currently bound id, in `CORRELATION_KEYS` order, non-None only."""

    result: dict[str, str] = {}
    for key in CORRELATION_KEYS:
        value = _VARS[key].get()
        if value is not None:
            result[key] = value
    return result


class CorrelationToken:
    """Opaque handle returned by `bind()`; pass to `unbind()` to undo it.

    Carries one `contextvars.Token` per id this `bind()` call actually
    changed (ids left at `_UNSET` are not touched, so no token is kept for
    them). Callers must not inspect or construct these directly.
    """

    __slots__ = ("_resets",)

    def __init__(
        self,
        resets: list[tuple[contextvars.ContextVar[str | None], contextvars.Token[Any], str | None]],
    ) -> None:
        self._resets = resets

    def undo(self) -> None:
        """Restore every id this token changed, in reverse-set order. Never raises."""

        for var, tok, set_value in reversed(self._resets):
            with suppress(Exception):
                try:
                    var.reset(tok)
                except ValueError:
                    if var.get() == set_value:
                        var.set(None)


def bind(
    *,
    request_id: str | None = _UNSET,
    run_id: str | None = _UNSET,
    session_id: str | None = _UNSET,
    agent_id: str | None = _UNSET,
) -> CorrelationToken:
    """Bind the given ids in the current context; leave the rest unchanged.

    An id not passed (default `_UNSET`) is left exactly as it is. Passing
    `None` explicitly *clears* that id for the duration of the binding
    (used by `new_request_context` to blank `run_id`/`session_id`/
    `agent_id` inside a fresh request).
    """

    requested = {
        "request_id": request_id,
        "run_id": run_id,
        "session_id": session_id,
        "agent_id": agent_id,
    }
    resets: list[tuple[contextvars.ContextVar[str | None], contextvars.Token[Any], str | None]] = []
    for key, value in requested.items():
        if value is _UNSET:
            continue
        var = _VARS[key]
        token = var.set(value)
        resets.append((var, token, value))
    return CorrelationToken(resets)


def unbind(token: CorrelationToken) -> None:
    """Reverse a `bind()` call, restoring each id in reverse-set order.

    Never raises. `contextvars.Token.reset()` raises `ValueError` if the
    token was issued in a different `Context` than the one it is reset in
    (e.g. the binding crossed a thread/task boundary); in that case, fall
    back to clearing the var, but only if its current value still equals
    what this `bind()` set -- otherwise something else has legitimately
    changed it since, and stomping on that would be its own bug.
    """

    token.undo()


@contextmanager
def bound(**ids: str | None) -> Iterator[None]:
    """Context manager form of `bind()`/`unbind()`."""

    token = bind(**ids)
    try:
        yield
    finally:
        unbind(token)


@contextmanager
def new_request_context(request_id: str | None = None) -> Iterator[str]:
    """Start a fresh logical request: a new (or given) `request_id`.

    `run_id`/`session_id`/`agent_id` are cleared for the duration of the
    block -- a new request has not yet picked up a run/session/agent of its
    own -- and every id is restored to its prior value on exit, including
    when the block raises.
    """

    resolved_request_id = request_id if request_id is not None else new_id()
    token = bind(request_id=resolved_request_id, run_id=None, session_id=None, agent_id=None)
    try:
        yield resolved_request_id
    finally:
        unbind(token)


def run_in_context[T](fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> Callable[[], T]:
    """Snapshot the caller's contextvars now; replay `fn` under them later.

    `contextvars.copy_context()` runs at the time *this* function is
    called (in the calling thread/task), not lazily inside the returned
    callable -- a new OS thread starts with a fresh, empty top-level
    context, so capturing the snapshot only when the zero-arg callable is
    finally invoked (potentially already inside that new thread) would be
    too late to carry anything over.

    The returned callable is reusable and thread-safe: each invocation
    runs `fn` inside a *fresh copy* of that snapshot (`ctx.copy().run(...)`)
    rather than the shared snapshot `Context` object itself. A
    `contextvars.Context` can only be entered by one `run()` call at a
    time -- reusing it directly would raise `RuntimeError: cannot enter
    context: ... is already entered` the moment two invocations overlap
    (concurrently from different threads, or even a second sequential call
    while the first is still technically "entered" on a slow path) -- and,
    separately, any contextvar mutation `fn` makes during one invocation
    would persist into the *next* invocation, since both would otherwise
    run in that same mutable `Context` object. `ctx.copy()` sidesteps both:
    every call gets its own independent copy of the original snapshot, so
    concurrent calls never collide and no invocation can leak state into
    another.

    Usage: `threading.Thread(target=run_in_context(work, x))`.
    """

    ctx = contextvars.copy_context()

    def _runner() -> T:
        return ctx.copy().run(fn, *args, **kwargs)

    return _runner


class CorrelationFilter(logging.Filter):
    """Injects the currently bound correlation ids onto every log record.

    Total, per crash_safety.md §3: always returns `True` (a filter that
    returns `False` would drop the record; this filter's job is only to
    annotate, never to gate), wraps everything in `try/except` since it is
    the one stdlib layer with no safety net of its own, and never mutates
    anything beyond the record it was given.
    """

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        with suppress(Exception):
            for key, value in current_correlation().items():
                if key not in RESERVED_ATTRS:
                    setattr(record, key, value)
        return True


def _reset_for_tests() -> None:
    """Test-only: set all 4 correlation contextvars back to `None`."""

    for var in _VARS.values():
        var.set(None)
