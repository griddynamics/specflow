"""A deliberately changed ``ParseDocumentWorkflow`` for the negative replay test (no tests here)."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal.documents.models import ParseDocumentInput, ParseDocumentResult


@workflow.defn(name="ParseDocumentWorkflow")
class ReorderedParseWorkflow:
    """Schedules a different activity first: replay of the real history must fail."""

    @workflow.run
    async def run(self, data: ParseDocumentInput) -> ParseDocumentResult:
        await workflow.sleep(timedelta(seconds=1))
        return await workflow.execute_activity(
            "parse_document",
            data,
            result_type=ParseDocumentResult,
            start_to_close_timeout=timedelta(minutes=45),
        )
