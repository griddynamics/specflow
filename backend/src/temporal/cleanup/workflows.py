"""``SourceCleanupWorkflow``: the safety net after a source removal (SPECS 6.2).

Run: sleep 60 s -> sweep -> sleep 44 min -> sweep. The second sweep runs about 45
minutes after the removal, after the 30 min activity limit of an import file, so a
late write that survived the fence is still caught [R21]. The delays are start
arguments (``SourceCleanupInput``), so the workflow stays deterministic on replay
and tests can use seconds.

Sandbox rules as ``DriveImportWorkflow``: only ``temporalio`` and the pydantic-only
models are imported; the activity is scheduled by name. History carries ids and
counts only.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from temporal.cleanup.models import SourceCleanupInput, SweepInput, SweepResult

WORKFLOW_NAME: Final = "SourceCleanupWorkflow"
SWEEP_ACTIVITY: Final = "sweep_source_objects"

FIRST_SWEEP_DELAY: Final = timedelta(seconds=60)
SECOND_SWEEP_DELAY: Final = timedelta(minutes=44)
SWEEP_START_TO_CLOSE: Final = timedelta(minutes=10)
SWEEP_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=5)
#: Caller-side start option (the API passes it as ``execution_timeout``). Worst case:
#: 60 s + 44 min + 2 sweeps x 5 attempts x 10 min = about 2 h 25 min, so 3 h.
EXECUTION_TIMEOUT: Final = timedelta(hours=3)


@workflow.defn(name=WORKFLOW_NAME)
class SourceCleanupWorkflow:
    @workflow.run
    async def run(self, data: SourceCleanupInput) -> SweepResult:
        await workflow.sleep(timedelta(seconds=data.first_delay_s))
        # A short outage right after the removal must not cost sweep 2 (the
        # activity logs ``source.cleanup.failed`` on each failed attempt).
        with suppress(ActivityError):
            await self._sweep(data, 1)
        await workflow.sleep(timedelta(seconds=data.second_delay_s))
        return await self._sweep(data, 2)

    async def _sweep(self, data: SourceCleanupInput, sweep: int) -> SweepResult:
        result: SweepResult = await workflow.execute_activity(
            SWEEP_ACTIVITY,
            SweepInput(
                project_id=data.project_id,
                source_id=data.source_id,
                document_ids=data.document_ids,
                id_prefix=data.id_prefix,
                sweep=sweep,
            ),
            result_type=SweepResult,
            start_to_close_timeout=SWEEP_START_TO_CLOSE,
            retry_policy=SWEEP_RETRY,
        )
        return result
