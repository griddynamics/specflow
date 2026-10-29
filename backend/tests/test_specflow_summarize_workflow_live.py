"""Tier 2: ``SummarizeDocumentWorkflow`` with a scripted TestModel against the
live local Temporal and a scratch Postgres (session test agent role, RLS).

Gates: ``SPECFLOW_TEST_TEMPORAL=1 SPECFLOW_TEST_POSTGRES=1``
(run with ``uv run --env-file ../.env.local``). ``SPECFLOW_RECORD_FIXTURES=1``
also rewrites the replay fixtures ``fixtures/temporal/summarize_*.json``.
No LLM gateway call is made here.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest
from agents import set_trace_processors
from temporalio import activity
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import ApplicationError, CancelledError

from settings import load_settings
from specflow_live import requires_live
from specflow_summarize_live_support import (
    count_events,
    happy_calls,
    happy_script,
    history_events,
    record_fixture,
    scheduled_activities,
    scripted,
    secret_values,
    summarize_stack,
    synthetic_markdown,
)
from temporal.agents.models import (
    DocumentChunk,
    ReadChunkInput,
    SummarizeDocumentInput,
)
from temporal.agents.workflows import SummarizeDocumentWorkflow
from test_specflow_summarize_faulty import FaultySummarizeWorkflow

pytest_plugins = ["specflow_db_live_support"]
SLOW_ACTIVITY_SECONDS = 30
pytestmark = [
    requires_live("TEMPORAL"),
    requires_live("POSTGRES"),
]

MARKER = "PERMANENT-READ-FAILURE-TEXT-9c41"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


@pytest.fixture
def no_trace_export(monkeypatch):
    """TestModel runs export nothing (no default OpenAI exporter, P12)."""

    monkeypatch.setenv("OPENAI_AGENTS_DISABLE_TRACING", "1")
    set_trace_processors([])


@pytest.fixture
def worker_settings(valid_env):
    env = {
        **valid_env,
        "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        "TEMPORAL_NAMESPACE": "specflow-local",
    }
    return load_settings("worker", env)


@pytest.fixture
def stack_args(worker_settings, temporal_live, migrated_database, agent_role, no_trace_export):
    return {
        "settings": worker_settings,
        "temporal_live": temporal_live,
        "database": migrated_database,
        "agent_role": agent_role,
    }


async def _failure(handle) -> ApplicationError:
    with pytest.raises(WorkflowFailureError) as caught:
        await asyncio.wait_for(handle.result(), 60)
    return caught.value.cause


# ------------------------------------------------------- AC-9.2, AC-8.4
async def test_the_scripted_agent_summarizes_and_saves(stack_args):
    provider = happy_script()
    async with summarize_stack(model_provider=provider, **stack_args) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("happy"))
        handle = await stack.start(seeded)
        result = await asyncio.wait_for(handle.result(), 60)

        row = await stack.document_row(seeded)
        events = await history_events(handle)
        await record_fixture(handle, "success")

    assert (result.title, result.summary_chars, result.model) == (
        "Synthetic widget pipeline",
        len("Five stages."),
        "test/model",
    )
    assert result.usage.requests >= 0  # TestModel reports no usage; 3 model calls below
    assert (row.summarization_status, row.summarization, row.summarization_model) == (
        "succeeded",
        "Five stages.",
        "test/model",
    )
    assert row.summarization_error is None and row.summarized_at is not None

    scheduled = scheduled_activities(events)
    assert [name for name, _ in scheduled if name != "invoke_model_activity"] == [
        "read_document_chunk",
        "read_document_chunk",
        "save_summarization",
    ]
    assert sum(name == "invoke_model_activity" for name, _ in scheduled) == 3
    # Ids in every tool activity are the workflow input's (from the run context).
    for name, data in scheduled:
        if name == "read_document_chunk":
            assert str(seeded.project_id) in data and str(seeded.document_id) in data
    # AC-8.4: markdown only in tool chunks / model turns; no secrets; payloads < 2 MB.
    for name, data in scheduled:
        if name == "save_summarization":
            assert "CANARY-happy" not in data
    raw = b"".join(event.SerializeToString() for event in events)
    for value in secret_values():
        assert value.encode() not in raw
    assert all(event.ByteSize() < MAX_PAYLOAD_BYTES for event in events)
    assert count_events(events, "workflow_task_failed_event_attributes") == 0


# ---------------------------------------------------------------- AC-9.4
async def test_a_rerun_overwrites_the_summary(stack_args):
    provider = scripted(
        *happy_calls("First title", "First summary."),
        *happy_calls("Second title", "Second summary."),
    )
    async with summarize_stack(model_provider=provider, **stack_args) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("rerun"))
        first = await stack.start(seeded)
        await asyncio.wait_for(first.result(), 60)
        second = await stack.start(seeded)  # same workflow id, closed -> a new run
        result = await asyncio.wait_for(second.result(), 60)
        row = await stack.document_row(seeded)

    assert second.id == first.id and result.title == "Second title"
    assert (row.summarization, row.summarization_status) == (
        "Second summary.",
        "succeeded",
    )
    assert first.result_run_id != second.result_run_id


# ---------------------------------------------------------------- AC-9.3
async def test_max_turns_fails_the_workflow_without_a_task_loop(stack_args):
    provider = scripted(*[("read_document", {"offset": 0})] * 14)
    async with summarize_stack(model_provider=provider, **stack_args) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("turns"), summary="Old summary.")
        handle = await stack.start(seeded)
        cause = await _failure(handle)
        row = await stack.document_row(seeded)
        events = await history_events(handle)
        await record_fixture(handle, "max_turns")

    assert isinstance(cause, ApplicationError)
    assert (cause.type, cause.message, cause.non_retryable) == (
        "SummarizationFailed",
        "agent_failed",
        True,
    )
    assert cause.cause is None
    assert (row.summarization_status, row.summarization_error) == (
        "failed",
        "agent_failed",
    )
    assert row.summarization == "Old summary."
    assert sum(name == "invoke_model_activity" for name, _ in scheduled_activities(events)) == 12
    assert count_events(events, "workflow_task_failed_event_attributes") == 0


# ---------------------------------------------------------------- AC-9.7
@activity.defn(name="read_document_chunk")
async def failing_read_document_chunk(data: ReadChunkInput) -> DocumentChunk:
    raise ApplicationError(f"permanent read failure {MARKER}", type="Stubbed", non_retryable=True)


async def test_a_permanent_tool_failure_fails_cleanly_after_one_model_call(stack_args):
    provider = scripted(("read_document", {"offset": 0}), "should never be reached")
    overrides = {"read_document_chunk": failing_read_document_chunk}
    async with summarize_stack(model_provider=provider, overrides=overrides, **stack_args) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("fail"), summary="Kept summary.")
        handle = await stack.start(seeded)
        cause = await _failure(handle)
        row = await stack.document_row(seeded)
        events = await history_events(handle)
        await record_fixture(handle, "tool_failed")

    assert (cause.type, cause.message, cause.non_retryable) == (
        "SummarizationFailed",
        "tool_failed",
        True,
    )
    assert cause.cause is None and MARKER not in str(cause)
    assert (row.summarization_status, row.summarization_error) == (
        "failed",
        "tool_failed",
    )
    assert row.summarization == "Kept summary."
    # Exactly one model call, and no model input ever saw the error text.
    model_inputs = [data for name, data in scheduled_activities(events) if name == "invoke_model_activity"]
    assert len(model_inputs) == 1 and len(provider.model.inputs) == 1
    assert all(MARKER not in data and "permanent read failure" not in data for data in model_inputs)
    assert all(MARKER not in data for data in provider.model.inputs)
    assert count_events(events, "workflow_task_failed_event_attributes") == 0


# ---------------------------------------------------------------- AC-9.8
async def test_an_unexpected_exception_fails_the_workflow_with_internal_error(
    stack_args,
):
    stack_args = {**stack_args}
    async with summarize_stack(
        model_provider=happy_script(),
        workflows=(SummarizeDocumentWorkflow, FaultySummarizeWorkflow),
        **stack_args,
    ) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("faulty"), summary="Kept.")
        handle = await stack.start(seeded, workflow=FaultySummarizeWorkflow.run, name="faulty")
        cause = await _failure(handle)
        row = await stack.document_row(seeded)
        events = await history_events(handle)
        description = await handle.describe()

    assert (cause.type, cause.message, cause.non_retryable) == (
        "SummarizationFailed",
        "internal_error",
        True,
    )
    assert description.status == WorkflowExecutionStatus.FAILED
    assert count_events(events, "workflow_task_failed_event_attributes") <= 1
    assert (row.summarization_status, row.summarization_error, row.summarization) == (
        "failed",
        "internal_error",
        "Kept.",
    )


SLOW_STARTED: dict[str, asyncio.Event] = {}


@activity.defn(name="read_document_chunk")
async def slow_read_document_chunk(data: ReadChunkInput) -> DocumentChunk:
    SLOW_STARTED["event"].set()
    await asyncio.sleep(SLOW_ACTIVITY_SECONDS)
    return DocumentChunk(text="", offset=0, next_offset=None, total_chars=0)


async def test_cancelling_the_workflow_is_not_swallowed(stack_args):
    SLOW_STARTED["event"] = asyncio.Event()
    provider = scripted(("read_document", {"offset": 0}), "never")
    overrides = {"read_document_chunk": slow_read_document_chunk}
    async with summarize_stack(model_provider=provider, overrides=overrides, **stack_args) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("cancel"))
        handle = await stack.start(seeded)
        await asyncio.wait_for(SLOW_STARTED["event"].wait(), 30)
        await handle.cancel()
        with pytest.raises(WorkflowFailureError) as caught:
            await asyncio.wait_for(handle.result(), 30)
        description = await handle.describe()
        row = await stack.document_row(seeded)
        events = await history_events(handle)

    assert isinstance(caught.value.cause, CancelledError)
    assert description.status == WorkflowExecutionStatus.CANCELED
    assert row.summarization_status == "running"  # covered by AC-8.6
    assert "mark_summarization_failed" not in [n for n, _ in scheduled_activities(events)]


# ---------------------------------------------------------------- AC-8.6
async def test_a_terminated_run_leaves_running_and_a_new_start_runs_fresh(stack_args):
    SLOW_STARTED["event"] = asyncio.Event()
    overrides = {"read_document_chunk": slow_read_document_chunk}
    async with summarize_stack(
        model_provider=scripted(("read_document", {"offset": 0}), "never"),
        overrides=overrides,
        **stack_args,
    ) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("stuck"))
        stuck = await stack.start(seeded)
        await asyncio.wait_for(SLOW_STARTED["event"].wait(), 30)
        await stuck.terminate("pytest: simulate a stuck run")
        row = await stack.document_row(seeded)
    assert row.summarization_status == "running"

    async with summarize_stack(model_provider=happy_script(), **stack_args) as stack:
        fresh = stack.track(
            await stack.client.start_workflow(
                SummarizeDocumentWorkflow.run,
                SummarizeDocumentInput(
                    project_id=seeded.project_id,
                    document_id=seeded.document_id,
                    requested_by=seeded.user_id,
                    model="test/model",
                    trace_sensitive_data=True,
                ),
                id=stuck.id,
                task_queue=stack.task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                execution_timeout=timedelta(minutes=2),
            )
        )
        result = await asyncio.wait_for(fresh.result(), 60)
        row = await stack.document_row(seeded)

    assert fresh.result_run_id != stuck.result_run_id
    assert result.title and row.summarization_status == "succeeded"


# ------------------------------------------------------ follow-up F1
async def test_a_huge_read_offset_returns_the_empty_chunk_and_the_run_succeeds(
    stack_args,
):
    """Offsets past int32 used to fail the SQL (3 retries, then tool_failed)."""

    calls = [
        ("read_document", {"offset": 2**31}),
        ("read_document", {"offset": 2**40}),
        ("read_document", {"offset": 2**63}),
        ("submit_summary", {"title": "Huge offsets", "summary": "Fine."}),
    ]
    async with summarize_stack(model_provider=scripted(*calls), **stack_args) as stack:
        seeded = await stack.seed(markdown=synthetic_markdown("huge-offset"))
        handle = await stack.start(seeded)
        result = await asyncio.wait_for(handle.result(), 60)
        row = await stack.document_row(seeded)
        events = await history_events(handle)

    assert result.title == "Huge offsets"
    assert row.summarization_status == "succeeded"
    reads = [n for n, _ in scheduled_activities(events) if n == "read_document_chunk"]
    assert len(reads) == 3  # one attempt each: no retries
    assert count_events(events, "activity_task_failed_event_attributes") == 0
