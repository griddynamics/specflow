"""Tests for LOG-09, LOG-18, LOG-20, LOG-22, LOG-23, LOG-24 (AC-10, AC-11).

Target-state contract under test: `src/logger/handlers.py`'s crash-safety
guarantees (`CentralQueueListener`'s `handle()` override, `_SafeErrorMixin`,
bootstrap failure), `src/logger/correlation.py`'s total `CorrelationFilter`,
and `src/logger/logger.py`'s format-error policy (`logging.raiseExceptions`
under pytest vs. "prod").

Every `logger.*` name is imported **inside** each test function (or a
small local helper), never at module scope — the lazy-import convention
`tests/logger_harness.py` uses.

Authoritative sources (read in full before writing these tests):
- The central-logger test catalog: harness, rows LOG-09,
  LOG-18, LOG-20, LOG-22, LOG-23, LOG-24, and the LOG-20/O19
  paragraph in the TLDR.
- The crash-safety rules (all 7 failure points
  + the 4 acceptance tests).
- The StrFormatter rules, §2 (format-error
  policy).

Ambiguity flagged (per the task's failure
conditions): LOG-20/23/24 need a *handler instance* to monkeypatch directly
(`emit`, or its `.formatter.format`), but no name in the public contract
(`get_logger`/`log_event`/`flush`/`close`/`dropped_count`/`reset`/
`get_config`) returns handler instances. `_find_handler()` below reaches into
`logging.getLogger().__dict__["_central_logger_state"]`, which is
documented by name (kept there specifically so it survives
`importlib.reload`, per LOG-35) — this is
the best-effort, most-literal reading available, not a private/undocumented
attribute invented for this test. The "listener thread is still alive" check
is NOT ambiguous: handlers.py's contract names the thread explicitly
("daemon thread named `central-logger-listener`"), so `_listener_thread()`
below just filters `threading.enumerate()` by that name — the same
by-name convention LOG-26 (a separate track's test) already relies on.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from typing import Any
from unittest.mock import Mock

import pytest


def _object_public_values(obj: Any) -> list[Any]:
    """Values off `obj`, robust to `@dataclass(slots=True)` (no `__dict__`).

    It is not specified whether Wave 3 implements the published bootstrap
    state as a dataclass, and if so whether it uses `slots=True` — a
    slotted dataclass has no `__dict__`,
    so a bare `vars(obj)` would raise `TypeError` against it. Try
    `dataclasses.fields()` + `getattr()` per field first (works for both
    slotted and unslotted dataclasses); fall back to `vars(obj)` for a
    plain object.
    """

    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return [getattr(obj, f.name) for f in dataclasses.fields(obj)]
    try:
        return list(vars(obj).values())
    except TypeError:
        return []


def _find_handler(handler_type: type[logging.Handler]) -> logging.Handler:
    """Best-effort lookup of a live handler instance of `handler_type` inside
    the bootstrapped `CentralQueueListener`. See the module docstring above
    for why this reaches into `_central_logger_state`.
    """

    root = logging.getLogger()
    state = root.__dict__.get("_central_logger_state")
    assert state is not None, (
        "expected logger.logger bootstrap to publish pipeline state at "
        "logging.getLogger().__dict__['_central_logger_state'] per "
        "its bootstrap sequence"
    )

    candidates: list[Any] = [state]
    if isinstance(state, dict):
        candidates.extend(state.values())
    else:
        candidates.extend(_object_public_values(state))

    for candidate in candidates:
        handlers = getattr(candidate, "handlers", None)
        if not handlers:
            continue
        for handler in handlers:
            if isinstance(handler, handler_type):
                return handler

    raise AssertionError(
        f"could not find a {handler_type.__name__} among the bootstrapped "
        "pipeline listener's handlers; see this file's module docstring for "
        "the best-effort _central_logger_state walk this relies on"
    )


def _listener_thread() -> threading.Thread | None:
    """The daemon `central-logger-listener` thread, if bootstrapped.

    handlers.py's contract names this thread explicitly ("daemon thread
    named `central-logger-listener`"), and LOG-26 (a different W2 track's
    test) already relies on the same by-name convention — so this is not an
    ambiguous lookup.
    """

    for thread in threading.enumerate():
        if thread.name == "central-logger-listener":
            return thread
    return None


# ---------------------------------------------------------------------------
# LOG-09 — malformed format args: `info("%d", "x")` returns normally; stderr
# shows the stdlib "--- Logging error ---" dump under pytest
# (raiseExceptions=True, U4); silent of that dump when raiseExceptions=False.
# ---------------------------------------------------------------------------


def test_log_09_malformed_format_args_return_normally_and_dump_to_stderr_under_pytest(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    # pytest's own root-logger handler (_pytest.logging.LogCaptureHandler)
    # overrides handleError to deliberately re-raise when raiseExceptions is
    # True ("fail the test if the log message is bad") -- the opposite of
    # stdlib's own Handler.handleError, which prints the dump and returns.
    # Neutralize just that override for this one test so a malformed format
    # call demonstrates real (non-pytest) behavior, which this test asserts.
    import _pytest.logging

    from logger.logger import flush, get_logger

    monkeypatch.setattr(_pytest.logging.LogCaptureHandler, "handleError", logging.Handler.handleError)

    log = get_logger("test.logger_crash_safety.log_09_pytest_mode")

    # "%d" % ("not-a-number",) raises TypeError at emit time. Under pytest,
    # bootstrap step 2 sets logging.raiseExceptions = True (U4), so stdlib's
    # own Handler.handleError prints its "--- Logging error ---" dump. The
    # call itself must return normally regardless.
    log.info("%d", "not-a-number")

    assert flush(timeout=2.0) is True

    stderr_text = central_log.stderr()
    assert "--- Logging error ---" in stderr_text


def test_log_09_malformed_format_args_suppress_stdlib_dump_when_raise_exceptions_false(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import flush, get_logger

    # Bootstrap first (sets raiseExceptions True under pytest, U4), then
    # force "prod mode" for this call only.
    log = get_logger("test.logger_crash_safety.log_09_prod_mode")
    monkeypatch.setattr(logging, "raiseExceptions", False)

    log.info("%d", "not-a-number")

    assert flush(timeout=2.0) is True

    stderr_text = central_log.stderr()
    assert "--- Logging error ---" not in stderr_text


# ---------------------------------------------------------------------------
# LOG-18 — fuzz CorrelationFilter with exotic extras / junk contextvars /
# raising __getattr__ → filter() always returns True, never raises
# (crash_safety.md acc. 2, §3: the one stdlib layer with no safety net).
# ---------------------------------------------------------------------------


class _RaisingGetattr:
    """An object whose attribute access always explodes."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"boom accessing {name!r}")

    def __repr__(self) -> str:
        return "_RaisingGetattr()"


class _RaisingAttrRecord(logging.LogRecord):
    """A LogRecord whose *missing*-attribute lookups always explode."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"record boom accessing {name!r}")


def _plain_record(name: str = "test.logger_crash_safety.log_18") -> logging.LogRecord:
    return logging.LogRecord(name, logging.INFO, __file__, 1, "msg", (), None)


def test_log_18_correlation_filter_never_raises_on_exotic_records() -> None:
    from logger.correlation import CorrelationFilter

    flt = CorrelationFilter()

    record_with_prebound_junk = _plain_record()
    record_with_prebound_junk.run_id = object()
    record_with_prebound_junk.request_id = _RaisingGetattr()
    record_with_prebound_junk.session_id = float("nan")
    record_with_prebound_junk.agent_id = {"a": {"b": {"c": [1, 2, {"d": _RaisingGetattr()}]}}}

    exotic_records: list[logging.LogRecord] = [
        _plain_record(),
        record_with_prebound_junk,
        _RaisingAttrRecord(
            "test.logger_crash_safety.log_18_raising_attr",
            logging.INFO,
            __file__,
            1,
            "msg",
            (),
            None,
        ),
    ]

    for record in exotic_records:
        assert flt.filter(record) is True


def test_log_18_correlation_filter_never_raises_with_junk_contextvar_values() -> None:
    from logger.correlation import CorrelationFilter, _reset_for_tests, bind, unbind

    _reset_for_tests()
    flt = CorrelationFilter()

    junk_ids = {
        "request_id": 12345,  # not a str
        "run_id": None,
        "session_id": ["not", "a", "string"],
        "agent_id": _RaisingGetattr(),
    }

    token = bind(**junk_ids)
    try:
        assert flt.filter(_plain_record()) is True
    finally:
        unbind(token)
        _reset_for_tests()


# ---------------------------------------------------------------------------
# LOG-20 (AC-10) — stdout emit() monkeypatched directly to raise once
# (bypassing the handler's own internal try/except) → all 4 of: (1)
# logger.info(...) returns normally; (2) the listener thread stays alive;
# (3) the next record still reaches stdout as parsed JSON; (4)
# logger.logger.flush() still returns True within its timeout. Guards O19.
# ---------------------------------------------------------------------------


def test_log_20_raising_stdout_emit_keeps_listener_alive_and_next_record_flows(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    central_log.configure(LOGS_LEVEL="INFO", LOGS_STDOUT_LEVEL="INFO")

    from logger.handlers import StdoutHandler
    from logger.logger import _get_state, flush, get_logger

    log = get_logger("test.logger_crash_safety.log_20")  # triggers bootstrap
    assert _get_state().status == "active"
    assert not hasattr(_get_state(), "file_handler")

    listener_thread_before = _listener_thread()
    assert listener_thread_before is not None, (
        "expected a bootstrapped pipeline to start a thread named 'central-logger-listener'"
    )
    assert listener_thread_before.is_alive()

    stdout_handler = _find_handler(StdoutHandler)
    real_emit = stdout_handler.emit
    # Raise on the first record only; every later one goes to stdout.
    calls = iter([Exception("boom: stdout emit")])

    def emit_once_raising(record: logging.LogRecord) -> None:
        failure = next(calls, None)
        if failure is not None:
            raise failure
        real_emit(record)

    monkeypatch.setattr(stdout_handler, "emit", emit_once_raising)

    # (1) logger.info(...) returns normally / no exception propagates to the
    # caller.
    log.info("first record hits the now-raising stdout handler")

    # Synchronize with the listener thread before inspecting it. If O19's
    # guard is missing, stdlib 3.12's QueueListener._monitor only catches
    # queue.Empty, so the unguarded handle() dispatch above would already
    # have killed the listener thread and left this flush() hanging/False.
    assert flush(timeout=2.0) is True

    # (2) the listener thread is still alive after the failure — literally
    # the same thread object, not a silently-restarted one (nothing in the
    # contract auto-restarts a dead listener).
    listener_thread_after = _listener_thread()
    assert listener_thread_after is not None
    assert listener_thread_after is listener_thread_before
    assert listener_thread_after.is_alive()

    # (3) a second record logged after the failure still reaches stdout.
    log.info("second record after the handler failure")

    # (4) flush() still returns True within its timeout.
    assert flush(timeout=2.0) is True

    messages = [record["message"] for record in central_log.records()]
    assert "second record after the handler failure" in messages
    assert "first record hits the now-raising stdout handler" not in messages


# ---------------------------------------------------------------------------
# LOG-22 — CentralQueueListener.start() raises → get_logger() still returns
# a usable logger, exactly one stderr warning.
# ---------------------------------------------------------------------------


def test_log_22_listener_start_failure_still_returns_a_usable_logger(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    from logger.handlers import CentralQueueListener

    monkeypatch.setattr(
        CentralQueueListener,
        "start",
        Mock(side_effect=RuntimeError("listener start boom")),
    )

    central_log.configure(LOGS_LEVEL="INFO")

    from logger.logger import get_logger

    log = get_logger("test.logger_crash_safety.log_22")
    assert isinstance(log, logging.Logger)
    log.info("bootstrap degraded, but this call must not raise")

    stderr_lines = [line for line in central_log.stderr().splitlines() if line.strip()]
    assert len(stderr_lines) == 1
    assert stderr_lines[0].startswith("[central-logger]")


# ---------------------------------------------------------------------------
# LOG-23 — "prod mode" (raiseExceptions=False), 100 failing emits (via a
# raising formatter, i.e. the handler's *own* internal emit try/except is
# exercised naturally, unlike LOG-20's direct-emit bypass) → at most 1
# stderr line for that one failure type (crash_safety.md §4 rate limiting).
# ---------------------------------------------------------------------------


def test_log_23_prod_mode_rate_limits_repeated_format_failures_to_one_stderr_line(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    central_log.configure(LOGS_LEVEL="INFO", LOGS_STDOUT_LEVEL="INFO")

    from logger.handlers import StdoutHandler
    from logger.logger import flush, get_logger

    log = get_logger("test.logger_crash_safety.log_23")  # triggers bootstrap
    monkeypatch.setattr(logging, "raiseExceptions", False)  # "prod mode"

    handler = _find_handler(StdoutHandler)
    monkeypatch.setattr(handler.formatter, "format", Mock(side_effect=Exception("format boom")))

    for i in range(100):
        log.info("record %d", i)

    assert flush(timeout=2.0) is True

    stderr_lines = [line for line in central_log.stderr().splitlines() if line.strip()]
    assert len(stderr_lines) <= 1


# ---------------------------------------------------------------------------
# LOG-24 — stdout BrokenPipeError → app continues, flush() still drains.
# ---------------------------------------------------------------------------


def test_log_24_broken_pipe_on_stdout_does_not_raise(central_log, monkeypatch: pytest.MonkeyPatch) -> None:
    central_log.configure(LOGS_LEVEL="INFO", LOGS_STDOUT_LEVEL="INFO")

    from logger.handlers import StdoutHandler
    from logger.logger import flush, get_logger

    log = get_logger("test.logger_crash_safety.log_24_no_raise")

    stdout_handler = _find_handler(StdoutHandler)
    monkeypatch.setattr(stdout_handler, "emit", Mock(side_effect=BrokenPipeError()))

    for i in range(5):
        log.info("record %d survives a broken stdout pipe", i)

    assert flush(timeout=2.0) is True
