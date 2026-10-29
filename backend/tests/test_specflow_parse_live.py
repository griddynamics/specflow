"""Tier 2 (``SPECFLOW_TEST_TEMPORAL/POSTGRES/GCS=1``): the parse workflow end to end.

Real local Temporal (``specflow-local``, unique queue and id prefix), a scratch
database (never the dev one) and GCS objects only under
``dev/local/pytest/<run-id>/``. Covers AC-8.2, AC-8.4 (history scan), AC-8.6 and
records the replay fixtures when ``SPECFLOW_PARSE_RECORD=1`` (scrubbed, see
``specflow_parse_support.scrub_history``). Nothing is downloaded and no
test server is started (AC-16.0).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError

from db import repositories
from specflow_live import requires_live
from specflow_parse_live_support import (
    Stack,
    activity_types,
    record,
)
from specflow_parse_support import PDF_FIXTURE
from temporal.documents.models import ParseDocumentInput, ParseDocumentResult

pytest_plugins = ["specflow_db_live_support", "specflow_parse_live_support"]
pytestmark = [requires_live("TEMPORAL"), requires_live("POSTGRES"), requires_live("GCS")]
CORRUPT_PDF = b"%PDF-1.4\nthis is not a real pdf\n"


async def test_a_pdf_is_parsed_end_to_end(stack: Stack):
    """AC-8.2 (success): ``parsed`` with markdown set; AC-8.4 on the recorded history."""

    data = await stack.seed(PDF_FIXTURE.read_bytes())
    worker, built = stack.worker()
    async with worker:
        handle = await stack.start(data)
        result = await asyncio.wait_for(handle.result(), 120)
    await built.close()

    assert isinstance(result, ParseDocumentResult)
    assert result.status == "parsed" and result.markdown_chars > 0
    row = await stack.row(data)
    assert row.status == "parsed" and row.error is None
    assert row.parser == "pymupdf4llm" and len(row.markdown) == result.markdown_chars
    raw = await record(handle, "parse_success.json", stack=stack, data=data)
    assert activity_types(raw) == ["parse_document"]


async def test_a_corrupt_pdf_marks_the_document_failed_without_retry(stack: Stack):
    """AC-8.2 (failure): ``pdf_parse_failed``; fixed code in history (AC-8.4)."""

    data = await stack.seed(CORRUPT_PDF)
    worker, built = stack.worker()
    async with worker:
        handle = await stack.start(data)
        with pytest.raises(WorkflowFailureError) as caught:
            await asyncio.wait_for(handle.result(), 120)
    await built.close()

    cause = caught.value.cause
    assert isinstance(cause, ApplicationError)
    assert (cause.message, cause.type, cause.non_retryable) == (
        "pdf_parse_failed",
        "ParseDocumentFailed",
        True,
    )
    row = await stack.row(data)
    assert (row.status, row.error) == ("failed", "pdf_parse_failed")
    raw = await record(handle, "parse_failed.json", stack=stack, data=data)
    assert activity_types(raw) == ["parse_document", "mark_document_failed"]  # no retry


async def test_a_missing_original_and_a_missing_row_use_their_fixed_codes(stack: Stack):
    no_object = await stack.seed(None)
    no_row = ParseDocumentInput(project_id=no_object.project_id, document_id=uuid.uuid4())
    worker, built = stack.worker()
    async with worker:
        first = await stack.start(no_object)
        second = await stack.start(no_row)
        for handle, code in ((first, "original_not_found"), (second, "document_not_found")):
            with pytest.raises(WorkflowFailureError) as caught:
                await asyncio.wait_for(handle.result(), 120)
            assert caught.value.cause.message == code
    await built.close()

    assert (await stack.row(no_object)).error == "original_not_found"
    assert await stack.row(no_row) is None
    await record(second, "parse_not_found.json", stack=stack, data=no_object)


async def test_a_fresh_run_can_start_after_a_failure(stack: Stack):
    """AC-8.6: the closed id is reused (ALLOW_DUPLICATE); the second run parses."""

    data = await stack.seed(None)
    worker, built = stack.worker()
    async with worker:
        failed = await stack.start(data)
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(failed.result(), 120)
        assert (await stack.row(data)).status == "failed"

        await stack.upload(data.project_id, data.document_id, PDF_FIXTURE.read_bytes())
        async with stack.sessions() as session, session.begin():
            assert await repositories.mark_processing(session, data.project_id, data.document_id)
        second = await stack.start(data)
        result = await asyncio.wait_for(second.result(), 120)
    await built.close()

    assert second.id == failed.id
    assert result.status == "parsed"
    assert (await stack.row(data)).status == "parsed"


async def test_a_fresh_run_can_start_after_a_timeout(stack: Stack):
    """AC-8.6: with no worker the run times out (start option only); a new run then parses."""

    data = await stack.seed(PDF_FIXTURE.read_bytes())
    timed_out = await stack.start(data, execution_timeout=timedelta(seconds=3))
    with pytest.raises(WorkflowFailureError):
        await asyncio.wait_for(timed_out.result(), 60)
    assert (await timed_out.describe()).status.name == "TIMED_OUT"
    assert (await stack.row(data)).status == "processing"  # the stuck state

    worker, built = stack.worker()
    async with worker:
        fresh = await stack.start(data)
        result = await asyncio.wait_for(fresh.result(), 120)
    await built.close()

    assert result.status == "parsed"
    assert (await stack.row(data)).status == "parsed"
