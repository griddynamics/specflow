"""Tier 1: follow-up checks of the parse workflow and activities (S8 follow-up).

- the workflow code path with a fake ``execute_activity``: the scheduled
  timeouts and retry policies of BOTH activities, the code stored on failure,
  the non-retryable final error and that a cancellation is never swallowed
  (these were not pinned by the replay fixtures),
- a document waiting for the parse slot keeps heartbeating,
- expected failures are benign (DEBUG in the SDK) and logged as WARNING,
- ``Registration.close`` releases the object store, idempotently.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from temporalio import workflow as temporal_workflow
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    ApplicationErrorCategory,
    CancelledError,
)
from temporalio.testing import ActivityEnvironment

from file_parsers.pdf.to_markdown import ParsedMarkdown, PdfParseError
from specflow_parse_support import FakeRepository, FakeSessions, install_fake_repository
from storage.keys import document_original
from storage.memory import InMemoryObjectStore
from temporal.documents import registration, workflows
from temporal.documents.activities import ParseActivities
from temporal.documents.models import MarkFailedInput, ParseDocumentInput, ParseDocumentResult


def _activity_error(cause: BaseException) -> ActivityError:
    error = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="parse_document",
        activity_id="1",
        retry_state=None,
    )
    error.__cause__ = cause
    return error


class FakeWorkflowApi:
    """Replaces ``workflow.execute_activity``: records the calls, fails on demand."""

    def __init__(self, parse_failure: BaseException | None = None, mark_failure: BaseException | None = None):
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.parse_failure = parse_failure
        self.mark_failure = mark_failure

    async def execute_activity(self, name: str, arg: Any = None, **kwargs: Any) -> Any:
        self.calls.append((name, arg, kwargs))
        failure = self.parse_failure if name == workflows.PARSE_ACTIVITY else self.mark_failure
        if failure is not None:
            raise failure
        return ParseDocumentResult(document_id=arg.document_id, status="parsed", markdown_chars=1)


@pytest.fixture
def data() -> ParseDocumentInput:
    return ParseDocumentInput(project_id=uuid4(), document_id=uuid4())


def run_workflow(monkeypatch, api: FakeWorkflowApi, data: ParseDocumentInput):
    monkeypatch.setattr(temporal_workflow, "execute_activity", api.execute_activity)
    return workflows.ParseDocumentWorkflow().run(data)


async def test_the_parse_activity_is_scheduled_with_the_specified_options(monkeypatch, data):
    api = FakeWorkflowApi()

    await run_workflow(monkeypatch, api, data)

    ((name, arg, options),) = api.calls
    assert (name, arg) == ("parse_document", data)
    assert options["start_to_close_timeout"] == timedelta(minutes=45)
    assert options["heartbeat_timeout"] == timedelta(seconds=60)
    retry = options["retry_policy"]
    assert (retry.maximum_attempts, retry.initial_interval, retry.backoff_coefficient) == (
        3,
        timedelta(seconds=5),
        2.0,
    )
    assert set(retry.non_retryable_error_types) == {
        "DocumentNotFound",
        "PdfParseError",
        "OriginalNotFound",
    }
    assert options["result_type"] is ParseDocumentResult


@pytest.mark.parametrize(
    ("error_type", "code"),
    [
        ("PdfParseError", "pdf_parse_failed"),
        ("DocumentNotFound", "document_not_found"),
        ("OriginalNotFound", "original_not_found"),
        ("ParseIoError", "internal_error"),
    ],
)
async def test_a_failed_parse_marks_the_document_with_its_code_and_fails_for_good(monkeypatch, data, error_type, code):
    api = FakeWorkflowApi(parse_failure=_activity_error(ApplicationError("x", type=error_type)))

    with pytest.raises(ApplicationError) as caught:
        await run_workflow(monkeypatch, api, data)

    assert (caught.value.message, caught.value.type, caught.value.non_retryable) == (
        code,
        "ParseDocumentFailed",
        True,
    )
    assert [name for name, _, _ in api.calls] == ["parse_document", "mark_document_failed"]
    _, mark_input, mark_options = api.calls[1]
    assert mark_input == MarkFailedInput(project_id=data.project_id, document_id=data.document_id, error_code=code)
    assert mark_options["start_to_close_timeout"] == timedelta(seconds=30)
    assert mark_options["retry_policy"].maximum_attempts == 5


async def test_a_cancelled_parse_is_not_swallowed_and_marks_nothing(monkeypatch, data):
    api = FakeWorkflowApi(parse_failure=_activity_error(CancelledError("cancelled")))

    with pytest.raises(ActivityError) as caught:
        await run_workflow(monkeypatch, api, data)

    assert isinstance(caught.value.cause, CancelledError)
    assert [name for name, _, _ in api.calls] == ["parse_document"]


async def test_a_cancelled_marking_is_not_swallowed(monkeypatch, data):
    api = FakeWorkflowApi(
        parse_failure=_activity_error(ApplicationError("x", type="PdfParseError")),
        mark_failure=_activity_error(CancelledError("cancelled")),
    )

    with pytest.raises(ActivityError):
        await run_workflow(monkeypatch, api, data)


async def test_a_failing_marking_still_fails_the_workflow_with_the_code(monkeypatch, data):
    api = FakeWorkflowApi(
        parse_failure=_activity_error(ApplicationError("x", type="PdfParseError")),
        mark_failure=_activity_error(ApplicationError("db down", type="MarkFailedError")),
    )

    with pytest.raises(ApplicationError) as caught:
        await run_workflow(monkeypatch, api, data)

    assert caught.value.message == "pdf_parse_failed"
    assert caught.value.__cause__ is None


# ---------------------------------------------------------------- activities
@pytest.fixture
def store(data) -> InMemoryObjectStore:
    store = InMemoryObjectStore(app_env="local")
    store.objects[document_original(str(data.project_id), str(data.document_id))] = b"%PDF-fake"
    return store


BEATS_NEEDED = 5


@pytest.fixture
def repository(monkeypatch) -> FakeRepository:
    repository = FakeRepository()
    install_fake_repository(monkeypatch, repository)
    return repository


async def test_a_document_waiting_for_the_parse_slot_keeps_heartbeating(data, store, repository):
    release = threading.Event()
    started = threading.Event()
    other = ParseDocumentInput(project_id=data.project_id, document_id=uuid4())
    store.objects[document_original(str(other.project_id), str(other.document_id))] = b"%PDF-fake"

    def parse(path: Path) -> ParsedMarkdown:
        if not started.is_set():  # the first caller holds the slot until released
            started.set()
            release.wait(10)
        return ParsedMarkdown(markdown="ok")

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        activities = ParseActivities(FakeSessions(), store, pool, parse=parse, heartbeat_interval=0.02)
        beats: list[tuple] = []
        enough_beats = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_heartbeat(*details: Any) -> None:
            beats.append(details)
            if len(beats) >= BEATS_NEEDED:
                loop.call_soon_threadsafe(enough_beats.set)

        waiting_env = ActivityEnvironment()
        waiting_env.on_heartbeat = on_heartbeat

        first = asyncio.create_task(ActivityEnvironment().run(activities.parse_document, data))
        assert await asyncio.to_thread(started.wait, 10), "the first parse never started"
        second = asyncio.create_task(waiting_env.run(activities.parse_document, other))
        # The second one heartbeats while queued behind the slot.
        await asyncio.wait_for(enough_beats.wait(), 10)
        assert not second.done()
        release.set()

        results = await asyncio.wait_for(asyncio.gather(first, second), 10)
    finally:
        release.set()
        pool.shutdown(wait=True)

    assert [r.status for r in results] == ["parsed", "parsed"]


@pytest.mark.parametrize(
    ("scenario", "benign"),
    [("corrupt", True), ("missing_row", True), ("missing_object", True), ("io", False)],
)
async def test_only_expected_failures_are_benign(scenario, benign, data, store, repository):
    def parse(path: Path) -> ParsedMarkdown:
        if scenario == "corrupt":
            raise PdfParseError("x")
        if scenario == "io":
            raise OSError("x")
        return ParsedMarkdown(markdown="ok")

    if scenario == "missing_row":
        repository.exists = False
    if scenario == "missing_object":
        store.objects.clear()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        activities = ParseActivities(FakeSessions(), store, pool, parse=parse)
        with pytest.raises(ApplicationError) as caught:
            await ActivityEnvironment().run(activities.parse_document, data)
    finally:
        pool.shutdown(wait=True)

    expected = ApplicationErrorCategory.BENIGN if benign else ApplicationErrorCategory.UNSPECIFIED
    assert caught.value.category == expected


@pytest.mark.parametrize(
    ("code", "level"),
    [
        ("pdf_parse_failed", logging.WARNING),
        ("document_not_found", logging.WARNING),
        ("original_not_found", logging.WARNING),
        ("internal_error", logging.ERROR),
    ],
)
async def test_expected_failures_are_logged_as_warning_not_error(*, code, level, data, store, repository, caplog):
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        activities = ParseActivities(FakeSessions(), store, pool)
        caplog.set_level(logging.DEBUG)
        await ActivityEnvironment().run(
            activities.mark_document_failed,
            MarkFailedInput(project_id=data.project_id, document_id=data.document_id, error_code=code),
        )
    finally:
        pool.shutdown(wait=True)

    failed = [r for r in caplog.records if "document.parse.failed" in r.getMessage()]
    assert [r.levelno for r in failed] == [level]
    assert all(r.exc_info is None for r in failed)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR and code != "internal_error"]


# -------------------------------------------------------------- registration
async def test_close_closes_the_object_store_once_and_is_idempotent():
    closed: list[bool] = []

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    class ClosableStore(InMemoryObjectStore):
        async def close(self) -> None:
            closed.append(True)

    built = registration.build_registration(cast(Any, FakeEngine()), cast(Any, ClosableStore(app_env="local")))

    await built.close()
    await built.close()

    assert closed == [True]


async def test_close_releases_the_store_even_when_the_engine_fails():
    closed: list[bool] = []

    class BrokenEngine:
        async def dispose(self) -> None:
            raise RuntimeError("dispose failed")

    class ClosableStore(InMemoryObjectStore):
        async def close(self) -> None:
            closed.append(True)

    built = registration.build_registration(cast(Any, BrokenEngine()), cast(Any, ClosableStore(app_env="local")))

    with pytest.raises(RuntimeError):
        await built.close()

    assert closed == [True]
