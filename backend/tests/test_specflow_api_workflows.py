"""Tier 1: parse and summarize start rules (AC-7.8 to 7.10, status ownership)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from specflow_api_support import PDF, bearer

PDF_HEADERS = {"Content-Type": "application/pdf"}


async def uploaded(harness) -> tuple[str, str]:
    pid = (await harness.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer())).json()["id"]
    response = await harness.client.post(
        f"/api/v1/projects/{pid}/documents?filename=a.pdf",
        content=PDF,
        headers={**bearer(), **PDF_HEADERS},
    )
    did = response.json()["document"]["id"]
    # K5: the upload itself starts the parse. These tests exercise the explicit
    # parse/summarize endpoints, so they begin from the pre-parse state.
    harness.temporal.calls.clear()
    harness.temporal.attempts = 0
    harness.repo.documents[uuid.UUID(did)].status = "uploaded"
    return pid, did


def row(harness, did):
    return harness.repo.documents[uuid.UUID(did)]


async def post(harness, pid, did, action, **kwargs):
    return await harness.client.post(f"/api/v1/projects/{pid}/documents/{did}/{action}", headers=bearer(), **kwargs)


async def test_parse_marks_processing_then_starts_the_named_workflow(harness):
    pid, did = await uploaded(harness)
    response = await post(harness, pid, did, "parse", json={})
    assert response.status_code == 202
    assert response.json() == {
        "workflow_id": f"doc-parse-{did}",
        "status": "processing",
    }
    assert row(harness, did).status == "processing"
    [call] = harness.temporal.calls
    assert call["workflow"] == "ParseDocumentWorkflow"
    assert call["id"] == f"doc-parse-{did}" and call["task_queue"] == "specflow-parse"
    assert call["id_conflict_policy"] == WorkflowIDConflictPolicy.USE_EXISTING
    assert call["id_reuse_policy"] == WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert call["execution_timeout"] == timedelta(minutes=60)
    assert (str(call["arg"].project_id), str(call["arg"].document_id)) == (pid, did)


async def test_a_repeat_parse_click_returns_the_same_workflow_id(harness):
    pid, did = await uploaded(harness)
    first = (await post(harness, pid, did, "parse")).json()
    second = await post(harness, pid, did, "parse")
    assert second.status_code == 202 and second.json() == first
    assert [c["id"] for c in harness.temporal.calls] == [first["workflow_id"]]


async def test_parse_of_a_failed_document_restarts_it(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status, row(harness, did).error = "failed", "pdf_parse_failed"
    assert (await post(harness, pid, did, "parse")).status_code == 202
    assert row(harness, did).status == "processing" and row(harness, did).error is None


async def test_parse_of_a_parsed_document_is_409(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    response = await post(harness, pid, did, "parse")
    assert response.status_code == 409 and response.json()["error"]["code"] == "already_parsed"
    assert not harness.temporal.calls


async def test_a_failed_parse_start_is_503_and_marks_the_row_failed(harness):
    pid, did = await uploaded(harness)
    harness.temporal.fail = RuntimeError("connect to 10.0.0.1:7233 password=hunter2")
    response = await post(harness, pid, did, "parse")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workflow_start_failed"
    assert "hunter2" not in response.text and "10.0.0.1" not in response.text
    assert (row(harness, did).status, row(harness, did).error) == (
        "failed",
        "workflow_start_failed",
    )


async def test_summarize_requires_a_parsed_document(harness):
    pid, did = await uploaded(harness)
    response = await post(harness, pid, did, "summarize", json={})
    assert response.status_code == 409 and response.json()["error"]["code"] == "document_not_parsed"
    assert not harness.temporal.calls


async def test_summarize_resolves_model_and_trace_flag_into_the_input(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    response = await post(harness, pid, did, "summarize", json={})
    assert response.status_code == 202
    assert response.json() == {
        "workflow_id": f"doc-summary-{did}",
        "summarization_status": "running",
    }
    assert row(harness, did).summarization_status == "running"
    [call] = harness.temporal.calls
    assert call["workflow"] == "SummarizeDocumentWorkflow" and call["task_queue"] == "specflow-agents"
    assert call["execution_timeout"] == timedelta(minutes=15)
    arg = call["arg"]
    assert arg.model == "test/model" and arg.trace_sensitive_data is False  # AGENT_TRACE_SENSITIVE_DATA default
    assert str(arg.requested_by) == str(next(iter(harness.repo.users.values())).id)
    assert str(arg.document_id) == did


async def test_a_request_model_equal_to_agent_model_is_accepted(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    response = await post(harness, pid, did, "summarize", json={"model": "test/model"})
    assert response.status_code == 202
    assert harness.temporal.calls[0]["arg"].model == "test/model"


async def test_the_trace_flag_comes_from_settings(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    settings = harness.runtime.settings
    harness.runtime.settings = replace(
        settings,
        agent_run=settings.agent_run.model_copy(update={"trace_sensitive_data": False}),
    )
    await post(harness, pid, did, "summarize")  # no body at all
    assert harness.temporal.calls[0]["arg"].trace_sensitive_data is False


async def test_a_double_summarize_click_starts_exactly_one_workflow(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    one = await post(harness, pid, did, "summarize", json={})
    two = await post(harness, pid, did, "summarize", json={})
    assert one.status_code == two.status_code == 202 and one.json() == two.json()
    assert harness.temporal.attempts == 2  # the server was asked twice ...
    [call] = harness.temporal.calls  # ... and ran one execution (USE_EXISTING)
    assert call["id"] == f"doc-summary-{did}"


async def test_a_failed_summarize_start_is_503_and_keeps_the_document_parsed(harness):
    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    harness.temporal.fail = RuntimeError("boom")
    response = await post(harness, pid, did, "summarize", json={})
    assert response.status_code == 503 and response.json()["error"]["code"] == "workflow_start_failed"
    assert (
        row(harness, did).summarization_status,
        row(harness, did).summarization_error,
    ) == (
        "failed",
        "workflow_start_failed",
    )
    assert row(harness, did).status == "parsed"


async def test_a_repeat_click_whose_start_fails_does_not_clobber_a_running_row(harness):
    pid, did = await uploaded(harness)
    await post(harness, pid, did, "parse")  # row: processing
    harness.temporal.fail = RuntimeError("boom")
    assert (await post(harness, pid, did, "parse")).status_code == 503
    assert row(harness, did).status == "processing"  # not ours to fail


@pytest.mark.parametrize("action", ["parse", "summarize"])
async def test_unknown_document_is_404(harness, action):
    pid = (await harness.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer())).json()["id"]
    response = await post(harness, pid, "00000000-0000-0000-0000-000000000009", action)
    assert response.status_code == 404 and response.json()["error"]["code"] == "document_not_found"


async def test_the_detail_caps_the_markdown_and_has_no_graph_node(harness):
    pid, did = await uploaded(harness)
    doc = row(harness, did)
    doc.status, doc.markdown = "parsed", "m" * 1_000_005
    detail = (await harness.client.get(f"/api/v1/projects/{pid}/documents/{did}", headers=bearer())).json()
    assert len(detail["markdown"]) == 1_000_000 and detail["markdown_truncated"] is True
    assert "graph_node" not in detail


async def test_the_detail_never_reads_neo4j(harness):
    """A1: the host no longer reads ``:Document`` nodes; the graph library owns them."""

    pid, did = await uploaded(harness)
    row(harness, did).status = "parsed"
    response = await harness.client.get(f"/api/v1/projects/{pid}/documents/{did}", headers=bearer())
    assert response.status_code == 200
    assert harness.driver.queries == 0
