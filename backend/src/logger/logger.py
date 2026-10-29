"""Central logger public entrypoint.

This file re-exports the
`logging` level constants (so library callers -- which are
banned from `import logging` themselves -- can still
name a level) and declares the module's stable public surface.

`get_logger`/`flush`/`close`/`dropped_count`/`reset` (Wave 3 step `w3-a`)
implement the real lazy, idempotent, thread-safe bootstrap: one
`StdoutHandler` (`JsonFormatter`, one JSON object per line -- the only
sink; nothing is ever written to the filesystem) behind a single
`CentralQueueListener` thread, fed by one `CentralQueueHandler` on the root
logger. Pipeline state lives on the root logger's own `__dict__` (see
`_STATE_KEY` below) rather than in this module's namespace, so it survives
`importlib.reload(logger.logger)` (LOG-35) -- a reload must find the
already-running pipeline and never build a second one. `log_event`, the
module constants, and `get_config` are unchanged from the `w3-0` contract
skeleton.
"""

from __future__ import annotations

import atexit
import dataclasses
import logging
import queue
import threading
import time
from contextlib import suppress
from logging import CRITICAL, DEBUG, ERROR, INFO, WARNING
from types import TracebackType
from typing import Any

from logger import _stderr
from logger._stderr import warn_once
from logger.config import LogConfig, raise_exceptions_from_env
from logger.correlation import CORRELATION_KEYS, RESERVED_ATTRS
from logger.formatters import CLOUD_LOGGING_KEYS, EXCEPTION_KEYS, JsonFormatter
from logger.handlers import CentralQueueHandler, CentralQueueListener, StdoutHandler

__all__ = [
    "APP_ROOT_LOGGERS",
    "CRITICAL",
    "DEBUG",
    "ERROR",
    "INFO",
    "QUEUE_MAXSIZE",
    "THIRD_PARTY_LEVELS",
    "WARNING",
    "close",
    "dropped_count",
    "flush",
    "get_config",
    "get_logger",
    "log_event",
    "reset",
]

#: Bound of the queue `CentralQueueHandler` publishes onto.
QUEUE_MAXSIZE: int = 10_000

#: Bootstrap-time level overrides for noisy third-party loggers (ops.md
#: "Third-party noise" table). `LOGS_LEVEL` never applies to these, so the
#: app level cannot accidentally re-enable the flood.
THIRD_PARTY_LEVELS: dict[str, int] = {
    "httpx": WARNING,
    "httpcore": WARNING,
    "urllib3": WARNING,
    "langfuse": WARNING,
    "neo4j": WARNING,
    "asyncio": ERROR,
    "claude_agent_sdk": WARNING,
}

#: Top-level logger names `get_logger` treats as "ours" -- these get
#: `cfg.level` applied once (manual overrides afterward survive); anything
#: else falls through to whatever the root/third-party rules already set.
APP_ROOT_LOGGERS: tuple[str, ...] = ("llm", "logger")

# `logging`'s own accepted shapes for `exc_info=`.
_ExcInfo = bool | BaseException | tuple[type[BaseException], BaseException, TracebackType | None] | None

#: Key under `logging.getLogger().__dict__` where the pipeline state lives.
#: Anchored on the root *logger instance* (not this module's namespace) so
#: it survives `importlib.reload(logger.logger)` -- LOG-35.
_STATE_KEY = "_central_logger_state"

#: Guards the (rare) bootstrap decision + mutation of `_PipelineState`.
#: Re-entrant: `reset()` holds it while calling `close()`, which takes it
#: again, and that nested acquire must not deadlock.
_lock = threading.RLock()


@dataclasses.dataclass
class _PipelineState:
    """Everything the bootstrapped pipeline needs to tear itself back down.

    `reset()` replaces this object wholesale (except `atexit_registered`, a
    once-per-*process* guard, not a once-per-bootstrap one -- see
    `_bootstrap`'s step 8 and LOG-35's `atexit.register` call-count check).
    """

    status: str = "unconfigured"  # unconfigured | active | failed | closed
    cfg: LogConfig | None = None
    queue: queue.Queue[Any] | None = None
    queue_handler: CentralQueueHandler | None = None
    listener: CentralQueueListener | None = None
    stdout_handler: StdoutHandler | None = None
    configured_tops: set[str] = dataclasses.field(default_factory=set)
    atexit_registered: bool = False


def _get_state() -> _PipelineState:
    """Return the process-wide `_PipelineState`, creating it exactly once.

    Double-checked locking, done correctly: the unlocked fast path only
    ever *reads* an already-published state object -- it never creates
    one. Creation happens exclusively inside `_lock`'s critical section,
    with a re-check after acquiring it, so two threads racing to call
    this for the very first time in a process (before `_STATE_KEY` has
    ever been set on the root logger) are guaranteed to observe and share
    the SAME `_PipelineState` instance, never two independent ones. The
    previous version created the state object on the unlocked fast path
    itself (check-then-set, with no lock at all guarding the "set" half),
    which let two threads each construct and publish their own state --
    each then independently believed itself to be bootstrapping "the"
    pipeline, producing two listener threads for one process (M3).
    """

    root = logging.getLogger()
    state: _PipelineState | None = root.__dict__.get(_STATE_KEY)
    if state is not None:
        return state
    with _lock:
        state = root.__dict__.get(_STATE_KEY)
        if state is None:
            state = _PipelineState()
            root.__dict__[_STATE_KEY] = state
        return state


def _ensure_bootstrap() -> _PipelineState:
    """Lazy, idempotent, double-checked-locking bootstrap trigger."""

    state = _get_state()
    if state.status != "unconfigured":
        return state
    with _lock:
        state = _get_state()
        if state.status != "unconfigured":
            return state
        _bootstrap(state)
        return state


def _bootstrap(state: _PipelineState) -> None:
    """Build the real pipeline. Never raises -- see step 11 below."""

    try:
        cfg = LogConfig.from_env()
        # U4: the only dev/prod switch, set by LOGS_RAISE_EXCEPTIONS. Loud
        # stderr dumps when on (fail visibly while developing); silent,
        # rate-limited ones when off (the deployed default).
        logging.raiseExceptions = raise_exceptions_from_env()

        stdout_handler = StdoutHandler(cfg.stdout_level)
        stdout_handler.setFormatter(JsonFormatter())
        state.stdout_handler = stdout_handler

        pipeline_queue: queue.Queue[Any] = queue.Queue(QUEUE_MAXSIZE)
        state.queue = pipeline_queue
        queue_handler = CentralQueueHandler(pipeline_queue)
        state.queue_handler = queue_handler

        listener = CentralQueueListener(pipeline_queue, stdout_handler)
        state.listener = listener
        listener.start()

        root = logging.getLogger()
        for existing in list(root.handlers):
            if getattr(existing, "_central_logger", False):
                root.removeHandler(existing)
        root.addHandler(queue_handler)
        root.setLevel(logging.WARNING)

        for name, level in THIRD_PARTY_LEVELS.items():
            logging.getLogger(name).setLevel(level)

        # Every bootstrap -- the first one in the process and every one
        # after a `reset()` -- applies `cfg.level` to every app-root logger
        # name unconditionally. A deliberate manual override made *after*
        # `get_logger()`/`get_config()` was already callable is what
        # `configured_tops` (not this eager loop) protects.
        configured_tops: set[str] = set()
        for name in APP_ROOT_LOGGERS:
            logging.getLogger(name).setLevel(cfg.level)
            configured_tops.add(name)
        state.configured_tops = configured_tops
        state.cfg = cfg

        if not state.atexit_registered:
            atexit.register(_atexit_close)
            state.atexit_registered = True

        state.status = "active"
    except Exception as exc:
        warn_once("bootstrap", f"central logger bootstrap failed: {exc!r}")
        _teardown_pipeline(state)
        state.status = "failed"


def _teardown_pipeline(state: _PipelineState) -> None:
    """Best-effort teardown of whatever partial pipeline was built."""

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_central_logger", False):
            with suppress(Exception):
                root.removeHandler(handler)
    if state.listener is not None:
        with suppress(Exception):
            state.listener.stop(2.0)
    if state.stdout_handler is not None:
        with suppress(Exception):
            state.stdout_handler.close()
    state.queue = None
    state.queue_handler = None
    state.listener = None
    state.stdout_handler = None
    state.configured_tops = set()


def _atexit_close() -> None:
    """Total: atexit itself must never see an exception from this hook."""

    with suppress(Exception):
        close(2.0)


def get_logger(name: str) -> logging.Logger:
    """Return the named stdlib logger, ensuring the pipeline is bootstrapped.

    Sets `cfg.level` on `name`'s top-level segment exactly once per
    bootstrap, unless that top-level name is a third-party name
    (`THIRD_PARTY_LEVELS`, never overridden) or was already configured --
    which includes both `APP_ROOT_LOGGERS` (set eagerly during bootstrap)
    and any other top-level name this function has already set once. A
    manual override made by calling code after that point is never
    clobbered by a later `get_logger()` call for a sibling name.
    """

    state = _ensure_bootstrap()
    cfg = state.cfg
    if cfg is not None:
        top = name.partition(".")[0]
        if top not in THIRD_PARTY_LEVELS and top not in state.configured_tops:
            with _lock:
                if top not in state.configured_tops:
                    logging.getLogger(top).setLevel(cfg.level)
                    state.configured_tops.add(top)
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    message: str | None = None,
    /,
    *,
    exc_info: _ExcInfo = None,
    **fields: Any,
) -> None:
    """Emit one structured log record.

    `message` defaults to `event` when omitted. Any field name that would
    collide with a stdlib `LogRecord` attribute (`RESERVED_ATTRS`), a
    correlation key (`CORRELATION_KEYS`), or the literal `"event"` key is
    renamed to `field_<name>` before being passed through -- so a caller's
    own data can never shadow the record's reserved machinery or the real
    bound correlation ids. The same applies to Cloud Logging's special JSON
    keys (`CLOUD_LOGGING_KEYS`: `severity`, `time`, `trace`, `labels`, ...)
    and the formatter's own exception keys (`EXCEPTION_KEYS`: `exc_type`,
    `exc_message`, `exc_traceback`, `format_error`), so a field can never
    override the platform's view of a line or fake an exception.
    `stacklevel=2` makes `funcName` on the emitted record the caller's
    function, not `log_event` itself.

    Guarded by `logger.isEnabledFor(level)` up front so building the
    `fields` mapping and formatting are skipped entirely for a disabled
    level.
    """

    if not logger.isEnabledFor(level):
        return

    renamed: dict[str, Any] = {}
    for key, value in fields.items():
        if (
            key in RESERVED_ATTRS
            or key in CORRELATION_KEYS
            or key == "event"
            or key in CLOUD_LOGGING_KEYS
            or key in EXCEPTION_KEYS
        ):
            renamed[f"field_{key}"] = value
        else:
            renamed[key] = value

    logger.log(
        level,
        message or event,
        exc_info=exc_info,
        extra={"event": event, **renamed},
        stacklevel=2,
    )


def get_config() -> LogConfig:
    """Return the current `LogConfig`, ensuring the pipeline is bootstrapped.

    Per its own documented contract ("get_config() -> LogConfig
    (bootstraps)"), `get_config()` is -- alongside `get_logger()` -- a
    trigger point for the lazy bootstrap: both funnel through the same
    idempotent `_ensure_bootstrap()`, so calling either first has the same
    effect. Returns the `LogConfig` the pipeline actually bootstrapped
    with; falls back to a fresh `LogConfig.from_env()` read in the
    (defensive, should not happen) case where bootstrap left no config.
    """

    state = _ensure_bootstrap()
    return state.cfg if state.cfg is not None else LogConfig.from_env()


def flush(timeout: float = 5.0) -> bool:
    """Wait for every queued record to be written; return whether it drained.

    Unconfigured (never bootstrapped, or already closed): nothing can be
    pending, so this returns `True` immediately without triggering a
    bootstrap of its own (only `get_logger()` does that). Otherwise waits
    on the queue's `unfinished_tasks == 0` condition -- `queue.Queue.join()`
    itself has no timeout parameter, so this polls the same condition
    variable `join()` uses, bounded by `timeout`. Never raises.
    """

    state = _get_state()
    pipeline_queue = state.queue
    if pipeline_queue is None:
        return True

    try:
        deadline = time.monotonic() + timeout
        with pipeline_queue.all_tasks_done:
            while pipeline_queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                pipeline_queue.all_tasks_done.wait(remaining)
            return pipeline_queue.unfinished_tasks == 0
    except (TypeError, ValueError, OverflowError):
        # The only failures of the bounded wait: a `timeout` that is not a
        # number (TypeError), or one the platform clock cannot represent
        # (`Condition.wait` raises OverflowError/ValueError). Not drained.
        return False


def close(timeout: float = 5.0) -> None:
    """Idempotently shut down the logging pipeline.

    A no-op if the pipeline was never bootstrapped, already failed, or is
    already closed. Otherwise: flush, remove the queue handler from root,
    stop the listener thread (draining it), and warn once (with the total
    dropped count) if any records were ever dropped. After this,
    `get_logger()`/`log_event()` still work -- records just fall through to
    stdlib's `lastResort` -- but the pipeline is never auto-restarted; only
    `reset()` followed by a new `get_logger()` call rebuilds it. Never
    raises.
    """

    state = _get_state()
    with _lock:
        if state.status != "active":
            return

        with suppress(Exception):
            flush(timeout)

        root = logging.getLogger()
        if state.queue_handler is not None:
            with suppress(Exception):
                root.removeHandler(state.queue_handler)

        dropped = state.queue_handler.dropped if state.queue_handler is not None else 0

        if state.listener is not None:
            with suppress(Exception):
                state.listener.stop(timeout)

        if dropped > 0:
            warn_once(
                "dropped_records",
                f"central logger dropped {dropped} records total before shutdown",
            )

        state.status = "closed"


def dropped_count() -> int:
    """Return how many records the queue has dropped since bootstrap."""

    state = _get_state()
    if state.queue_handler is None:
        return 0
    return state.queue_handler.dropped


def reset() -> None:
    """Test-only: tear down and clear all bootstrap state.

    Closes the pipeline (idempotent, see `close()`), then replaces the
    published state with a fresh, unconfigured one -- except
    `atexit_registered`, a once-per-*process* guard that must survive any
    number of `reset()`/re-bootstrap cycles, so `atexit.register` is never
    called more than once for the whole process (bootstrap step 8, LOG-35's
    call-count check). Every bootstrap -- the first one in the process and
    every one after a `reset()` -- applies `cfg.level` to `APP_ROOT_LOGGERS`
    unconditionally; see `_bootstrap`. Also clears `logger._stderr`'s dedup
    state, so a fresh bootstrap gets its own one-time warnings again. Does
    NOT immediately re-bootstrap -- the next `get_logger()`/`get_config()`
    call does that lazily, honoring whatever env vars are set at that
    point.
    """

    with _lock:
        close()

        root = logging.getLogger()
        previous = root.__dict__.get(_STATE_KEY)
        fresh = _PipelineState()
        if previous is not None:
            fresh.atexit_registered = previous.atexit_registered
        root.__dict__[_STATE_KEY] = fresh

    _stderr.reset_state()
