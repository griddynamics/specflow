"""Tier 1: the parse activity (stored key, routing, limits, progress, codes, time limits).

The conversion step is the real ``conversion.convert`` (limits first); only the engine entry point
``file_to_markdown`` is replaced where a test needs a recorder, a stub or a failure. Real PDFs are built
with ``pdf_builder_support`` (a 501-page one needs no conversion: pdfium reads the page tree).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from temporalio.client import WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer

from db import source_files
from db.repositories import PARSE_ERROR_CODES
from db.sources import UPLOAD_STALE_AFTER
from file_parsers import formats
from file_parsers.convert import ParsedMarkdown, PdfParseError
from file_parsers.engines.docling import engine_id
from file_parsers.engines.port import UnsupportedFormatError
from file_parsers.limits import MAX_PDF_PAGES, DocumentParseFailedError, DocumentTooLargeError
from pdf_builder_support import build_pdf
from specflow_parse_support import (
    FIXED_WORKFLOW_ID,
    FIXTURE_DIR,
    PDF_FIXTURE,
    FakeRepository,
    FakeSessions,
    install_fake_repository,
)
from storage.keys import document_original
from storage.memory import InMemoryObjectStore
from temporal.documents import conversion, workflows
from temporal.documents.activities import ParseActivities
from temporal.documents.models import ParseDocumentInput

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PRIVATE_NAME = "confidential-contract-name.pdf"
STORED_KEY = "projects/p/documents/d/original.bin"  # not what a key builder would give for the media type


@pytest.fixture
def data() -> ParseDocumentInput:
    return ParseDocumentInput(project_id=uuid4(), document_id=uuid4())


@pytest.fixture
def repository(monkeypatch) -> FakeRepository:
    repository = FakeRepository()
    install_fake_repository(monkeypatch, repository)
    return repository


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parse-runtime")
    yield pool
    pool.shutdown(wait=True)


def store_with(key: str, content: bytes) -> InMemoryObjectStore:
    store = InMemoryObjectStore(app_env="local")
    store.objects[key] = content
    return store


def activities(store, executor, **kwargs) -> ParseActivities:
    return ParseActivities(FakeSessions(), store, executor, **kwargs)


async def run(parse_activities: ParseActivities, data, env: ActivityEnvironment | None = None):
    return await (env or ActivityEnvironment()).run(parse_activities.parse_document, data)


async def failure_of(coro) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        await coro
    return caught.value


class Recorder:
    """Replaces ``conversion.file_to_markdown``: records the calls and returns or raises on demand."""

    def __init__(self, result: ParsedMarkdown | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.result = result or ParsedMarkdown(markdown="# Doc\n\ntext", parser="stub@1")
        self.raises = raises
        self.progress: list[tuple[int, int]] = []

    def __call__(self, path: Path, media_type: str, *, on_progress=None, **_: Any) -> ParsedMarkdown:
        self.calls.append((path.name, media_type))
        for done, total in self.progress:
            on_progress(done, total)
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def recorder(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(conversion, "file_to_markdown", recorder)
    return recorder


# ------------------------------------------------------------ key and routing
async def test_the_activity_downloads_the_stored_original_key_not_a_rebuilt_one(data, repository, executor, recorder):
    repository.original_key = STORED_KEY
    repository.media_type = DOCX

    result = await run(activities(store_with(STORED_KEY, b"PK\x03\x04docx"), executor), data)

    assert result.status == "parsed"
    assert recorder.calls == [("original.docx", DOCX)]


async def test_a_missing_stored_original_is_original_not_found(data, repository, executor, recorder):
    repository.original_key = STORED_KEY

    error = await failure_of(run(activities(InMemoryObjectStore(app_env="local"), executor), data))

    assert (error.type, error.message, error.non_retryable) == ("OriginalNotFound", "original_not_found", True)
    assert recorder.calls == []


@pytest.mark.parametrize("spec", formats.FORMATS, ids=lambda spec: spec.format.value)
async def test_every_media_type_is_routed_to_the_facade_with_its_own_type_and_extension(
    spec, data, repository, executor, recorder
):
    repository.media_type = spec.media_type
    repository.original_key = STORED_KEY

    await run(
        activities(
            store_with(STORED_KEY, b"%PDF-" if spec.media_type == formats.PDF_MEDIA_TYPE else b"x"),
            executor,
            parse=_no_limits(recorder),
        ),
        data,
    )

    assert recorder.calls == [(f"original{spec.extension}", spec.media_type)]


def _no_limits(recorder: Recorder) -> conversion.ParseFunction:
    """The page-tree check needs a real PDF; the routing test feeds placeholder bytes, so skip it."""

    def parse(path: Path, media_type: str, on_progress) -> ParsedMarkdown:
        return recorder(path, media_type, on_progress=on_progress)

    return parse


async def test_an_unknown_media_type_is_a_parse_failure_not_an_internal_error(data, repository, executor):
    repository.media_type = "application/x-unknown"
    repository.original_key = STORED_KEY

    error = await failure_of(run(activities(store_with(STORED_KEY, b"x"), executor), data))

    assert (error.type, error.message, error.non_retryable) == (
        "DocumentParseFailedError",
        "document_parse_failed",
        True,
    )
    assert "x-unknown" not in repr(error)


async def test_an_office_type_is_document_parse_failed_while_the_engine_does_not_support_it(
    data, repository, executor, monkeypatch
):
    """Until the Office path exists the facade raises UnsupportedFormatError; the code stays fixed."""

    monkeypatch.setattr(conversion, "file_to_markdown", Recorder(raises=UnsupportedFormatError(DOCX)))
    repository.media_type = DOCX
    repository.original_key = STORED_KEY

    error = await failure_of(run(activities(store_with(STORED_KEY, b"PK"), executor), data))

    assert (error.type, error.message, error.non_retryable) == (
        "DocumentParseFailedError",
        "document_parse_failed",
        True,
    )
    assert error.__cause__ is None and DOCX not in repr(error)


# --------------------------------------------------------------------- limits
def page_pdf(pages: int) -> bytes:
    return build_pdf([[] for _ in range(pages)])


async def test_a_501_page_pdf_fails_before_any_conversion_with_document_too_large(data, repository, executor, recorder):
    store = store_with(str_key(data), page_pdf(MAX_PDF_PAGES + 1))

    error = await failure_of(run(activities(store, executor), data))

    assert (error.type, error.message, error.non_retryable) == (
        "DocumentTooLargeError",
        "document_too_large",
        True,
    )
    assert error.__cause__ is None and "501" not in repr(error)
    assert recorder.calls == []  # the engine was never reached


async def test_exactly_500_pages_pass_the_limit_and_reach_the_engine(data, repository, executor, recorder):
    store = store_with(str_key(data), page_pdf(MAX_PDF_PAGES))

    result = await run(activities(store, executor), data)

    assert result.status == "parsed"
    assert recorder.calls == [("original.pdf", formats.PDF_MEDIA_TYPE)]


async def test_a_zero_page_pdf_is_pdf_parse_failed_because_pdfium_cannot_open_it(data, repository, executor, recorder):
    store = store_with(str_key(data), page_pdf(0))

    error = await failure_of(run(activities(store, executor), data))

    assert (error.type, error.message) == ("PdfParseError", "pdf_parse_failed")
    assert recorder.calls == []


async def test_a_corrupt_pdf_is_pdf_parse_failed_from_the_limit_check(data, repository, executor, recorder):
    store = store_with(str_key(data), b"%PDF-1.4\nthis is not a real pdf\n")

    error = await failure_of(run(activities(store, executor), data))

    assert (error.type, error.message, error.non_retryable) == ("PdfParseError", "pdf_parse_failed", True)
    assert error.__cause__ is None
    assert recorder.calls == []


def str_key(data: ParseDocumentInput, media_type: str = formats.PDF_MEDIA_TYPE) -> str:
    return document_original(str(data.project_id), str(data.document_id), media_type)


# ---------------------------------------------------------------- error codes
@pytest.mark.parametrize(
    ("raised", "error_type", "code"),
    [
        (PdfParseError(f"cannot parse {PRIVATE_NAME}"), "PdfParseError", "pdf_parse_failed"),
        (DocumentTooLargeError(PRIVATE_NAME, 9, 5), "DocumentTooLargeError", "document_too_large"),
        (DocumentParseFailedError(f"bad {PRIVATE_NAME}"), "DocumentParseFailedError", "document_parse_failed"),
    ],
)
@pytest.mark.usefixtures("repository")
async def test_each_conversion_error_becomes_one_fixed_non_retryable_code(raised, error_type, code, data, executor):
    def parse(path: Path, *_args: object) -> ParsedMarkdown:
        raise raised

    store = store_with(str_key(data), b"%PDF-fake")

    error = await failure_of(run(activities(store, executor, parse=parse), data))

    assert (error.type, error.message, error.non_retryable) == (error_type, code, True)
    assert error.__cause__ is None and PRIVATE_NAME not in repr(error)
    assert code in PARSE_ERROR_CODES


@pytest.mark.parametrize("code", ["pdf_parse_failed", "document_too_large", "document_parse_failed"])
def test_the_three_parse_codes_show_as_the_reason_read_failed(code):
    outcome = source_files.file_state(
        source_failed=False,
        item_state="processed",
        item_error_reason=None,
        holds=True,
        doc_status="failed",
        doc_error=code,
        doc_age=timedelta(minutes=1),
    )

    assert (outcome.state, outcome.reason) == ("failed", "read_failed")


@pytest.mark.parametrize("code", ["internal_error", "document_not_found", "original_not_found", None])
def test_the_other_codes_keep_the_reason_unknown(code):
    outcome = source_files.file_state(
        source_failed=False,
        item_state="processed",
        item_error_reason=None,
        holds=True,
        doc_status="failed",
        doc_error=code,
        doc_age=timedelta(minutes=1),
    )

    assert (outcome.state, outcome.reason) == ("failed", "unknown")


def test_the_closed_reason_set_is_unchanged():
    assert source_files.REASON_CODES == (
        "source_failed",
        "read_failed",
        "unknown",
        "already_in_project",
        "not_readable",
        "not_imported",
        "access_lost",
        "drive_unavailable",
        "store_failed",
    )


def test_the_read_failed_sql_covers_the_same_codes():
    compiled = source_files._files_cte(uuid4()).compile(dialect=postgresql.dialect())
    bound = [value for value in compiled.params.values() if isinstance(value, (list, tuple))]

    assert list(source_files.READ_FAILED_DOCUMENT_ERRORS) in [list(value) for value in bound]


# ------------------------------------------------------------------ progress
async def test_heartbeats_carry_pages_done_and_total_and_each_range_is_logged(data, repository, executor, caplog):
    """The parse thread reports 10/30, then waits until a heartbeat showed it, reports 20/30 and waits again."""

    beats: list[dict] = []
    shown = {10: threading.Event(), 20: threading.Event()}

    def on_heartbeat(*details: Any) -> None:
        beats.append(details[0])
        event = shown.get(details[0].get("pages_done"))
        if event is not None:
            event.set()

    def parse(path: Path, media_type: str, on_progress) -> ParsedMarkdown:
        for done in (10, 20):
            on_progress(done, 30)
            assert shown[done].wait(10), "no heartbeat showed the progress"
        return ParsedMarkdown(markdown="ok", parser="stub@1")

    env = ActivityEnvironment()
    env.on_heartbeat = on_heartbeat
    caplog.set_level(logging.INFO)
    store = store_with(str_key(data), b"%PDF-")

    await run(activities(store, executor, parse=parse, heartbeat_interval=0.05), data, env)

    pairs = [(b["pages_done"], b["pages_total"]) for b in beats]
    assert pairs[0] == (None, None)  # the first beat comes before any range is done
    assert (10, 30) in pairs and (20, 30) in pairs
    assert pairs.index((10, 30)) < pairs.index((20, 30))
    ranges = [r for r in caplog.records if "document.parse.progress" in r.getMessage()]
    assert len(ranges) == 2


async def test_a_real_pdf_parse_stores_docling_markdown_and_reports_its_progress(data, repository, executor, caplog):
    """extraction.pdf through the whole activity: Docling Markdown, the Docling engine id, one range logged."""

    store = store_with(str_key(data), PDF_FIXTURE.read_bytes())
    caplog.set_level(logging.INFO)

    result = await run(activities(store, executor), data)

    ((markdown, parser),) = repository.parsed
    assert parser == engine_id() and parser.startswith("docling@")
    assert markdown.strip() and result.markdown_chars == len(markdown)
    done = [r for r in caplog.records if "document.parse.progress" in r.getMessage()]
    assert len(done) == 1


# ----------------------------------------------------------------- time limits
MARGIN_MINUTES = 10


def test_the_execution_limit_covers_every_attempt_plus_a_margin():
    attempts = workflows.PARSE_RETRY.maximum_attempts
    assert attempts * workflows.PARSE_START_TO_CLOSE + timedelta(minutes=MARGIN_MINUTES) <= workflows.EXECUTION_TIMEOUT


def test_the_limits_are_the_designed_values():
    assert timedelta(minutes=120) == workflows.PARSE_START_TO_CLOSE
    assert workflows.PARSE_RETRY.maximum_attempts == 2
    assert timedelta(minutes=250) == workflows.EXECUTION_TIMEOUT
    assert timedelta(seconds=60) == workflows.PARSE_HEARTBEAT_TIMEOUT  # unchanged: it detects a dead worker


def test_one_maximum_size_parse_fits_one_attempt_with_room_for_a_queue():
    worst_parse = timedelta(seconds=MAX_PDF_PAGES * 2.86 * 1.2)  # DESIGN: 2.86 s per page, +20 % noise
    assert worst_parse * 3 < workflows.PARSE_START_TO_CLOSE


@pytest.mark.parametrize(
    ("error_type", "code"),
    [("DocumentTooLargeError", "document_too_large"), ("DocumentParseFailedError", "document_parse_failed")],
)
def test_the_workflow_maps_the_new_types_and_never_retries_them(error_type, code):
    cause = ApplicationError(code, type=error_type)
    error = _activity_error(cause)

    assert workflows.failure_code(error) == code
    assert error_type in workflows.PARSE_RETRY.non_retryable_error_types
    assert code in PARSE_ERROR_CODES


def test_a_timeout_stays_an_internal_error():
    error = _activity_error(TemporalTimeoutError("t", type=None, last_heartbeat_details=[]))

    assert workflows.failure_code(error) == workflows.INTERNAL_ERROR


def _activity_error(cause: BaseException):
    error = ActivityError(
        "failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="w",
        activity_type="parse_document",
        activity_id="1",
        retry_state=None,
    )
    error.__cause__ = cause
    return error


# ---------------------------------------------------------------------- replay
async def test_histories_recorded_with_the_old_timeouts_replay_under_the_new_constants():
    """Decision: no version marker. The recorded ``ScheduleActivityTask`` carries 45 min / 3 attempts; the
    workflow now schedules 120 min / 2 attempts. Replay matches commands by type and activity id, not by
    timeout values, so it passes; running executions keep the options they were started with (the server
    holds them), and only a new execution or a new activity schedule uses the new values."""

    text = (FIXTURE_DIR / "parse_success.json").read_text()
    assert '"startToCloseTimeout": "2700s"' in text  # the recorded 45 minutes
    assert timedelta(seconds=2700) != workflows.PARSE_START_TO_CLOSE
    history = WorkflowHistory.from_json(FIXED_WORKFLOW_ID, text)

    await Replayer(workflows=[workflows.ParseDocumentWorkflow], data_converter=pydantic_data_converter).replay_workflow(
        history
    )


def test_an_upload_goes_stale_exactly_when_the_parse_workflow_times_out():
    """A parse may legitimately run (or queue) until ``EXECUTION_TIMEOUT``; earlier it is not stale (DESIGN D11)."""

    assert UPLOAD_STALE_AFTER == workflows.EXECUTION_TIMEOUT == timedelta(minutes=250)
