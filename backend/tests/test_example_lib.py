from __future__ import annotations

import re

import pytest

import example_lib
from example_lib import capture_failure, run_example
from logger.logger import INFO, get_logger, log_event


def test_run_example_binds_a_fresh_32_hex_request_id_to_every_record(central_log) -> None:
    """INT-28: every record logged inside the wrapped coroutine carries the
    same fresh 32-hex request_id that run_example's new_request_context()
    bound for this run."""

    central_log.configure()

    async def main() -> str:
        log_event(get_logger("examples.probe"), INFO, "probe.ran")
        return "ok"

    result = run_example(main)

    assert result == "ok"
    records = central_log.records()
    assert records, "expected run_example's coroutine to have logged something"
    # Every record was logged from inside run_example's
    # new_request_context(), so all of them share one id.
    request_ids = {record.get("request_id") for record in records}
    assert len(request_ids) == 1, f"expected one request_id, got {request_ids}"
    (request_id,) = request_ids
    assert isinstance(request_id, str)
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)


def test_run_example_closes_the_logging_pipeline_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INT-28: run_example always calls close() exactly once in its
    finally, win or lose, so the process never leaves the queue listener
    running past this call."""

    close_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(example_lib, "close", lambda *a, **_kw: close_calls.append(a))

    async def main() -> int:
        return 7

    result = run_example(main)

    assert result == 7
    assert len(close_calls) == 1


def test_run_example_reports_a_raising_coroutine_as_example_failed(central_log) -> None:
    """INT-28: a coroutine that raises is caught only by run_example's
    outermost handler — one ERROR example.failed record with the
    traceback, and a return value of 1 (never a bare, unhandled
    traceback)."""

    central_log.configure()

    async def main() -> None:
        raise RuntimeError("boom")

    result = run_example(main)

    assert result == 1
    failures = central_log.events("example.failed")
    assert len(failures) == 1, f"expected exactly 1, got {failures}"
    assert failures[0]["levelname"] == "ERROR"
    assert failures[0].get("exc_type") == "RuntimeError"


@pytest.mark.asyncio
async def test_capture_failure_returns_value_or_exception() -> None:
    async def ok() -> int:
        return 3

    async def bad() -> int:
        raise ValueError("boom")

    assert await capture_failure(ok()) == (3, None)
    value, error = await capture_failure(bad())
    assert value is None
    assert isinstance(error, ValueError)
