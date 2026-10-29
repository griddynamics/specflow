"""Tests for concurrency, shutdown, and correlation propagation —
LOG-17, LOG-25, LOG-26, LOG-27, LOG-28.

Scenario text: the central-logger test catalog (harness and rows
LOG-17/25/26/27/28). Target-state contract: the thread-safety rules in full (the
architecture diagram, "Filter placement rule", "contextvars propagation
rules" table, "Shutdown & data loss", and all 5 "Acceptance tests" this
file formalizes as LOG-17/25/26/27/28).

Every `logger.*` name is imported **inside** each test function (or a
small local helper), never at module scope — the lazy-import convention
`tests/logger_harness.py` uses.

No house pattern for combining real `threading.Thread`s with
`pytest-asyncio` was found in the existing suite (`tests/test_sequence.py`
and the rest are asyncio-only or thread-only, never both); this file uses
plain `threading.Thread` + `asyncio.gather`/`await`, synchronized with
`threading.Barrier`/`threading.Event` rather than sleeps, per the task's
own guidance.

**LOG-25/26/27's exact-count handling**: each test captures
`len(central_log.records())` as a baseline *right after*
`central_log.configure()`, then asserts the *increase* after the real work
equals the expected count — never filtering by event name — so any record
logged during bootstrap cannot throw the count off.

**LOG-28's subprocess mechanism**: `subprocess.run([sys.executable, "-c",
...])`, not `multiprocessing`. This is the more portable choice (no pickling
of bound methods/loggers across a fork/spawn boundary, no platform-specific
start-method quirks) and stays fast (a 20-record script exits in well under
a second). The child inherits this process's environment (so the editable
`logger`/`llm` install resolves import-wise) with `LOGS_STDOUT_LEVEL=INFO`,
and the test parses the JSON lines on its captured stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait

import pytest

# ---------------------------------------------------------------------------
# LOG-17 — thread_safety.md acceptance #2 + the contextvars propagation
# table: `run_in_context` copies the caller's context into a new thread (a
# bare `threading.Thread` target does not); two concurrently gathered
# asyncio tasks each keep their own bound `run_id` (asyncio tasks copy the
# context at creation, so this works without extra help).
# ---------------------------------------------------------------------------


def test_log_17_run_in_context_thread_inherits_ids_bare_thread_does_not() -> None:
    from logger.correlation import bind, current_run_id, run_in_context, unbind

    token = bind(run_id="parent-run-id")
    try:
        wrapped_result: dict[str, str | None] = {}
        bare_result: dict[str, str | None] = {}

        def capture(sink: dict[str, str | None]) -> None:
            sink["value"] = current_run_id()

        # `run_in_context` copies the *current* contextvars.Context at call
        # time and runs the target inside a `ctx.run(...)` — this is the
        # helper thread_safety.md's table says new threads need.
        wrapped = run_in_context(capture, wrapped_result)
        thread_with_context = threading.Thread(target=wrapped)
        thread_with_context.start()
        thread_with_context.join(timeout=5)
        assert not thread_with_context.is_alive()

        # A bare `threading.Thread` target starts with a fresh, top-level
        # context — no run_id inherited.
        thread_bare = threading.Thread(target=capture, args=(bare_result,))
        thread_bare.start()
        thread_bare.join(timeout=5)
        assert not thread_bare.is_alive()

        assert wrapped_result["value"] == "parent-run-id"
        assert bare_result["value"] is None
    finally:
        unbind(token)


@pytest.mark.asyncio
async def test_log_17_two_gathered_tasks_keep_distinct_run_ids() -> None:
    from logger.correlation import bind, current_run_id, unbind

    async def run_with_id(run_id: str) -> str | None:
        token = bind(run_id=run_id)
        try:
            await asyncio.sleep(0)
            return current_run_id()
        finally:
            unbind(token)

    results = await asyncio.gather(run_with_id("run-a"), run_with_id("run-b"))

    assert results == ["run-a", "run-b"]


# ---------------------------------------------------------------------------
# LOG-25 — thread_safety.md acceptance #1: 8 threads x 500 records + the
# event loop x 500 records, logging concurrently, with no exceptions, an
# exact record-count match, and every line parsing as valid JSON.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_25_eight_threads_and_the_event_loop_log_concurrently_without_loss_or_corruption(
    central_log,
) -> None:
    """Exact-count handling: `baseline` is taken right after
    `central_log.configure()`, and the assertion is on the *delta* after
    emitting — not a literal "records == 4500".

    "All lines parse, no interleaved lines" is asserted implicitly:
    `central_log.records()` calls `json.loads` on every stdout line that
    starts with `{` and fails the test on any that does not parse, so a
    torn/interleaved write fails this test outright.
    """

    central_log.configure()

    from logger.logger import INFO, get_logger, log_event

    baseline = len(central_log.records())

    thread_count = 8
    records_per_thread = 500
    event_loop_records = 500
    # +1 so the main (event-loop) thread also joins the rendezvous —
    # nobody starts logging until every producer is ready, avoiding a
    # timing-dependent race in the test's own design.
    ready = threading.Barrier(thread_count + 1)

    def worker(worker_index: int) -> None:
        logger_ = get_logger(f"logger-tests.concurrency.thread{worker_index}")
        ready.wait(timeout=5)
        for i in range(records_per_thread):
            log_event(
                logger_,
                INFO,
                "test.concurrent_thread",
                worker=worker_index,
                i=i,
            )

    pool = ThreadPoolExecutor(max_workers=thread_count)
    futures = [pool.submit(worker, index) for index in range(thread_count)]

    logger_main = get_logger("logger-tests.concurrency.eventloop")
    ready.wait(timeout=5)
    for i in range(event_loop_records):
        log_event(logger_main, INFO, "test.concurrent_eventloop", i=i)
        if i % 50 == 0:
            await asyncio.sleep(0)  # yield, keep this genuinely concurrent

    _, pending = wait(futures, timeout=15)
    pool.shutdown(wait=False)
    assert not pending, "a worker thread did not finish in time"

    errors = [future.exception() for future in futures if future.exception() is not None]
    assert errors == [], f"logging from a thread raised: {errors!r}"

    from logger.logger import flush

    flush()

    total_after = len(central_log.records())
    expected = thread_count * records_per_thread + event_loop_records
    assert total_after - baseline == expected


# ---------------------------------------------------------------------------
# LOG-26 — thread_safety.md acceptance #3: emit N records, call the REAL
# `close()` twice (idempotency), then log again — N records present, no
# raise, and no new `central-logger-listener` thread survives.
#
# Per qa/tests.md §1.2's documented exception, this is the ONE test in the
# whole suite allowed to call the real `logger.logger.close()` for its own
# assertions (not the harness's `reset()`-based teardown). `central_log`
# still cleans up *around* this test afterward via its own `_teardown()`.
# ---------------------------------------------------------------------------


def test_log_26_close_is_idempotent_and_drains_without_a_new_listener_thread(
    central_log,
) -> None:
    central_log.configure()

    from logger.logger import INFO, close, get_logger, log_event

    baseline = len(central_log.records())

    logger_ = get_logger("logger-tests.concurrency.close")
    emitted = 25
    for i in range(emitted):
        log_event(logger_, INFO, "test.before_close", i=i)

    close(timeout=5.0)
    close(timeout=5.0)  # idempotency: must not raise, must not hang

    records = central_log.records()
    assert len(records) - baseline == emitted

    # Logging after close() must not raise. Per the close() contract,
    # records fall through to stdlib's `lastResort` and the pipeline is
    # never silently restarted by a bare log call (only `reset()` +
    # `get_logger()` can restart it).
    log_event(logger_, INFO, "test.after_close", i=-1)

    # Checked *after* the post-close log call above (not before it), so an
    # implementation that buggily restarts the listener thread on a bare
    # log call following close() would be caught here.
    listener_threads_after_close = [t for t in threading.enumerate() if t.name == "central-logger-listener"]
    assert listener_threads_after_close == []


# ---------------------------------------------------------------------------
# LOG-27 — thread_safety.md acceptance #4 + crash_safety.md acceptance #4:
# with `QUEUE_MAXSIZE` patched to 100 and the stdout handler's `emit` blocked,
# 250 records overflow the queue -> drops counted (149 or 150), exactly one
# rate-limited stderr heartbeat, no crash; releasing the block drains
# everything that wasn't dropped.
# ---------------------------------------------------------------------------


def test_log_27_queue_overflow_drops_are_counted_with_one_heartbeat_and_drains_after_release(
    central_log, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per the task's LOG-27 special note: `QUEUE_MAXSIZE` (a module-level
    constant on `logger.logger`) is patched down to 100 via
    `monkeypatch.setattr`, instead of emitting the ~10,050 real records the
    default 10_000-slot queue would need — keeps this test fast. The stdout
    handler's `emit` is blocked on a `threading.Event` so the single
    listener thread cannot drain anything while 250 records are enqueued
    from this thread, forcing the queue past `maxsize`.

    Before `emit` is blocked, `get_logger()` (the first bootstrap trigger)
    is called and then `flush()` is used to drain whatever bootstrap
    already queued, using the still-unpatched (non-blocking) `emit`.
    Without this drain, a record still sitting in the queue (a race against
    the listener thread dequeuing it) at the moment `emit` gets patched to
    block would shift the drop count outside {149, 150}.
    """

    import logger.logger as logger_module

    monkeypatch.setattr(logger_module, "QUEUE_MAXSIZE", 100, raising=False)

    central_log.configure()

    import logger.handlers as handlers_module
    from logger.logger import INFO, dropped_count, flush, get_logger, log_event

    logger_ = get_logger("logger-tests.concurrency.overflow")

    # Drain anything already in flight with the real `emit` before
    # blocking it — see the docstring note above.
    assert flush(timeout=5.0) is True

    release = threading.Event()
    original_emit = handlers_module.StdoutHandler.emit

    def blocking_emit(self, record):
        release.wait(timeout=10)
        return original_emit(self, record)

    monkeypatch.setattr(handlers_module.StdoutHandler, "emit", blocking_emit)

    try:
        # Emitting past queue capacity must never raise.
        for i in range(250):
            log_event(logger_, INFO, "test.overflow", i=i)

        assert dropped_count() in (149, 150)

        heartbeat_lines = [
            line for line in central_log.stderr().splitlines() if "[central-logger]" in line and "queue" in line.lower()
        ]
        assert len(heartbeat_lines) == 1
    finally:
        # Always release the blocked listener thread, even on assertion
        # failure — otherwise it stays blocked for up to 10s, slowing down
        # the rest of the suite.
        release.set()

    assert flush(timeout=10.0) is True


# ---------------------------------------------------------------------------
# LOG-28 — thread_safety.md acceptance #5 + "Shutdown & data loss": a
# hard-killed subprocess (`os._exit`, skipping the atexit hook) may lose
# queued-but-unflushed records (documented, not a bug); a subprocess that
# exits normally without an explicit `close()` relies on the `atexit` hook
# and must have every record present.
# ---------------------------------------------------------------------------


def _run_subprocess_logging(*, hard_kill: bool) -> str:
    """Run a 20-record logging script in a subprocess; return its stdout."""

    script = (
        "import os\n"
        "from logger.logger import get_logger, log_event, INFO\n"
        "logger_ = get_logger('logger-tests.concurrency.subprocess')\n"
        "for i in range(20):\n"
        "    log_event(logger_, INFO, 'test.subprocess_record', i=i)\n"
        f"if {hard_kill!r}:\n"
        "    os._exit(0)\n"
        # else: fall off the end -> normal interpreter shutdown runs the
        # `atexit` hook registered by bootstrap.
    )

    env = os.environ.copy()
    env["LOGS_LEVEL"] = "INFO"
    env["LOGS_STDOUT_LEVEL"] = "INFO"

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"subprocess exited with {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def _count_subprocess_test_records(stdout: str) -> int:
    """Count only the 'test.subprocess_record' JSON lines on `stdout`.

    A hard kill can cut the last line short, so a line that does not parse
    is skipped rather than failing the count."""

    count = 0
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if record.get("event") == "test.subprocess_record":
            count += 1
    return count


def test_log_28_hard_kill_may_lose_unflushed_records_documented_behavior() -> None:
    stdout = _run_subprocess_logging(hard_kill=True)
    lines_written = _count_subprocess_test_records(stdout)

    # Documented loss window (thread_safety.md "Shutdown & data loss"): a
    # hard exit skips the atexit-driven flush/close, so whatever the
    # listener thread hadn't yet written when os._exit() fired may be
    # missing. The assertion is the bound (never more than emitted, never
    # a crash/hang/negative count) rather than an exact number, because
    # the loss itself is inherently timing-dependent — that is the point
    # of this acceptance test.
    assert 0 <= lines_written <= 20


def test_log_28_normal_exit_without_explicit_close_flushes_everything_via_atexit() -> None:
    stdout = _run_subprocess_logging(hard_kill=False)
    lines_written = _count_subprocess_test_records(stdout)

    assert lines_written == 20
