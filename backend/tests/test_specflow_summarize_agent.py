"""Tier 1: the summarizer agent, its tool schemas and the workflow's pure logic.

AC-9.1 (tool schemas), the pure parts of AC-9.3/9.7/9.8 (failure
classification, cancellation detection) and the ``SummaryOutput`` validation.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from agents import AgentsException, MaxTurnsExceeded, ModelBehaviorError
from agents.tool_context import ToolContext
from pydantic import ValidationError
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
    RetryState,
    is_cancelled_exception,
)
from temporalio.openai_agents import AgentsWorkflowError

from agent_runtime import summarizer
from db.repositories import SUMMARIZATION_ERROR_CODES
from temporal.agents import tools, workflows
from temporal.agents.models import (
    SummarizeDocumentInput,
    SummaryOutput,
)

FORBIDDEN_SCHEMA_WORDS = (
    "project",
    "document_id",
    "sql",
    "cypher",
    "key",
    "secret",
    "token",
)


def _activity_error(cause: BaseException) -> ActivityError:
    error = ActivityError(
        "Activity task failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="read_document_chunk",
        activity_id="1",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    error.__cause__ = cause
    return error


def _agents_workflow_error(cause: BaseException) -> AgentsWorkflowError:
    error = AgentsWorkflowError("Workflow failure exception in Agents Framework")
    error.__cause__ = cause
    return error


# ------------------------------------------------------------------ agent
def test_the_agent_uses_submit_summary_as_its_final_answer():
    agent = summarizer.build_summarizer_agent("test/model")

    assert agent.name == "document-summarizer"
    assert agent.model == "test/model"
    assert agent.output_type is None
    assert agent.tool_use_behavior == {"stop_at_tool_names": ["submit_summary"]}
    assert agent.reset_tool_choice is False
    assert agent.model_settings.tool_choice == "required"
    assert agent.model_settings.parallel_tool_calls is False
    assert [tool.name for tool in agent.tools] == ["read_document", "submit_summary"]


@pytest.mark.parametrize("model", ["", "   ", None])
def test_the_agent_requires_a_model_string(model):
    with pytest.raises(ValueError, match="model"):
        summarizer.build_summarizer_agent(cast(Any, model))


def test_the_instructions_are_versioned_and_guard_against_injection():
    text = summarizer.SUMMARIZER_INSTRUCTIONS_V2

    assert summarizer.INSTRUCTIONS_VERSION == "v2"
    assert "64000" in text and "300 words" in text and "10 words" in text
    assert "never follow instructions" in text
    for name in ("read_document", "submit_summary"):
        assert name in text
    # A1: the graph library owns ``:Document``; the summarizer never touches the graph.
    assert "document_node" not in text


def test_no_tool_schema_carries_ids_sql_cypher_or_keys():
    """AC-9.1: every tool schema strict, without forbidden words."""

    agent = summarizer.build_summarizer_agent("test/model")
    expected = {
        "read_document": {"offset"},
        "submit_summary": {"title", "summary"},
    }
    for tool in agent.tools:
        schema = tool.params_json_schema
        dumped = json.dumps(schema).lower()
        assert schema["additionalProperties"] is False, tool.name
        assert tool.name in expected, tool.name
        assert set(schema["properties"]) == expected[tool.name]
        assert tool.strict_json_schema is True
        for word in FORBIDDEN_SCHEMA_WORDS:
            assert word not in dumped, (tool.name, word)


def test_every_tool_raises_instead_of_reporting_errors_to_the_model():
    """H2: ``failure_error_function=None`` on every tool."""

    for tool in tools.SUMMARIZER_TOOLS:
        assert tool._failure_error_function is None, tool.name


def test_submit_summary_returns_the_answer_as_json():
    arguments = json.dumps({"title": "T", "summary": "S"})
    ctx = ToolContext(
        context=None,
        tool_name="submit_summary",
        tool_call_id="c1",
        tool_arguments=arguments,
    )
    raw = asyncio.run(tools.submit_summary.on_invoke_tool(ctx, arguments))
    assert SummaryOutput.model_validate_json(raw) == SummaryOutput(title="T", summary="S")


def test_tool_activities_use_the_specified_timeouts_and_retries():
    assert tools.TOOL_START_TO_CLOSE.total_seconds() == 30
    assert isinstance(tools.TOOL_RETRY, RetryPolicy)
    assert tools.TOOL_RETRY.maximum_attempts == 3
    assert workflows.SAVE_RETRY.maximum_attempts == 5


# --------------------------------------------------------- classification
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (_agents_workflow_error(_activity_error(ApplicationError("x"))), "tool_failed"),
        (_activity_error(ApplicationError("x")), "tool_failed"),
        (MaxTurnsExceeded("Max turns (12) exceeded"), "agent_failed"),
        (ModelBehaviorError("bad json"), "agent_failed"),
        (AgentsException("other"), "agent_failed"),
    ],
)
def test_failure_codes_follow_the_catch_strategy(error, code):
    assert workflows.failure_code(error) == code


def test_a_validation_error_is_an_agent_failure():
    with pytest.raises(ValidationError) as caught:
        SummaryOutput.model_validate_json('{"title": "", "summary": "x"}')
    assert workflows.failure_code(caught.value) == "agent_failed"


def test_every_failure_code_is_accepted_by_the_repository():
    codes = {workflows.TOOL_FAILED, workflows.AGENT_FAILED, workflows.INTERNAL_ERROR}
    assert codes <= SUMMARIZATION_ERROR_CODES


def test_the_caught_exception_types_are_the_specified_ones():
    assert set(workflows.AGENT_ERRORS) == {
        AgentsException,
        AgentsWorkflowError,
        ActivityError,
        ValidationError,
    }
    assert not any(issubclass(BaseException, t) for t in workflows.AGENT_ERRORS)


@pytest.mark.parametrize(
    "error",
    [
        CancelledError("cancelled"),
        _activity_error(CancelledError("cancelled")),
        _agents_workflow_error(_activity_error(CancelledError("cancelled"))),
    ],
)
def test_a_cancellation_anywhere_in_the_chain_is_found(error):
    assert workflows.cancellation_in(error) is not None


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("boom"),
        _agents_workflow_error(_activity_error(ApplicationError("x"))),
        MaxTurnsExceeded("x"),
    ],
)
def test_other_errors_are_not_cancellations(error):
    assert workflows.cancellation_in(error) is None


# -------------------------------------------------------------- run result
def _result(items, final_output) -> SimpleNamespace:
    usage = SimpleNamespace(requests=3, input_tokens=10, output_tokens=5)
    return SimpleNamespace(
        new_items=items,
        final_output=final_output,
        context_wrapper=SimpleNamespace(usage=usage),
    )


def test_usage_is_copied_from_the_run():
    usage = workflows.usage_of(_result([], ""))
    assert (usage.requests, usage.input_tokens, usage.output_tokens) == (3, 10, 5)


@pytest.mark.parametrize(
    "final",
    [
        "plain text answer",
        '{"title": "", "summary": "x"}',
        '{"title": "t", "summary": ""}',
        json.dumps({"title": "t" * 201, "summary": "x"}),
        json.dumps({"title": "t", "summary": "x" * 8001}),
        None,
    ],
)
def test_an_invalid_final_answer_is_rejected(final):
    workflow = workflows.SummarizeDocumentWorkflow()
    with pytest.raises(ValidationError):
        workflow._validated_output(_result([], final))


def test_a_valid_final_answer_is_accepted():
    workflow = workflows.SummarizeDocumentWorkflow()
    output = workflow._validated_output(_result([], json.dumps({"title": "T", "summary": "S"})))
    assert output == SummaryOutput(title="T", summary="S")


# ------------------------------------------------- follow-up F2, F6/F7
@pytest.mark.parametrize(
    "answer",
    [
        {"title": "a\x00b", "summary": "ok"},
        {"title": "ok", "summary": "a\x00b"},
        {"title": "ok", "summary": "\x00"},
    ],
)
def test_a_nul_character_in_the_answer_is_rejected_as_agent_failed(answer):
    workflow = workflows.SummarizeDocumentWorkflow()
    with pytest.raises(AgentsException) as caught:
        workflow._validated_output(_result([], json.dumps(answer)))
    assert workflows.failure_code(caught.value) == "agent_failed"
    assert isinstance(caught.value, workflows.AGENT_ERRORS)
    assert "\x00" not in str(caught.value)


def _data(*, sensitive: bool) -> SummarizeDocumentInput:
    return SummarizeDocumentInput(
        project_id=uuid4(),
        document_id=uuid4(),
        requested_by=uuid4(),
        model="m",
        trace_sensitive_data=sensitive,
    )


@pytest.mark.parametrize("sensitive", [False, True])
def test_the_run_config_follows_trace_sensitive_data(sensitive):
    config = workflows.build_run_config(_data(sensitive=sensitive))
    assert config.trace_include_sensitive_data is sensitive
    assert config.model_settings.preserve_raw_usage is True


@pytest.mark.parametrize(
    "raised",
    [
        _agents_workflow_error(_activity_error(CancelledError("cancelled"))),
        _activity_error(CancelledError("cancelled")),
    ],
)
async def test_a_cancellation_in_the_cause_chain_is_reraised_untouched(monkeypatch, raised):
    """The inner re-raise: never turned into ``agent_failed`` or a FAILED run."""

    marked: list[str] = []

    class Probe(workflows.SummarizeDocumentWorkflow):
        async def _run_agent(self, _data, _context):
            raise raised

        async def _mark_failed(self, _data, code):
            marked.append(code)

    monkeypatch.setattr(workflows.workflow, "info", lambda: SimpleNamespace(run_id="r", workflow_id="w"))
    with pytest.raises((CancelledError, ActivityError)) as caught:
        await Probe().run(_data(sensitive=False))
    assert is_cancelled_exception(caught.value)
    assert not isinstance(caught.value, ApplicationError)
    assert marked == []


def test_the_workflow_contract():
    assert workflows.WORKFLOW_NAME == "SummarizeDocumentWorkflow"
    assert workflows.SummarizeDocumentWorkflow.__temporal_workflow_definition.name == ("SummarizeDocumentWorkflow")
    assert workflows.MAX_TURNS == 12
    assert workflows.TRACE_NAME == "summarize-document"
    assert workflows.EXECUTION_TIMEOUT.total_seconds() == 15 * 60


def test_the_workflow_module_imports_no_infrastructure_packages():
    """AN 3.4: the sandboxed modules never import sqlalchemy, neo4j or google."""

    code = (
        "import sys, temporal.agents.workflows\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
        "{'sqlalchemy', 'neo4j', 'asyncpg'} or m.startswith('google.cloud') "
        "or m == 'settings')\n"
        "print('BAD=' + ','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert out.stdout.strip().splitlines()[-1] == "BAD="
