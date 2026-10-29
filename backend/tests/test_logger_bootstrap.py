"""Tests for `logger.logger`'s bootstrap sequence — LOG-10, LOG-13,
LOG-14 (AC-8 end to end, AC-6), LOG-19, LOG-29, LOG-30, LOG-32, LOG-33,
LOG-35.

The logger's only sink is stdout, one JSON object per line; `central_log`
parses those lines. Scenario text: the central-logger test catalog (harness and the rows for
the ids above). Cross-checked against the ops rules ("Runtime
configuration", "Third-party noise", "Acceptance tests") and the thread-safety
rules ("Filter placement rule", "Shutdown & data loss", "Acceptance tests" #3).

Every `logger.*` name is imported **inside** each test function (or a
small local helper), never at module scope — the lazy-import convention
`tests/logger_harness.py` uses.

Ambiguities flagged here (best-effort interpretation taken, not blocking —
see the task's failure conditions):

- **LOG-33** ("`get_logger('task.x')` sets `task` once"): SPECS §4.1 names
  `APP_ROOT_LOGGERS = ("llm", "logger")` explicitly, yet the catalog's own
  LOG-33 scenario text uses `"task.x"` — a name outside that literal tuple.
  Read `APP_ROOT_LOGGERS` as documenting the two roots real production code
  actually uses, not as an allowlist that narrows `get_logger`'s general
  rule ("sets the top-level segment's level once, unless it is
  third-party"); this test therefore exercises `get_logger("task.x")`
  exactly as the catalog row states, on a name assumed to be treated
  generically (not third-party) by that general rule.
"""

from __future__ import annotations

import atexit
import dataclasses
import importlib
import io
import json
import logging
import sys
import threading
from typing import Any

import pytest


def _central_logger_state() -> Any:
    """Best-effort reach into the published bootstrap state.

    This location is documented by name ("State lives on the root logger
    `__dict__["_central_logger_state"]`, so it survives
    `importlib.reload(logger.logger)`") — the same convention
    `tests/test_logger_crash_safety.py`'s `_find_handler()` already relies
    on for a different W2 track's tests.
    """

    root = logging.getLogger()
    state = root.__dict__.get("_central_logger_state")
    assert state is not None, (
        "expected logger.logger bootstrap to publish pipeline state at "
        "logging.getLogger().__dict__['_central_logger_state'] per "
        "its bootstrap sequence"
    )
    return state


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


def _listener_handlers() -> list[logging.Handler]:
    """The `CentralQueueListener`'s own (formatter-carrying) handlers."""

    state = _central_logger_state()
    candidates: list[Any] = [state]
    if isinstance(state, dict):
        candidates.extend(state.values())
    else:
        candidates.extend(_object_public_values(state))
    for candidate in candidates:
        handlers = getattr(candidate, "handlers", None)
        if handlers:
            return list(handlers)
    raise AssertionError("could not find the CentralQueueListener's handlers inside _central_logger_state")


def _marked_root_handlers() -> list[logging.Handler]:
    """Root-logger handlers carrying the `_central_logger` marker."""

    root = logging.getLogger()
    return [h for h in root.handlers if getattr(h, "_central_logger", False)]


def _listener_threads() -> list[threading.Thread]:
    """Live threads named `central-logger-listener` (handlers.py's contract
    names this daemon thread explicitly)."""

    return [t for t in threading.enumerate() if t.name == "central-logger-listener"]


# ---------------------------------------------------------------------------
# LOG-10 — U4: `logging.raiseExceptions` is True while the suite runs (the
# conftest guard sets `LOGS_RAISE_EXCEPTIONS=1`), and False with the
# deployed default (the variable unset).
# ---------------------------------------------------------------------------


def test_log_10_raise_exceptions_true_under_pytest(central_log) -> None:
    central_log.configure()

    assert logging.raiseExceptions is True


def test_log_10_raise_exceptions_false_when_the_development_switch_is_off(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bootstrap step 2 (SPECS §4.1): `logging.raiseExceptions` follows
    # `LOGS_RAISE_EXCEPTIONS`. Removing the variable (as deployed) exercises
    # the silent branch deterministically; `monkeypatch.delenv` restores it.
    original_raise_exceptions = logging.raiseExceptions
    monkeypatch.delenv("LOGS_RAISE_EXCEPTIONS", raising=False)

    try:
        central_log.configure()

        assert logging.raiseExceptions is False
    finally:
        # Bootstrap itself (not this test) mutates the *global*
        # `logging.raiseExceptions` as a side effect of the silent branch
        # under test; `monkeypatch` only auto-restores what it changed
        # directly (the environment), so this must be restored by hand or
        # it leaks `False` into every later test in the session.
        logging.raiseExceptions = original_raise_exceptions


# ---------------------------------------------------------------------------
# LOG-13 — O8 / TracebackFormatter.md: an exception logged through the
# queue still carries its full traceback once it reaches stdout (as
# `exc_type`/`exc_message`/`exc_traceback` inside the one JSON line), never
# leaking into the JSON `message` field itself.
# ---------------------------------------------------------------------------


def test_log_13_exception_through_the_queue_keeps_its_traceback_in_json(
    central_log,
) -> None:
    central_log.configure(LOGS_STDOUT_LEVEL="WARNING")

    from logger.logger import WARNING, get_logger, log_event

    logger_ = get_logger("logger-tests.bootstrap.exc")

    def boom() -> None:
        raise ValueError("boom-through-queue")

    try:
        boom()
    except ValueError:
        log_event(logger_, WARNING, "test.exc_through_queue", exc_info=True)

    records = central_log.events("test.exc_through_queue")
    assert len(records) == 1
    record = records[0]
    assert record["exc_type"] == "ValueError"
    assert record["exc_message"] == "boom-through-queue"
    assert "Traceback (most recent call last)" in record["exc_traceback"]
    assert "ValueError: boom-through-queue" in record["exc_traceback"]
    # O8's whole point: the queue-side `prepare()` must not let the
    # traceback leak into (or duplicate inside) the plain `message` field.
    assert "Traceback" not in record["message"]

    # The multi-line traceback stays inside one JSON line: every stdout
    # line parses on its own.
    for line in central_log.stdout().splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict)


# ---------------------------------------------------------------------------
# LOG-14 (AC-8 end to end) — no raw payload body ever reaches stdout: a
# field `K` with a `K_sha256` sibling is hidden, only its preview prints.
# LOG-14b (AC-6) — `LOGS_STDOUT_LEVEL=WARNING` hides INFO, keeps WARNING.
# ---------------------------------------------------------------------------


def test_log_14_raw_payload_body_never_reaches_stdout(central_log) -> None:
    central_log.configure(LOGS_STDOUT_LEVEL="WARNING")

    from logger.logger import WARNING, get_logger, log_event

    logger_ = get_logger("logger-tests.bootstrap.payload")
    raw_body = "RAW-BODY-" + "x" * 500
    # Hand-built on purpose: `summarise_payload` can no longer return the
    # body, so this proves the formatter guard on its own.
    log_event(
        logger_,
        WARNING,
        "test.payload_hidden",
        payload=raw_body,
        payload_sha256="0" * 64,
        payload_preview=raw_body[:200],
    )

    records = central_log.events("test.payload_hidden")
    assert len(records) == 1
    assert "payload" not in records[0]
    assert records[0]["payload_preview"] == raw_body[:200]
    assert raw_body not in central_log.stdout()


def test_log_14_stdout_level_hides_info_and_keeps_warning(central_log) -> None:
    central_log.configure(LOGS_STDOUT_LEVEL="WARNING", LOGS_LEVEL="INFO")

    from logger.logger import INFO, WARNING, get_logger, log_event

    logger_ = get_logger("logger-tests.bootstrap.stdout_level")
    log_event(logger_, INFO, "test.stdout_info_hidden")
    log_event(logger_, WARNING, "test.stdout_warning_shown")

    assert central_log.events("test.stdout_info_hidden") == []
    assert len(central_log.events("test.stdout_warning_shown")) == 1


# ---------------------------------------------------------------------------
# LOG-19 — `CorrelationFilter` sits only on the producer-side
# `CentralQueueHandler`; the listener's own handler (stdout) carries no
# filter; exactly one root handler is marked `_central_logger`.
# ---------------------------------------------------------------------------


def test_log_19_filter_only_on_the_queue_handler_and_one_marked_root_handler(
    central_log,
) -> None:
    central_log.configure()

    from logger.correlation import CorrelationFilter
    from logger.logger import get_logger

    get_logger("logger-tests.bootstrap.filter")

    marked = _marked_root_handlers()
    assert len(marked) == 1
    queue_handler = marked[0]
    assert len(queue_handler.filters) >= 1
    assert any(isinstance(f, CorrelationFilter) for f in queue_handler.filters), (
        "expected the marked root handler (CentralQueueHandler) to carry a "
        f"CorrelationFilter specifically, got filters={queue_handler.filters!r}"
    )

    for handler in _listener_handlers():
        assert handler.filters == [], (
            f"listener-side handler {handler!r} must carry no filter — "
            "thread_safety.md's filter-placement rule: a filter on a "
            "listener-side handler runs in the listener thread, whose "
            "contextvars context is wrong"
        )


# ---------------------------------------------------------------------------
# LOG-29 — the stdout handler resolves `sys.stdout` at emit time, not at
# handler-construction time (so a later stdout swap — e.g. pytest's own
# capsys machinery between tests — is still captured).
# ---------------------------------------------------------------------------


def test_log_29_stdout_handler_resolves_stream_at_emit_not_construction(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    central_log.configure(LOGS_STDOUT_LEVEL="INFO")

    from logger.logger import INFO, flush, get_logger, log_event

    # Bootstrap already wired the stdout handler while `sys.stdout` was
    # whatever capsys had installed at that moment. Swap it for a brand
    # new stream *after* bootstrap and assert the handler still writes
    # there — proving it looks up `sys.stdout` fresh at each emit rather
    # than caching a reference at construction time.
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stdout", replacement)

    logger_ = get_logger("logger-tests.bootstrap.capsys")
    log_event(logger_, INFO, "test.capsys_after_bootstrap")
    flush()

    lines = [line for line in replacement.getvalue().splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    assert [r["event"] for r in records] == ["test.capsys_after_bootstrap"]
    assert records[0]["severity"] == "INFO"


# ---------------------------------------------------------------------------
# LOG-30 — `flush()` returns True after a normal drain and when there is
# nothing configured to drain; returns False (within its timeout) when the
# listener is blocked.
# ---------------------------------------------------------------------------


def test_log_30_flush_true_after_a_normal_drain(central_log) -> None:
    central_log.configure()

    from logger.logger import INFO, flush, get_logger, log_event

    logger_ = get_logger("logger-tests.bootstrap.flush")
    for i in range(5):
        log_event(logger_, INFO, "test.flush_drain", i=i)

    assert flush() is True


def test_log_30_flush_true_when_unconfigured(central_log) -> None:
    from logger.logger import flush, reset

    reset()

    assert flush() is True


def test_log_30_flush_false_when_blocked_within_a_short_timeout(central_log, monkeypatch: pytest.MonkeyPatch) -> None:
    central_log.configure()

    import logger.handlers as handlers_module
    from logger.logger import INFO, flush, get_logger, log_event

    release = threading.Event()
    original_emit = handlers_module.StdoutHandler.emit

    def blocking_emit(self: Any, record: Any) -> Any:
        release.wait(timeout=10)
        return original_emit(self, record)

    monkeypatch.setattr(handlers_module.StdoutHandler, "emit", blocking_emit)

    logger_ = get_logger("logger-tests.bootstrap.flush_blocked")
    log_event(logger_, INFO, "test.flush_blocked")

    try:
        assert flush(timeout=0.1) is False
    finally:
        # Always release the blocked listener thread, even on assertion
        # failure — otherwise it stays blocked for up to 10s, slowing down
        # the rest of the suite.
        release.set()

    assert flush(timeout=10.0) is True


# ---------------------------------------------------------------------------
# LOG-32 — third-party noise isolation: `httpx` DEBUG is not written with
# defaults; raising `LOGS_LEVEL=DEBUG` makes app-level DEBUG land while
# `httpx` (and `asyncio` at WARNING) stay suppressed per the ops.md table.
# ---------------------------------------------------------------------------


def test_log_32_third_party_levels_stay_isolated_from_the_app_level(
    central_log,
) -> None:
    central_log.configure()

    from logger.logger import get_logger

    # Default LOGS_LEVEL (INFO): httpx DEBUG noise is not written at all.
    logging.getLogger("httpx").debug("httpx debug default noise")
    records = central_log.records()
    assert not any(r.get("message") == "httpx debug default noise" for r in records)

    # asyncio is pinned to ERROR by the ops.md table — a WARNING is noise.
    logging.getLogger("asyncio").warning("asyncio warning noise")
    records = central_log.records()
    assert not any(r.get("message") == "asyncio warning noise" for r in records)

    # Raising the app level to DEBUG makes *app* DEBUG land...
    central_log.configure(LOGS_LEVEL="DEBUG")
    app_logger = get_logger("llm.logger_tests_probe")
    app_logger.debug("app debug at debug level")
    # ...but httpx, a third-party name, is unaffected by LOGS_LEVEL and
    # stays at its fixed WARNING policy.
    logging.getLogger("httpx").debug("httpx debug still suppressed at app debug")

    records = central_log.records()
    messages = {r.get("message") for r in records}
    assert "app debug at debug level" in messages
    assert "httpx debug still suppressed at app debug" not in messages


# ---------------------------------------------------------------------------
# LOG-33 — root sits at WARNING after bootstrap; `get_logger("task.x")`
# sets the "task" top-level logger once (a later call never clobbers a
# manual override); a manual `llm` DEBUG override survives; `httpx` stays
# at its third-party WARNING regardless.
# ---------------------------------------------------------------------------


def test_log_33_root_level_app_root_semantics_and_manual_overrides(
    central_log,
) -> None:
    """Scenario (O7; ops.md "override note"; SPECS §4.1 `get_logger` row):

    1. Bootstrap with LOGS_LEVEL=INFO -> root logger sits at WARNING.
    2. `get_logger("task.x")` sets the "task" top-level logger to cfg.level
       (INFO) once.
    3. A later `get_logger()` call for a sibling name never re-clobbers a
       manual override made on that top-level logger in between.
    4. A manual override on "llm" (DEBUG) likewise survives a later
       `get_logger()` call.
    5. `httpx`, a third-party name from the ops.md table, is never touched
       by this app-root plumbing and stays at WARNING even when
       LOGS_LEVEL=DEBUG.
    """

    # `logging.getLogger(name)` objects are process-wide singletons that
    # persist across tests — capture the levels this test is about to
    # mutate so they can be restored afterward, instead of leaking into
    # whatever test runs next.
    task_logger = logging.getLogger("task")
    llm_logger = logging.getLogger("llm")
    original_task_level = task_logger.level
    original_llm_level = llm_logger.level

    try:
        central_log.configure(LOGS_LEVEL="INFO")
        assert logging.getLogger().level == logging.WARNING

        from logger.logger import get_logger

        get_logger("task.x")
        assert task_logger.level == logging.INFO

        task_logger.setLevel(logging.ERROR)
        get_logger("task.y")
        assert task_logger.level == logging.ERROR, (
            "a manual override on the app-root logger must survive a later get_logger() call for a sibling name"
        )

        llm_logger.setLevel(logging.DEBUG)
        get_logger("llm.probe")
        assert llm_logger.level == logging.DEBUG

        central_log.configure(LOGS_LEVEL="DEBUG")
        get_logger("llm.another_probe")
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        task_logger.setLevel(original_task_level)
        llm_logger.setLevel(original_llm_level)


# ---------------------------------------------------------------------------
# LOG-35 — U2: repeated `get_logger()`, an `importlib.reload`, and a
# `reset()` with a new `LOGS_STDOUT_LEVEL` never grow past one marked root
# handler / one `central-logger-listener` thread, register `atexit` exactly
# once for the whole process, and the new level is actually used.
# ---------------------------------------------------------------------------


def test_log_35_repeated_bootstrap_reload_and_reset_stay_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGS_STDOUT_LEVEL", "INFO")
    monkeypatch.setenv("LOGS_LEVEL", "INFO")

    atexit_registrations: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn, *_a, **_kw: atexit_registrations.append(fn))

    import logger.logger as logger_module

    logger_module.reset()

    try:
        logger_module.get_logger("logger-tests.bootstrap.singleton")
        logger_module.get_logger("logger-tests.bootstrap.singleton")
        logger_module.get_logger("logger-tests.bootstrap.singleton")

        assert len(_marked_root_handlers()) == 1
        assert len(_listener_threads()) == 1

        importlib.reload(logger_module)
        logger_module.get_logger("logger-tests.bootstrap.singleton")

        assert len(_marked_root_handlers()) == 1
        assert len(_listener_threads()) == 1

        monkeypatch.setenv("LOGS_STDOUT_LEVEL", "ERROR")
        logger_module.reset()
        logger_module.get_logger("logger-tests.bootstrap.singleton")

        assert len(_marked_root_handlers()) == 1
        assert len(_listener_threads()) == 1
        state = _central_logger_state()
        assert state.cfg.stdout_level == logging.ERROR
        assert state.stdout_handler.level == logging.ERROR

        # Relaxed to `<= 1`: the bootstrap sequence (step 8) only documents
        # "atexit.register(_atexit_close) once [...] not once per bootstrap
        # cycle" — it does not say
        # whether `reset()` clears the internal "already registered" flag.
        # An absolute `== 1` is therefore order-dependent: it can only
        # reliably pass when this test file runs in isolation, since other
        # tests earlier in the same pytest session may have already
        # triggered registration before this test's own
        # `monkeypatch.setattr(atexit, "register", ...)` started
        # observing it. `LOG-28` (tests/test_logger_concurrency.py)
        # already separately proves atexit actually works end-to-end via a
        # real subprocess, so this assertion only needs to guard against
        # *this test's own* get_logger()/reload/reset() calls
        # re-registering more than once. Wave 3's implementer should pin
        # this down explicitly in `logger.py`'s docstring.
        assert len(atexit_registrations) <= 1, (
            "atexit.register(_atexit_close) must not fire more than once "
            "as a result of this test's own get_logger()/reload/reset() "
            "calls (bootstrap step 8)"
        )
    finally:
        logger_module.reset()
