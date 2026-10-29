from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Any

from llm.captured import CapturedException
from llm.tracing.core import (
    ToolEnd,
    ToolStart,
    TraceClose,
    TraceResult,
    TraceSink,
    TraceStart,
)
from logger.correlation import bind, new_id, unbind
from logger.logger import WARNING, get_logger, log_event


class CompositeTraceSink:
    """Fan every `TraceSink` method out to a fixed list of child sinks.

    Binds a fresh `run_id` (plus `agent_id`/`session_id` derived from the
    `TraceStart` event) in `logger.correlation` for the duration of one
    traced run, so everything logged through `logger.logger.log_event`
    while this run is open -- this run's `LoggingSink` included -- carries
    the same correlation ids automatically, via `CorrelationFilter`.

    A child that raises is isolated: a failing `start()` skips that child
    for the rest of the run (retried on the next `start()`); a failing
    call to any other method is logged (`trace.sink_failed`, rate-limited
    to the first occurrence per child/method per run) and otherwise
    ignored. If *every* child fails `start()`, there is nothing usable
    this run, so the correlation binding is undone and the first error is
    re-raised -- matching a single failing `TraceSink.start()`, so the
    wrapping `TraceSession` marks itself unavailable exactly as it does
    today.
    """

    def __init__(self, sinks: Sequence[TraceSink]) -> None:
        if not sinks:
            raise ValueError("CompositeTraceSink needs at least one sink")
        # Public: callers unwrap a fanned-out LoggingSink by walking `.sinks`
        # (e.g. a test helper that finds the LoggingSink).
        self.sinks: tuple[TraceSink, ...] = tuple(sinks)
        self.run_id: str | None = None
        self.failure_count: int = 0
        # Indices into `self.sinks` that failed `start()` this run.
        self._skipped: set[int] = set()
        # The `logger.correlation.CorrelationToken` from the current run's
        # `bind()`, or None between runs.
        self._token: Any = None
        # (sink_index, method_name) pairs already warned about this run.
        self._warned: set[tuple[int, str]] = set()

    def start(self, event: TraceStart) -> None:
        # Defensive: a token can still be sitting here from a run whose
        # close() could not fully unbind it in this context (e.g. close()
        # ran inside a `contextvars.Context` copied since this bind --
        # see `close()`'s docstring). This is the first chance, back in
        # the context that actually created the token, to finish that
        # cleanup before a new run starts.
        if self._token is not None:
            unbind(self._token)
            self._token = None
        self._skipped.clear()
        self._warned.clear()

        run_id = new_id()
        session_id = (event.metadata or {}).get("langfuse_session_id")
        self._token = bind(run_id=run_id, agent_id=event.name, session_id=session_id)
        self.run_id = run_id

        failures: list[tuple[int, Exception]] = []
        for index, sink in enumerate(self.sinks):
            with CapturedException() as start_capture:
                sink.start(event)
            if start_capture.error is not None:
                failures.append((index, start_capture.error))
                self._skipped.add(index)

        if len(failures) == len(self.sinks):
            unbind(self._token)
            self._token = None
            self.run_id = None
            raise failures[0][1]

        self.failure_count += len(failures)
        for index, failure in failures:
            self._warn(index, "start", failure)

    def update_model(self, model: str) -> None:
        self._fan_out("update_model", model)

    def record_system_message(self, data: Mapping[str, Any]) -> None:
        self._fan_out("record_system_message", data)

    def start_tool(self, event: ToolStart) -> None:
        self._fan_out("start_tool", event)

    def end_tool(self, event: ToolEnd) -> None:
        self._fan_out("end_tool", event)

    def abandon_tool(self, tool_id: str) -> None:
        self._fan_out("abandon_tool", tool_id)

    def record_result(self, event: TraceResult) -> None:
        self._fan_out("record_result", event)

    def close(self, event: TraceClose) -> None:
        """Fan out `close`, then unbind this run's correlation ids.

        The unbind can fail to take effect if this call runs inside a
        `contextvars.Context` copied since `start()` (an SDK boundary that
        forks the context between start and close): `logger.correlation.
        unbind` absorbs that case's `ValueError` rather than raising, and
        the next `start()` on this instance gets another chance to finish
        the cleanup once it runs back in the context that owns the token.
        """

        try:
            self._fan_out("close", event)
        finally:
            if self._token is not None:
                unbind(self._token)

    def _fan_out(self, method_name: str, *args: Any) -> None:
        for index, sink in enumerate(self.sinks):
            if index in self._skipped:
                continue
            with CapturedException() as call_capture:
                getattr(sink, method_name)(*args)
            if call_capture.error is not None:
                self.failure_count += 1
                self._warn(index, method_name, call_capture.error)

    def _warn(self, sink_index: int, method_name: str, exc: Exception) -> None:
        """Best-effort diagnostic only -- its own failure must never cascade.

        This runs from inside `start()`/`_fan_out()`'s partial-failure
        path, *after* the correlation ids for this run are already bound
        (`start()`) or while other sinks may still need their turn
        (`_fan_out()`). `log_event` itself is total for the central
        pipeline's own filter, but a third-party filter someone else
        attached to this logger could still raise -- and if it did here,
        unguarded, that would raise back out of `start()`/`_fan_out()`
        after the bind already happened, leaving `run_id` bound with the
        session left in an inconsistent, unavailable state. Suppressing
        everything below means a raising diagnostic is dropped silently
        rather than being allowed to break the run it was only trying to
        report on.
        """

        key = (sink_index, method_name)
        if key in self._warned:
            return
        self._warned.add(key)

        with suppress(Exception):
            sink_name = type(self.sinks[sink_index]).__name__
            log_event(
                get_logger(__name__),
                WARNING,
                "trace.sink_failed",
                f"trace sink {sink_name}.{method_name} failed",
                exc_info=exc,
                sink=sink_name,
                method=method_name,
            )
