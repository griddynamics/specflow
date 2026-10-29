"""Gating helpers and the tier-2 Temporal fixture for the live test tiers.

``requires_live("POSTGRES")`` skips a test unless ``SPECFLOW_TEST_POSTGRES=1``.
The ``temporal_live`` fixture connects to the local ``specflow-local``
namespace and cleans up behind the test. It never starts a test server:
``start_time_skipping()`` and ``start_local()`` download a binary and are not
used anywhere (SPECS AC-16.0).
"""

from __future__ import annotations

import itertools
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any, Final

import pytest
import pytest_asyncio
from temporalio.client import Client
from temporalio.service import RPCError

LIVE_NAMES: Final = ("POSTGRES", "GCS", "S3", "NEO4J", "TEMPORAL", "LLM_GATEWAY")
LOCAL_NAMESPACE: Final = "specflow-local"

_RUN_ID: Final = uuid.uuid4().hex[:8]
_QUEUE_COUNTER = itertools.count(1)


def live_enabled(name: str) -> bool:
    """True when ``SPECFLOW_TEST_<name>=1`` is set."""

    if name not in LIVE_NAMES:
        raise ValueError(f"Unknown live tier {name!r}; expected one of {LIVE_NAMES}")
    return os.environ.get(f"SPECFLOW_TEST_{name}") == "1"


def requires_live(name: str) -> pytest.MarkDecorator:
    """Skip unless ``SPECFLOW_TEST_<name>=1``; use as a decorator or ``pytestmark``."""

    if name not in LIVE_NAMES:
        raise ValueError(f"Unknown live tier {name!r}; expected one of {LIVE_NAMES}")
    return pytest.mark.skipif(
        not live_enabled(name),
        reason=f"set SPECFLOW_TEST_{name}=1 (run with `--env-file ../.env.local`) to run this live test",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip every test that uses the ``temporal_live`` fixture unless the live tier targets the local namespace."""

    namespace = os.environ.get("TEMPORAL_NAMESPACE", LOCAL_NAMESPACE).strip()
    gates = (
        pytest.mark.skipif(
            not live_enabled("TEMPORAL"),
            reason="set SPECFLOW_TEST_TEMPORAL=1 to run live Temporal tests",
        ),
        pytest.mark.skipif(
            namespace != LOCAL_NAMESPACE,
            reason=f"live Temporal tests only run in the {LOCAL_NAMESPACE} namespace",
        ),
    )
    for item in items:
        if isinstance(item, pytest.Function) and "temporal_live" in item.fixturenames:
            for gate in gates:
                item.add_marker(gate)


class TemporalLive:
    """A client on the local namespace, a unique queue and a workflow-id prefix.

    ``queue_number`` makes the queue ``pytest-<run>-<n>``; workflow ids carry
    the prefix ``pytest-<run>-``. Every handle started through ``start`` or
    registered with ``track`` is terminated by ``cleanup``.
    """

    def __init__(self, client: Any, run_id: str, queue_number: int) -> None:
        self.client = client
        self.run_id = run_id
        self.id_prefix = f"pytest-{run_id}-"
        self.task_queue = f"pytest-{run_id}-{queue_number}"
        self._handles: list[Any] = []

    def workflow_id(self, name: str | None = None) -> str:
        return f"{self.id_prefix}{name or uuid.uuid4().hex[:12]}"

    def track(self, handle: Any) -> Any:
        self._handles.append(handle)
        return handle

    async def start(self, workflow: Any, arg: Any = None, *, workflow_id: str | None = None, **options: Any) -> Any:
        """Start a workflow on this test's queue with a prefixed id; tracked for cleanup."""

        options.setdefault("task_queue", self.task_queue)
        args = () if arg is None else (arg,)
        handle = await self.client.start_workflow(workflow, *args, id=self.workflow_id(workflow_id), **options)
        return self.track(handle)

    async def cleanup(self) -> int:
        """Terminate every tracked workflow that is still running; return how many tried."""

        tried = 0
        for handle in reversed(self._handles):
            tried += 1
            # Already closed or unknown workflows are fine: the goal is none running.
            with suppress(RPCError):
                await handle.terminate("pytest teardown")
        self._handles.clear()
        return tried


async def connect_local_temporal() -> Any:
    """Connect to ``TEMPORAL_ADDRESS`` in the ``specflow-local`` namespace."""

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip()
    return await Client.connect(address, namespace=LOCAL_NAMESPACE)


@pytest_asyncio.fixture
async def temporal_live() -> AsyncIterator[TemporalLive]:
    """Tier-2 fixture: skipped unless ``SPECFLOW_TEST_TEMPORAL=1``."""

    client = await connect_local_temporal()
    live = TemporalLive(client, _RUN_ID, next(_QUEUE_COUNTER))
    try:
        yield live
    finally:
        await live.cleanup()
