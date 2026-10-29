"""``ParseDocumentWorkflow`` (SPECS section 11).

Sandbox rules: only ``temporalio`` and the pydantic-only ``models`` module are
imported here, the latter inside ``imports_passed_through()``. The activities
are scheduled by name, so the database, storage and parser packages are never
imported into the workflow sandbox.

Failure handling: a failed ``parse_document`` (non-retryable, or retries
exhausted) runs ``mark_document_failed`` with a FIXED code, then the workflow
fails with a non-retryable ``ApplicationError`` whose message is that code.
Cancellation is never swallowed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError

with workflow.unsafe.imports_passed_through():
    from temporal.documents.models import (
        MarkFailedInput,
        ParseDocumentInput,
        ParseDocumentResult,
    )

WORKFLOW_NAME: Final = "ParseDocumentWorkflow"
PARSE_ACTIVITY: Final = "parse_document"
MARK_FAILED_ACTIVITY: Final = "mark_document_failed"
FAILURE_TYPE: Final = "ParseDocumentFailed"

#: Caller-side start option (the API passes it as ``execution_timeout``).
EXECUTION_TIMEOUT: Final = timedelta(minutes=60)
PARSE_START_TO_CLOSE: Final = timedelta(minutes=45)
PARSE_HEARTBEAT_TIMEOUT: Final = timedelta(seconds=60)
MARK_START_TO_CLOSE: Final = timedelta(seconds=30)

#: Activity failure type -> the fixed code stored on the document.
FAILURE_CODES: Final = {
    "DocumentNotFound": "document_not_found",
    "PdfParseError": "pdf_parse_failed",
    "OriginalNotFound": "original_not_found",
}
NON_RETRYABLE_TYPES: Final = list(FAILURE_CODES)
INTERNAL_ERROR: Final = "internal_error"

PARSE_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_attempts=3,
    non_retryable_error_types=NON_RETRYABLE_TYPES,
)
MARK_RETRY: Final = RetryPolicy(maximum_attempts=5)


def failure_code(error: ActivityError) -> str:
    """The fixed code for a failed ``parse_document`` (never derived from messages)."""

    cause = error.cause
    if isinstance(cause, ApplicationError) and cause.type in FAILURE_CODES:
        return FAILURE_CODES[cause.type]
    return INTERNAL_ERROR


@workflow.defn(name=WORKFLOW_NAME)
class ParseDocumentWorkflow:
    @workflow.run
    async def run(self, data: ParseDocumentInput) -> ParseDocumentResult:
        try:
            result: ParseDocumentResult = await workflow.execute_activity(
                PARSE_ACTIVITY,
                data,
                result_type=ParseDocumentResult,
                start_to_close_timeout=PARSE_START_TO_CLOSE,
                heartbeat_timeout=PARSE_HEARTBEAT_TIMEOUT,
                retry_policy=PARSE_RETRY,
            )
        except ActivityError as error:
            if isinstance(error.cause, CancelledError):
                raise
            code = failure_code(error)
        else:
            return result
        # Outside the handler: the marking failure must not chain the parse error text.
        await self._mark_failed(data, code)
        raise ApplicationError(code, type=FAILURE_TYPE, non_retryable=True)

    async def _mark_failed(self, data: ParseDocumentInput, code: str) -> None:
        try:
            await workflow.execute_activity(
                MARK_FAILED_ACTIVITY,
                MarkFailedInput(
                    project_id=data.project_id,
                    document_id=data.document_id,
                    error_code=code,
                ),
                start_to_close_timeout=MARK_START_TO_CLOSE,
                retry_policy=MARK_RETRY,
            )
        except ActivityError as error:
            if isinstance(error.cause, CancelledError):
                raise
            # The row stays `processing`; a fresh run can be started (AC-8.6).
