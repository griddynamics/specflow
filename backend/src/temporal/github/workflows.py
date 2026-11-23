"""``GithubSnapshotWorkflow``: one clone check of one project repository (docs/github/clone.md).

One activity does the check and records a closed failure on the row itself. When that activity fails anyway (a crash,
a heartbeat or start-to-close timeout, two unexpected errors) or the workflow is cancelled, ``github_snapshot_fail``
ends the row ``failed`` with ``github_clone_failed``, so no row stays ``cloning``.

Sandbox rules as the other workflows: only ``temporalio`` and the pydantic models are imported; activities are
scheduled by name. History carries ids and codes only.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal.github.models import (
        WORKFLOW_NAME,
        GitHubSnapshotFailInput,
        GitHubSnapshotInput,
        GitHubSnapshotResult,
    )

SNAPSHOT_ACTIVITY: Final = "github_snapshot"
FAIL_ACTIVITY: Final = "github_snapshot_fail"
FAILURE_TYPE: Final = "GitHubSnapshotFailed"
CLONE_FAILED: Final = "github_clone_failed"
#: Attempts of the check: an unexpected error gets one more; the last attempt records it on the row.
SNAPSHOT_MAX_ATTEMPTS: Final = 2
#: The clone (10 minutes at most), the stats, the archive and the upload; two attempts fit the 45-minute execution.
SNAPSHOT_START_TO_CLOSE: Final = timedelta(minutes=20)
SNAPSHOT_HEARTBEAT_TIMEOUT: Final = timedelta(minutes=2)
SNAPSHOT_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=10), maximum_attempts=SNAPSHOT_MAX_ATTEMPTS)
FAIL_START_TO_CLOSE: Final = timedelta(minutes=1)
FAIL_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=5)


@workflow.defn(name=WORKFLOW_NAME)
class GithubSnapshotWorkflow:
    @workflow.run
    async def run(self, data: GitHubSnapshotInput) -> GitHubSnapshotResult:
        try:
            result: GitHubSnapshotResult = await workflow.execute_activity(
                SNAPSHOT_ACTIVITY,
                data,
                result_type=GitHubSnapshotResult,
                start_to_close_timeout=SNAPSHOT_START_TO_CLOSE,
                heartbeat_timeout=SNAPSHOT_HEARTBEAT_TIMEOUT,
                retry_policy=SNAPSHOT_RETRY,
            )
        except asyncio.CancelledError:
            await self._mark_failed(data)
            raise
        except ActivityError:
            pass  # outside the handler below: nothing chains the activity's failure
        else:
            return result
        await self._mark_failed(data)
        raise ApplicationError(CLONE_FAILED, type=FAILURE_TYPE, non_retryable=True)

    @staticmethod
    async def _mark_failed(data: GitHubSnapshotInput) -> None:
        await workflow.execute_activity(
            FAIL_ACTIVITY,
            GitHubSnapshotFailInput(project_id=data.project_id, repository_id=data.repository_id, code=CLONE_FAILED),
            start_to_close_timeout=FAIL_START_TO_CLOSE,
            retry_policy=FAIL_RETRY,
        )
