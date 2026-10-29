"""Tier 1: the parse activities with fakes (AC-8.4, AC-8.6 classification, AC-16.0)."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from file_parsers.pdf.to_markdown import ParsedMarkdown, PdfParseError, pdf_file_to_markdown
from specflow_parse_support import (
    PDF_FIXTURE,
    FakeRepository,
    FakeSessions,
    install_fake_repository,
)
from storage.keys import document_original
from storage.memory import InMemoryObjectStore
from temporal.documents.activities import ParseActivities
from temporal.documents.models import MarkFailedInput, ParseDocumentInput

PRIVATE_NAME = "confidential-contract-name.pdf"
BEATS_NEEDED = 3
HOLD_SECONDS = 0.1  # long enough for a second, concurrent parse to overlap this one
TICK_SECONDS = 0.01


@pytest.fixture
def data() -> ParseDocumentInput:
    return ParseDocumentInput(project_id=uuid4(), document_id=uuid4())


@pytest.fixture
def store(data) -> InMemoryObjectStore:
    store = InMemoryObjectStore(app_env="local")
    store.objects[document_original(str(data.project_id), str(data.document_id))] = b"%PDF-fake"
    return store


@pytest.fixture
def repository(monkeypatch) -> FakeRepository:
    repository = FakeRepository()
    install_fake_repository(monkeypatch, repository)
    return repository


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parse-test")
    yield pool
    pool.shutdown(wait=True)


def build(store, executor, parse, **kwargs) -> ParseActivities:
    return ParseActivities(FakeSessions(), store, executor, parse=parse, **kwargs)


def ok_parse(path: Path) -> ParsedMarkdown:
    return ParsedMarkdown(markdown="# Title\n\nbody")


async def run_parse(activities: ParseActivities, data, env: ActivityEnvironment | None = None):
    return await (env or ActivityEnvironment()).run(activities.parse_document, data)


async def failure_of(coro) -> ApplicationError:
    with pytest.raises(ApplicationError) as caught:
        await coro
    return caught.value


async def test_success_stores_markdown_and_returns_the_result(data, store, repository, executor):
    result = await run_parse(build(store, executor, ok_parse), data)

    assert (result.document_id, result.status, result.markdown_chars) == (
        data.document_id,
        "parsed",
        len("# Title\n\nbody"),
    )
    assert repository.parsed == [("# Title\n\nbody", "pymupdf4llm")]


async def test_the_real_parser_handles_the_fixture_pdf(data, repository, executor):
    store = InMemoryObjectStore(app_env="local")
    key = document_original(str(data.project_id), str(data.document_id))
    store.objects[key] = PDF_FIXTURE.read_bytes()

    result = await run_parse(build(store, executor, pdf_file_to_markdown), data)

    assert result.markdown_chars > 0
    assert repository.parsed[0][0].strip()


async def test_nul_characters_are_removed_before_the_write(data, store, repository, executor):
    def parse(_path: Path) -> ParsedMarkdown:
        return ParsedMarkdown(markdown="a\x00b")

    result = await run_parse(build(store, executor, parse), data)

    assert repository.parsed[0][0] == "ab"
    assert result.markdown_chars == 2


async def test_rerunning_is_idempotent(data, store, repository, executor):
    activities = build(store, executor, ok_parse)

    first = await run_parse(activities, data)
    second = await run_parse(activities, data)

    assert first == second
    assert len(repository.parsed) == 2


@pytest.mark.parametrize(
    ("scenario", "error_type", "message"),
    [
        ("missing_row", "DocumentNotFound", "document_not_found"),
        ("missing_object", "OriginalNotFound", "original_not_found"),
        ("corrupt_pdf", "PdfParseError", "pdf_parse_failed"),
    ],
)
async def test_known_failures_are_non_retryable_fixed_codes(
    *, scenario, error_type, message, data, store, repository, executor
):
    def corrupt(path: Path) -> ParsedMarkdown:
        raise PdfParseError(f"cannot parse {PRIVATE_NAME} at /Users/someone/{PRIVATE_NAME}")

    parse = ok_parse
    if scenario == "missing_row":
        repository.exists = False
    elif scenario == "missing_object":
        store.objects.clear()
    else:
        parse = corrupt

    error = await failure_of(run_parse(build(store, executor, parse), data))

    assert (error.type, error.message, error.non_retryable) == (error_type, message, True)
    assert error.__cause__ is None
    assert PRIVATE_NAME not in repr(error) and "/Users" not in repr(error)


async def test_a_row_that_vanishes_before_the_write_is_document_not_found(data, store, repository, executor):
    repository.vanishes = True

    error = await failure_of(run_parse(build(store, executor, ok_parse), data))

    assert error.type == "DocumentNotFound"


async def test_unexpected_errors_are_retryable_and_carry_no_data(data, store, repository, executor):
    class BoomError(OSError):
        pass

    def broken(path: Path) -> ParsedMarkdown:
        raise BoomError(f"disk error on /Users/someone/{PRIVATE_NAME} password=hunter2")

    error = await failure_of(run_parse(build(store, executor, broken), data))

    assert (error.type, error.message, error.non_retryable) == ("ParseIoError", "internal_error", False)
    assert error.__cause__ is None
    assert "hunter2" not in repr(error) and PRIVATE_NAME not in repr(error)


async def test_a_database_error_on_the_write_is_retryable_and_sanitized(data, store, repository, executor):
    repository.fail_with = RuntimeError("connection to host db.internal password=hunter2 failed")

    error = await failure_of(run_parse(build(store, executor, ok_parse), data))

    assert (error.type, error.non_retryable) == ("ParseIoError", False)
    assert "hunter2" not in repr(error) and "db.internal" not in repr(error)


async def test_the_temp_directory_is_removed_on_success_and_failure(data, store, repository, executor):
    seen: list[Path] = []

    def spy(path: Path) -> ParsedMarkdown:
        assert path.exists()
        seen.append(path.parent)
        if len(seen) == 2:
            raise PdfParseError("x")
        return ParsedMarkdown(markdown="ok")

    activities = build(store, executor, spy)
    await run_parse(activities, data)
    await failure_of(run_parse(activities, data))

    assert len(seen) == 2
    assert not any(directory.exists() for directory in seen)


async def test_heartbeats_are_sent_while_the_parse_runs(data, store, repository, executor):
    beats: list[tuple] = []
    enough_beats = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_heartbeat(*details: object) -> None:
        beats.append(details)
        if len(beats) >= BEATS_NEEDED:
            loop.call_soon_threadsafe(enough_beats.set)

    env = ActivityEnvironment()
    env.on_heartbeat = on_heartbeat

    release = threading.Event()

    def slow(path: Path) -> ParsedMarkdown:
        release.wait(10)  # held until the test saw enough heartbeats
        return ParsedMarkdown(markdown="ok")

    run = asyncio.create_task(run_parse(build(store, executor, slow, heartbeat_interval=0.05), data, env))
    try:
        await asyncio.wait_for(enough_beats.wait(), 10)
    finally:
        release.set()
    await run

    assert len(beats) >= BEATS_NEEDED
    # TEMPORARY (freeze investigation): numbers-only progress details.
    assert beats[-1][0]["stage"] == "parse"
    assert set(beats[-1][0]) == {"stage", "elapsed_s", "stage_elapsed_s", "rss_mb", "loop_lag_ms"}


async def test_the_parse_runs_off_the_event_loop_and_one_at_a_time(data, store, repository, executor):
    threads: set[str] = set()
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def tracked(path: Path) -> ParsedMarkdown:
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        threads.add(threading.current_thread().name)
        threading.Event().wait(HOLD_SECONDS)
        with lock:
            active["now"] -= 1
        return ParsedMarkdown(markdown="ok")

    activities = build(store, executor, tracked)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(TICK_SECONDS)

    task = asyncio.create_task(ticker())
    await asyncio.gather(run_parse(activities, data), run_parse(activities, data))
    task.cancel()

    assert active["max"] == 1
    assert all(name.startswith("parse-test") for name in threads)
    assert ticks > 5  # the loop stayed responsive during 0.2 s of parsing


async def test_mark_document_failed_stores_the_fixed_code(data, store, repository, executor):
    activities = build(store, executor, ok_parse)
    mark = MarkFailedInput(project_id=data.project_id, document_id=data.document_id, error_code="pdf_parse_failed")

    await ActivityEnvironment().run(activities.mark_document_failed, mark)

    assert repository.failed == ["pdf_parse_failed"]


async def test_mark_document_failed_rejects_an_unknown_code_without_retry(data, store, repository, executor):
    activities = build(store, executor, ok_parse)
    mark = MarkFailedInput(project_id=data.project_id, document_id=data.document_id, error_code="free text /Users/x")

    error = await failure_of(ActivityEnvironment().run(activities.mark_document_failed, mark))

    assert (error.type, error.non_retryable) == ("InvalidErrorCode", True)
    assert "/Users" not in repr(error)


async def test_mark_document_failed_database_errors_are_retryable_and_sanitized(data, store, repository, executor):
    repository.fail_with = RuntimeError("password=hunter2")
    activities = build(store, executor, ok_parse)
    mark = MarkFailedInput(project_id=data.project_id, document_id=data.document_id, error_code="internal_error")

    error = await failure_of(ActivityEnvironment().run(activities.mark_document_failed, mark))

    assert (error.type, error.non_retryable) == ("MarkFailedError", False)
    assert "hunter2" not in repr(error)
