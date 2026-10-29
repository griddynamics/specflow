"""Tier 1: the agents-queue activities in ``ActivityEnvironment`` with fakes.

AC-8.3 (chunking), AC-8.4 (fixed, sanitized error messages), AC-9.6
(``agent.tool.called`` without content), the registration contract.
"""

from __future__ import annotations

import inspect
import logging
import uuid

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from db import repositories
from db.repositories import mark_summarization_failed as real_mark
from logger.logger import get_config
from settings import load_settings
from specflow_summarize_support import (
    FakeAgentDb,
    FakeAppSessions,
    FakeRow,
    FakeSession,
)
from temporal.agents import activities as activities_module
from temporal.agents import registration
from temporal.agents.activities import SummarizeActivities, chunk_of
from temporal.agents.models import (
    CHUNK_LIMIT,
    MarkFailedInput,
    ReadChunkInput,
    SaveSummarizationInput,
)
from temporal.agents.workflows import SummarizeDocumentWorkflow
from temporal.registry import Registration, load_registration

PROJECT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
PROJECT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
DOC_A = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000d1")
DOC_B = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000d2")
LEAKED_TEXT = "SECRET-DB-ERROR-detail-1f2e"
MARKDOWN = "".join(f"[line {i:05d}] synthetic widget text.\n" for i in range(1200))  # ~40 K


@pytest.fixture(autouse=True)
def central_pipeline() -> None:
    """Start the central logger: its filter puts ``run_id`` on the ``caplog`` records.

    Without this, the ids are there only if an earlier test left it running.
    """

    get_config()


@pytest.fixture
def agent_db() -> FakeAgentDb:
    return FakeAgentDb(
        rows={
            DOC_A: FakeRow(PROJECT_A, MARKDOWN),
            DOC_B: FakeRow(PROJECT_B, "markdown of B"),
        }
    )


@pytest.fixture
def acts(agent_db) -> SummarizeActivities:
    return SummarizeActivities(FakeAppSessions(), agent_db)


@pytest.fixture
def env() -> ActivityEnvironment:
    return ActivityEnvironment()


def _read(offset: int, *, project=PROJECT_A, document=DOC_A, limit=CHUNK_LIMIT) -> ReadChunkInput:
    return ReadChunkInput(project_id=project, document_id=document, offset=offset, limit=limit)


# ------------------------------------------------------------- AC-8.3
async def test_reading_follows_next_offset_to_the_end(acts, env):
    offset, parts = 0, []
    while offset is not None:
        chunk = await env.run(acts.read_document_chunk, _read(offset))
        assert len(chunk.text) <= CHUNK_LIMIT
        assert chunk.total_chars == len(MARKDOWN)
        parts.append(chunk.text)
        offset = chunk.next_offset
    assert "".join(parts) == MARKDOWN
    assert len(parts) == 3


@pytest.mark.parametrize("offset", [len(MARKDOWN), len(MARKDOWN) + 1, 10**9])
async def test_an_offset_beyond_the_end_gives_empty_text_and_no_next_offset(acts, env, offset):
    chunk = await env.run(acts.read_document_chunk, _read(offset))
    assert (chunk.text, chunk.next_offset) == ("", None)


@pytest.mark.parametrize("offset", [2**31 - 2, 2**31 - 1, 2**31, 2**40, 2**63])
async def test_a_huge_offset_is_clamped_to_an_int32_sql_start(acts, env, agent_db, monkeypatch, offset):
    """F1: the bound substr start must fit int32; the answer is the empty chunk."""

    starts: list[int] = []
    original = FakeSession.execute

    async def spy(self, statement, params):
        if "start" in params:
            starts.append(params["start"])
        return await original(self, statement, params)

    monkeypatch.setattr(FakeSession, "execute", spy)
    chunk = await env.run(acts.read_document_chunk, _read(offset))
    assert (chunk.text, chunk.next_offset, chunk.offset) == ("", None, offset)
    assert starts and all(1 <= s <= 2**31 - 2 for s in starts)


async def test_the_limit_is_capped_and_honoured(acts, env):
    chunk = await env.run(acts.read_document_chunk, _read(5, limit=10))
    assert chunk.text == MARKDOWN[5:15] and chunk.next_offset == 15


def test_the_chunk_limit_is_sixteen_thousand_characters():
    """F6: literal values, not the constant itself."""

    assert CHUNK_LIMIT == 16000
    chunk = chunk_of("y" * 40_000, 40_000, 0)
    assert len(chunk.text) == 16000 and chunk.next_offset == 16000
    assert len(chunk_of("y" * 24_000, 40_000, 16_000).text) == 16000


def test_chunk_of_never_exceeds_the_limit():
    chunk = chunk_of("x" * (CHUNK_LIMIT + 50), CHUNK_LIMIT + 50, 0)
    assert len(chunk.text) == CHUNK_LIMIT and chunk.next_offset == CHUNK_LIMIT


async def test_reads_run_in_the_agent_scope_of_the_input_project(acts, env, agent_db):
    await env.run(acts.read_document_chunk, _read(0))
    assert "set_config('app.project_id'" in agent_db.statements[0]
    assert "substr(markdown" in agent_db.statements[1]


# --------------------------------------------- cross-project and errors
async def test_another_projects_document_is_not_found(acts, env):
    with pytest.raises(ApplicationError) as caught:
        await env.run(acts.read_document_chunk, _read(0, document=DOC_B))
    assert caught.value.message == "document_not_found"
    assert caught.value.non_retryable is True
    assert caught.value.__cause__ is None


async def test_a_document_without_markdown_fails_with_a_fixed_code(acts, env, agent_db):
    agent_db.rows[DOC_A] = FakeRow(PROJECT_A, None)
    with pytest.raises(ApplicationError, match=r": markdown_missing$"):
        await env.run(acts.read_document_chunk, _read(0))


async def test_a_database_error_becomes_a_sanitized_retryable_code(acts, env, agent_db, caplog):
    agent_db.fail = RuntimeError(LEAKED_TEXT)
    caplog.set_level(logging.INFO)
    with pytest.raises(ApplicationError) as caught:
        await env.run(acts.read_document_chunk, _read(0))
    error = caught.value
    assert error.message == "tool_unavailable" and error.type == "ToolActivityError"
    assert error.non_retryable is False
    assert error.__cause__ is None and error.__suppress_context__ is True
    assert LEAKED_TEXT not in caplog.text


# --------------------------------------------------------------- AC-9.6
async def test_every_tool_call_is_logged_without_content(acts, env, caplog):
    caplog.set_level(logging.INFO)
    await env.run(acts.read_document_chunk, _read(0))
    await env.run(acts.read_document_chunk, _read(16000))

    calls = [r for r in caplog.records if getattr(r, "event", None) == "agent.tool.called"]
    assert [r.tool for r in calls] == ["read_document", "read_document"]
    for record in calls:
        assert record.project_id == str(PROJECT_A)
        assert isinstance(record.duration_ms, int) and record.duration_ms >= 0
        assert record.run_id == "test-run"  # the Temporal run id (ActivityEnvironment)
        assert record.outcome == "ok"
    assert "synthetic widget" not in caplog.text


# ------------------------------------------------------- trusted writes
async def test_save_summarization_uses_the_repository_and_logs_saved(acts, env, monkeypatch, caplog):
    saved = []

    async def fake_save(session, project_id, document_id, summary, model):
        saved.append((project_id, document_id, summary, model))
        return True

    monkeypatch.setattr(repositories, "save_summarization", fake_save)
    caplog.set_level(logging.INFO)
    requested_by = uuid.uuid4()
    await env.run(
        acts.save_summarization,
        SaveSummarizationInput(
            project_id=PROJECT_A,
            document_id=DOC_A,
            summary="The summary.",
            title="T",
            model="m",
            workflow_id="wf-1",
            requested_by=requested_by,
        ),
    )
    assert saved == [(PROJECT_A, DOC_A, "The summary.", "m")]
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "agent.summary.saved"]
    assert record.requested_by == str(requested_by) and record.run_id == "test-run"
    assert record.workflow_id == "wf-1"
    assert "The summary." not in caplog.text


async def test_save_of_a_missing_document_is_not_retried(acts, env, monkeypatch):
    async def fake_save(*args):
        return False

    monkeypatch.setattr(repositories, "save_summarization", fake_save)
    data = SaveSummarizationInput(
        project_id=PROJECT_A,
        document_id=DOC_A,
        summary="S",
        title="T",
        model="m",
        workflow_id="wf",
        requested_by=uuid.uuid4(),
    )
    with pytest.raises(ApplicationError) as caught:
        await env.run(acts.save_summarization, data)
    assert caught.value.message == "document_not_found" and caught.value.non_retryable


async def test_a_save_error_is_sanitized(acts, env, monkeypatch):
    async def fake_save(*args):
        raise RuntimeError(LEAKED_TEXT)

    monkeypatch.setattr(repositories, "save_summarization", fake_save)
    data = SaveSummarizationInput(
        project_id=PROJECT_A,
        document_id=DOC_A,
        summary="S",
        title="T",
        model="m",
        workflow_id="wf",
        requested_by=uuid.uuid4(),
    )
    with pytest.raises(ApplicationError) as caught:
        await env.run(acts.save_summarization, data)
    assert caught.value.message == "save_failed" and caught.value.non_retryable is False


async def test_mark_failed_stores_the_code_and_logs_it(acts, env, monkeypatch, caplog):
    marked = []

    async def fake_mark(session, project_id, document_id, code):
        marked.append(code)
        return True

    monkeypatch.setattr(repositories, "mark_summarization_failed", fake_mark)
    caplog.set_level(logging.INFO)
    await env.run(
        acts.mark_summarization_failed,
        MarkFailedInput(project_id=PROJECT_A, document_id=DOC_A, error_code="tool_failed"),
    )
    assert marked == ["tool_failed"]
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "agent.summary.failed"]
    assert record.error_code == "tool_failed"


async def test_mark_failed_rejects_an_unknown_code_without_retry(acts, env):
    assert real_mark is repositories.mark_summarization_failed
    with pytest.raises(ApplicationError) as caught:
        await env.run(
            acts.mark_summarization_failed,
            MarkFailedInput(project_id=PROJECT_A, document_id=DOC_A, error_code="weird"),
        )
    assert caught.value.message == "invalid_error_code" and caught.value.non_retryable


# ---------------------------------------------------------- registration
def test_all_activities_are_async():
    names = [
        "read_document_chunk",
        "save_summarization",
        "mark_summarization_failed",
    ]
    for name in names:
        assert inspect.iscoroutinefunction(getattr(SummarizeActivities, name)), name


async def test_registration_lists_the_workflow_and_activities_and_closes_everything():
    events: list[str] = []

    class Engine:
        async def dispose(self) -> None:
            events.append("app.dispose")

    async def fake_dispose(factory) -> None:
        events.append("agent.dispose")

    original = registration.dispose_agent_session_factory
    registration.dispose_agent_session_factory = fake_dispose
    try:
        reg = registration.build_registration(Engine(), FakeAgentDb())
        assert isinstance(reg, Registration)
        assert list(reg.workflows) == [SummarizeDocumentWorkflow]
        names = [fn.__temporal_activity_definition.name for fn in reg.activities]
        assert names == [
            "read_document_chunk",
            "save_summarization",
            "mark_summarization_failed",
        ]
        await reg.close()
    finally:
        registration.dispose_agent_session_factory = original
    assert events == ["agent.dispose", "app.dispose"]


async def test_the_worker_registry_loads_the_agents_registration(valid_env):
    settings = load_settings("worker", valid_env)
    reg = load_registration("agents", settings)
    try:
        assert list(reg.workflows) == [SummarizeDocumentWorkflow]
        assert len(reg.activities) == 3
    finally:
        await reg.close()


def test_activity_modules_log_only_through_the_central_logger():
    source = inspect.getsource(activities_module)
    assert "import logging" not in source and "print(" not in source
