"""Queue-based handler architecture: producer-side handler, listener, sinks.

Architecture rationale: always QueueHandler + QueueListener, and the
filter placement rule. Crash-safety rationale: the O19 listener guard and
the per-handler `handleError` fallback chain.

`CentralQueueHandler` runs in the caller's thread (cheap: filter + enqueue
only). `CentralQueueListener` owns a single background thread that is the
*only* place the stdout handler is ever touched -- no cross-thread
handler-lock contention, and stdout has exactly one writer. Nothing here
ever writes to the filesystem: stdout is the only sink.
"""

from __future__ import annotations

import copy
import logging
import logging.handlers
import queue
import sys
import threading
from collections.abc import Callable
from typing import Any, override

from logger._stderr import warn_rate_limited
from logger.correlation import CorrelationFilter

_AnyQueue = queue.Queue[Any]
_MAX_EXC_DESC_CHARS = 500
_DEFAULT_STOP_PUT_TIMEOUT = 5.0


class _SafeErrorMixin(logging.Handler):
    """Shared `handleError` for our listener-side handlers (crash_safety.md §4).

    Under pytest (`logging.raiseExceptions is True`), defers to stdlib's own
    loud stderr dump -- the normal dev/test experience. In "prod mode"
    (`raiseExceptions is False`), the failure is routed through the central
    logger's *only* self-diagnostic channel instead: a rate-limited stderr
    line, never the logging pipeline itself (that would risk recursion).
    """

    @override
    def handleError(self, record: logging.LogRecord) -> None:
        if logging.raiseExceptions:
            super().handleError(record)
            return
        warn_rate_limited(
            f"handler:{type(self).__name__}",
            f"central logger {type(self).__name__} failed to emit a record (logger={record.name!r})",
        )


class CentralQueueHandler(logging.handlers.QueueHandler):
    """Producer-side handler: cheap filter + enqueue, nothing else.

    Carries the `_central_logger` marker so bootstrap can find (and, on a
    re-bootstrap, remove) exactly the handler it previously attached to the
    root logger -- see the "Bootstrap sequence" idempotency step.
    """

    _central_logger: bool = True

    def __init__(self, queue: queue.Queue[Any]) -> None:
        super().__init__(queue)
        # thread_safety.md "Filter placement rule": the filter MUST run here,
        # producer-side, in the caller's own thread/context -- contextvars
        # bound in the listener thread would be the wrong ones entirely.
        self.addFilter(CorrelationFilter())
        self._dropped = 0
        self._dropped_lock = threading.Lock()

    @override
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Freeze the record for cross-thread handoff -- but keep `exc_info`.

        Deliberately diverges from stdlib `QueueHandler.prepare()`, which
        also nulls `record.exc_info`/bakes the traceback into `record.msg`
        via `self.format(record)`. We must NOT do that: `JsonFormatter`
        needs the real `exc_info` tuple on the *listener* side to emit
        `exc_type`/`exc_message`/`exc_traceback` (O8).
        """

        record = copy.copy(record)
        record.message = record.getMessage()
        record.msg = record.message
        record.args = None
        record.exc_text = None
        return record

    @override
    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1
                dropped = self._dropped
            warn_rate_limited(
                "queue_full",
                f"central logger queue full; dropped={dropped} records total",
            )

    @property
    def dropped(self) -> int:
        return self._dropped


class CentralQueueListener(logging.handlers.QueueListener):
    """Single background writer thread -- the only handler-touching thread.

    `respect_handler_level=True` lets `LOGS_STDOUT_LEVEL` gate the stdout
    sink on top of the logger levels (`LOGS_LEVEL`).
    """

    _monitor: Callable[[], None]
    _sentinel: object

    def __init__(self, queue: queue.Queue[Any], *handlers: logging.Handler) -> None:
        super().__init__(queue, *handlers, respect_handler_level=True)
        self._pipeline_queue: _AnyQueue = queue

    @override
    def start(self) -> None:
        """Start the monitor thread under the contractual, fixed name.

        stdlib's own `QueueListener.start()` creates the thread with no
        `name=`, leaving it as an auto-numbered `Thread-N` -- tests (and
        `close()`'s own bookkeeping) rely on finding this thread by the
        literal name `central-logger-listener`.
        """

        self._thread = t = threading.Thread(target=self._monitor, name="central-logger-listener", daemon=True)
        t.start()

    @override
    def handle(self, record: logging.LogRecord) -> None:
        """O19: wrap each handler's dispatch in its own try/except.

        stdlib 3.12's `QueueListener._monitor` only catches `queue.Empty`
        around the whole `self.handle(record)` call -- an uncaught
        exception from a raising `emit()` (e.g. a test monkeypatching
        `emit` directly, bypassing that handler's own internal guard, or a
        genuine bug) would otherwise kill the listener thread silently and
        skip `queue.task_done()`, losing every later record. A caught
        failure here is counted (rate-limited stderr) and the loop
        continues to the next handler/record; `_monitor()`'s own
        `task_done()` call still runs since `handle()` itself never raises.

        Catches `BaseException`, not just `Exception`: this is a daemon
        background thread that nothing upstream is waiting to catch
        anything from, so even a `SystemExit`/`GeneratorExit` raised by a
        handler must not be allowed to silently kill it -- that would just
        stop the thread with no diagnostic at all, exactly the failure mode
        this guard exists to prevent. (A real Ctrl-C/`KeyboardInterrupt`
        never targets this background thread directly, so there is no
        legitimate interrupt to preserve here.) The formatting of `exc`
        itself is guarded too -- a malformed exception's own `__repr__` can
        raise, which would otherwise escape this guard the same way the
        original bug did -- and truncated, since `repr(exc)` can otherwise
        copy arbitrarily large record content (e.g. a `UnicodeEncodeError`
        embeds the whole offending string) onto stderr.
        """

        record = self.prepare(record)
        for handler in self.handlers:
            process = self.respect_handler_level
            if process:
                process = record.levelno >= handler.level
            if not process:
                continue
            try:
                handler.handle(record)
            except BaseException as exc:
                try:
                    exc_desc = repr(exc)
                except Exception:
                    exc_desc = type(exc).__name__
                if len(exc_desc) > _MAX_EXC_DESC_CHARS:
                    exc_desc = exc_desc[:_MAX_EXC_DESC_CHARS] + "...(truncated)"
                warn_rate_limited(
                    f"listener_handler:{type(handler).__name__}",
                    f"central logger listener handler {type(handler).__name__} "
                    f"raised while handling a record: {exc_desc}",
                )

    @override
    def stop(self, timeout: float | None = None) -> None:
        """Idempotent, bounded shutdown (R2).

        stdlib's `stop()` calls `enqueue_sentinel()` -- a `put()` that can
        block forever if the queue is full -- then `self._thread.join()`
        with no timeout, and raises `AttributeError` on a second call
        (`self._thread` is already `None`). Fixed here: (a) idempotent via
        an early `_thread is None` guard, (b) the sentinel put is bounded by
        `timeout` and tolerates a full queue (best-effort, proceeds to join
        regardless), (c) `join(timeout)` is a real bound.
        """

        if self._thread is None:
            return

        put_timeout = timeout if timeout is not None else _DEFAULT_STOP_PUT_TIMEOUT
        try:
            self._pipeline_queue.put(self._sentinel, timeout=put_timeout)
        except queue.Full:
            warn_rate_limited(
                "listener_stop_queue_full",
                "central logger listener queue full while stopping; proceeding to join the thread anyway",
            )

        self._thread.join(timeout)
        self._thread = None


class _LiveStdout:
    """Descriptor resolving `sys.stdout` on every read (never cached).

    A later swap of `sys.stdout` is therefore still followed. Assignment is
    absorbed: `StreamHandler`'s base `emit()` sometimes assigns `self.stream`
    back to itself, and that must not shadow the descriptor with an
    instance attribute.
    """

    def __get__(self, instance: object, owner: type | None = None) -> Any:
        return sys.stdout

    def __set__(self, instance: object, value: Any) -> None:
        return None


class StdoutHandler(_SafeErrorMixin, logging.StreamHandler[Any]):
    """Writes formatted JSON lines to `sys.stdout`, resolved at emit time.

    `stream` is a property (never cached at construction), so a later swap
    of `sys.stdout` -- pytest's `capsys` between tests, most notably -- is
    still followed. Never overrides `emit()`: `StreamHandler.emit()` already
    writes through `self.stream` (now our property) and calls
    `self.handleError` on failure.
    """

    def __init__(self, level: int) -> None:
        logging.Handler.__init__(self)
        self.setLevel(level)

    stream = _LiveStdout()
