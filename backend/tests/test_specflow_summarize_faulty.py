"""A summarize workflow variant that fails in workflow code (AC-9.8). No tests here.

Kept in its own small module: the workflow sandbox re-imports the module that
defines a workflow, so it must import nothing but the production workflow.
"""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal.agents.models import (
        SummarizeDocumentInput,
        SummarizeDocumentResult,
    )
    from temporal.agents.workflows import SummarizeDocumentWorkflow


@workflow.defn(name="SummarizeDocumentWorkflowFaulty")
class FaultySummarizeWorkflow(SummarizeDocumentWorkflow):
    """Raises an unexpected (non-Temporal) exception right after ``Runner.run``."""

    @workflow.run
    async def run(self, data: SummarizeDocumentInput) -> SummarizeDocumentResult:
        return await super().run(data)

    def _validated_output(self, _result):
        raise RuntimeError("injected unexpected failure after Runner.run")
