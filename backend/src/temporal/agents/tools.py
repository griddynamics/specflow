"""The summarizer's function tools (SPECS section 12, M1/H2).

Each tool is a workflow-side ``@function_tool(failure_error_function=None)``
that awaits ``workflow.execute_activity``. The ids it sends come ONLY from the
run context (``DocumentRunContext``), which the workflow builds from its own
input: no tool parameter carries an id, SQL, Cypher or a key, so the model can
never choose the project or the document. Tool results never contain the
project or document id either.

``failure_error_function=None`` makes a failed activity raise out of
``Runner.run`` (caught by the workflow) instead of becoming a tool message, so
no error text ever reaches the model.

Activities are scheduled by NAME: this module (imported into the workflow
sandbox) never imports the database or storage packages.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Final

from agents import RunContextWrapper, function_tool
from temporalio import workflow
from temporalio.common import RetryPolicy

from temporal.agents.models import (
    CHUNK_LIMIT,
    DocumentChunk,
    DocumentRunContext,
    ReadChunkInput,
)

READ_CHUNK_ACTIVITY: Final = "read_document_chunk"
SAVE_SUMMARIZATION_ACTIVITY: Final = "save_summarization"
MARK_FAILED_ACTIVITY: Final = "mark_summarization_failed"

READ_DOCUMENT: Final = "read_document"
SUBMIT_SUMMARY: Final = "submit_summary"

TOOL_START_TO_CLOSE: Final = timedelta(seconds=30)
TOOL_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)


def _context(ctx: RunContextWrapper[DocumentRunContext]) -> DocumentRunContext:
    return ctx.context


@function_tool(failure_error_function=None)
async def read_document(ctx: RunContextWrapper[DocumentRunContext], offset: int = 0) -> str:
    """Read up to 16000 characters of the document, starting at ``offset``.

    Args:
        offset: Character offset to start from; use the previous result's next_offset.
    """

    run = _context(ctx)
    chunk: DocumentChunk = await workflow.execute_activity(
        READ_CHUNK_ACTIVITY,
        ReadChunkInput(
            project_id=run.project_id,
            document_id=run.document_id,
            offset=max(offset, 0),
            limit=CHUNK_LIMIT,
        ),
        result_type=DocumentChunk,
        start_to_close_timeout=TOOL_START_TO_CLOSE,
        retry_policy=TOOL_RETRY,
        summary="tool:read_document",
    )
    return chunk.model_dump_json()


@function_tool(failure_error_function=None)
async def submit_summary(title: str, summary: str) -> str:
    """Submit the final answer. Call this last, exactly once.

    Args:
        title: The document title, at most 200 characters.
        summary: The summary, at most 300 words.
    """

    # Validated by the workflow (SummaryOutput); an invalid answer -> agent_failed.
    return json.dumps({"title": title, "summary": summary})


SUMMARIZER_TOOLS: Final = (read_document, submit_summary)
