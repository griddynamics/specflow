"""``SummarizeDocumentWorkflow`` (SPECS sections 11 and 12).

The workflow runs the summarizer agent with closure-bound tools (ids only from
the run context), validates the ``submit_summary`` answer as ``SummaryOutput``
and saves it itself through the trusted ``save_summarization`` activity (app
role).

Catch strategy (SP-A; an unexpected exception in workflow code would make the
workflow TASK retry in a loop):

- agent block (``Runner.run`` + validation): ``AgentsException``,
  ``AgentsWorkflowError``, ``ActivityError``, ``ValidationError`` ->
  ``tool_failed`` when an activity failed, else ``agent_failed``;
- anything else (``Exception``) -> ``internal_error``;
- every handler runs ``mark_summarization_failed`` with that fixed code and
  raises a non-retryable ``ApplicationError(type="SummarizationFailed")``
  without a cause, so no error text reaches history, the model or Langfuse;
- a cancellation is re-raised untouched, and ``BaseException`` is never
  caught, so cancel, eviction and continue-as-new keep working.

The API starts the workflow by name with id ``doc-summary-{document_id}``,
``USE_EXISTING`` and ``EXECUTION_TIMEOUT`` (start options, not set here).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import ValidationError
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    is_cancelled_exception,
)

with workflow.unsafe.imports_passed_through():
    from agents import AgentsException, ModelSettings, RunConfig, Runner, trace
    from agents.result import RunResult
    from openinference.instrumentation import using_attributes
    from temporalio.openai_agents import AgentsWorkflowError

    from agent_runtime.summarizer import SUMMARIZE_PROMPT, build_summarizer_agent
    from temporal.agents.models import (
        DocumentRunContext,
        MarkFailedInput,
        SaveSummarizationInput,
        SummarizeDocumentInput,
        SummarizeDocumentResult,
        SummaryOutput,
        UsageSummary,
    )
    from temporal.agents.tools import (
        MARK_FAILED_ACTIVITY,
        SAVE_SUMMARIZATION_ACTIVITY,
        TOOL_START_TO_CLOSE,
    )
    from temporal.containment import Contain

WORKFLOW_NAME: Final = "SummarizeDocumentWorkflow"
TRACE_NAME: Final = "summarize-document"
FAILURE_TYPE: Final = "SummarizationFailed"
MAX_TURNS: Final = 12

#: Caller-side start option (the API passes it as ``execution_timeout``).
EXECUTION_TIMEOUT: Final = timedelta(minutes=15)

TOOL_FAILED: Final = "tool_failed"
AGENT_FAILED: Final = "agent_failed"
INTERNAL_ERROR: Final = "internal_error"

AGENT_ERRORS: Final = (
    AgentsException,
    AgentsWorkflowError,
    ActivityError,
    ValidationError,
)
SAVE_RETRY: Final = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5)
CAUSE_DEPTH: Final = 8
NUL: Final = "\x00"


def failure_code(error: BaseException) -> str:
    """``tool_failed`` iff an activity failed (directly or as the cause), else ``agent_failed``."""

    if isinstance(error, ActivityError) or isinstance(error.__cause__, ActivityError):
        return TOOL_FAILED
    return AGENT_FAILED


def cancellation_in(error: BaseException) -> BaseException | None:
    """The cancellation inside ``error``'s cause chain, if any (it must propagate)."""

    current: BaseException | None = error
    for _ in range(CAUSE_DEPTH):
        if current is None:
            return None
        if is_cancelled_exception(current):
            return current
        current = current.__cause__
    return None


def build_run_config(data: SummarizeDocumentInput) -> RunConfig:
    """The run config; tracing of prompts and outputs follows ``trace_sensitive_data``."""

    return RunConfig(
        model_settings=ModelSettings(preserve_raw_usage=True),
        trace_include_sensitive_data=data.trace_sensitive_data,
    )


def usage_of(result: RunResult) -> UsageSummary:
    usage = result.context_wrapper.usage
    return UsageSummary(
        requests=usage.requests,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )


@workflow.defn(name=WORKFLOW_NAME)
class SummarizeDocumentWorkflow:
    @workflow.run
    async def run(self, data: SummarizeDocumentInput) -> SummarizeDocumentResult:
        # run_id = the Temporal run id (unique per execution).
        context = DocumentRunContext(
            project_id=data.project_id,
            run_id=workflow.info().run_id,
            document_id=data.document_id,
        )
        code: str = INTERNAL_ERROR  # an agent error below names a finer code
        with Contain() as contained:  # never BaseException (cancel, eviction)
            try:
                result = await self._run_agent(data, context)
                output = self._validated_output(result)
            except AGENT_ERRORS as error:
                _reraise_cancellation(error)
                code = failure_code(error)
            else:
                return await self._finish(data, result, output)
        if contained.error is not None:
            _reraise_cancellation(contained.error)
            code = INTERNAL_ERROR
        # Outside every handler: nothing chains the original error text.
        await self._mark_failed(data, code)
        raise ApplicationError(code, type=FAILURE_TYPE, non_retryable=True)

    async def _run_agent(self, data: SummarizeDocumentInput, context: DocumentRunContext) -> RunResult:
        workflow_id = workflow.info().workflow_id  # Langfuse sessionId (AC-11.3)
        metadata = {
            "workflow_id": workflow_id,
            "run_id": context.run_id,
            "requested_by": str(data.requested_by),
        }
        run_config = build_run_config(data)
        with (
            using_attributes(session_id=workflow_id, metadata=metadata),
            trace(TRACE_NAME, group_id=workflow_id, metadata=metadata),
        ):
            return await Runner.run(
                build_summarizer_agent(data.model),
                SUMMARIZE_PROMPT,
                context=context,
                max_turns=MAX_TURNS,
                run_config=run_config,
            )

    def _validated_output(self, result: RunResult) -> SummaryOutput:
        final = result.final_output
        if isinstance(final, SummaryOutput):
            output = final
        else:
            output = SummaryOutput.model_validate_json(final if isinstance(final, str) else "null")
        # Postgres rejects NUL in text; a clean agent_failed beats 5 save retries.
        if NUL in output.title or NUL in output.summary:
            raise AgentsException("invalid_output")
        return output

    async def _finish(
        self,
        data: SummarizeDocumentInput,
        result: RunResult,
        output: SummaryOutput,
    ) -> SummarizeDocumentResult:
        await workflow.execute_activity(
            SAVE_SUMMARIZATION_ACTIVITY,
            SaveSummarizationInput(
                project_id=data.project_id,
                document_id=data.document_id,
                summary=output.summary,
                title=output.title,
                model=data.model,
                workflow_id=workflow.info().workflow_id,
                requested_by=data.requested_by,
            ),
            start_to_close_timeout=TOOL_START_TO_CLOSE,
            retry_policy=SAVE_RETRY,
            summary="save_summarization",
        )
        return SummarizeDocumentResult(
            document_id=data.document_id,
            summary_chars=len(output.summary),
            title=output.title,
            model=data.model,
            usage=usage_of(result),
        )

    async def _mark_failed(self, data: SummarizeDocumentInput, code: str) -> None:
        await workflow.execute_activity(
            MARK_FAILED_ACTIVITY,
            MarkFailedInput(
                project_id=data.project_id,
                document_id=data.document_id,
                error_code=code,
            ),
            start_to_close_timeout=TOOL_START_TO_CLOSE,
            retry_policy=SAVE_RETRY,
            summary="mark_summarization_failed",
        )


def _reraise_cancellation(error: BaseException) -> None:
    cancelled = cancellation_in(error)
    if cancelled is not None:
        raise cancelled
