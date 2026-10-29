"""Tier 2: AC-8.6, SIGKILL a worker process mid-parse (about 70 s)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from logger.logger import INFO, get_logger, log_event
from specflow_live import requires_live
from specflow_parse_live_support import (
    Stack,
)
from specflow_parse_support import PDF_FIXTURE

MARKER_POLL_SECONDS = 0.5
pytest_plugins = ["specflow_db_live_support", "specflow_parse_live_support"]
pytestmark = [requires_live("TEMPORAL"), requires_live("POSTGRES"), requires_live("GCS")]
logger = get_logger("tests.specflow_parse_live_kill")
KILL_TEST_WAIT_SECONDS = 150


async def test_killing_the_worker_mid_parse_retries_within_the_heartbeat_timeout(stack: Stack, tmp_path: Path):
    """AC-8.6: SIGKILL a worker process mid-parse; heartbeat timeout (60 s) then a retry succeeds."""

    data = await stack.seed(PDF_FIXTURE.read_bytes())
    marker = tmp_path / "parsing.flag"
    env = {
        **os.environ,
        "PARSE_TEST_DATABASE": stack.database.name,
        "PARSE_TEST_PREFIX": stack.prefix,
        "PARSE_TEST_QUEUE": stack.temporal.task_queue,
        "PARSE_TEST_MARKER": str(marker),
        "TEMPORAL_NAMESPACE": "specflow-local",
        "PYTHONPATH": os.pathsep.join(sys.path),
    }
    doomed = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "specflow_parse_support",
        "600",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        handle = await stack.start(data)
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert doomed.returncode is None, "the doomed worker exited early"
            assert time.monotonic() < deadline, "the parse never started"
            await asyncio.sleep(MARKER_POLL_SECONDS)
        doomed.kill()  # SIGKILL: no heartbeat, no cancellation
        killed_at = time.monotonic()
        await asyncio.wait_for(doomed.wait(), 10)

        worker, built = stack.worker()
        async with worker:
            result = await asyncio.wait_for(handle.result(), KILL_TEST_WAIT_SECONDS)
        await built.close()
    finally:
        if doomed.returncode is None:
            doomed.kill()

    elapsed = time.monotonic() - killed_at
    log_event(logger, INFO, "parse.live.retry_after_kill", seconds=round(elapsed, 1))
    assert result.status == "parsed"
    assert 50 < elapsed < 100  # heartbeat timeout 60 s + retry interval 5 s
    assert (await stack.row(data)).status == "parsed"
    # A retried attempt leaves no timeout event; the started event carries the attempt.
    events = json.loads((await handle.fetch_history()).to_json())["events"]
    attempts = [
        e["activityTaskStartedEventAttributes"]["attempt"] for e in events if "activityTaskStartedEventAttributes" in e
    ]
    assert attempts == [2]
