"""Tier 1: workflow module hygiene, failure classification, registration (AC-8.1, AC-8.5)."""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from temporalio import activity, workflow
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from db.repositories import PARSE_ERROR_CODES
from settings import load_settings
from storage.memory import InMemoryObjectStore
from temporal.cleanup.workflows import SourceCleanupWorkflow
from temporal.documents import registration, workflows
from temporal.registry import Registration, load_registration
from temporal.sources.workflows import DriveImportWorkflow
from temporal.worker import has_sync_activities


def _with_cause(cause: BaseException) -> ActivityError:
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


def test_the_workflow_module_imports_no_infrastructure_packages():
    code = (
        "import sys\n"
        "import temporal.documents.workflows\n"
        "bad = [m for m in ('sqlalchemy', 'asyncpg', 'google.cloud', 'pymupdf', 'pymupdf4llm',"
        " 'settings', 'db', 'storage', 'file_parsers')"
        " if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""


def test_the_workflow_is_named_and_uses_the_specified_policies():
    definition = workflow._Definition.must_from_class(workflows.ParseDocumentWorkflow)
    assert definition.name == "ParseDocumentWorkflow"
    assert timedelta(minutes=60) == workflows.EXECUTION_TIMEOUT
    assert timedelta(minutes=45) == workflows.PARSE_START_TO_CLOSE
    assert timedelta(seconds=60) == workflows.PARSE_HEARTBEAT_TIMEOUT
    assert timedelta(seconds=30) == workflows.MARK_START_TO_CLOSE
    retry = workflows.PARSE_RETRY
    assert (retry.maximum_attempts, retry.initial_interval, retry.backoff_coefficient) == (
        3,
        timedelta(seconds=5),
        2.0,
    )
    assert set(retry.non_retryable_error_types) >= {"DocumentNotFound", "PdfParseError"}
    assert workflows.MARK_RETRY.maximum_attempts == 5


@pytest.mark.parametrize(
    ("cause", "code"),
    [
        (ApplicationError("document_not_found", type="DocumentNotFound"), "document_not_found"),
        (ApplicationError("pdf_parse_failed", type="PdfParseError"), "pdf_parse_failed"),
        (ApplicationError("original_not_found", type="OriginalNotFound"), "original_not_found"),
        (ApplicationError("internal_error", type="ParseIoError"), "internal_error"),
        (ApplicationError("pdf_parse_failed", type="SomethingElse"), "internal_error"),
        (TemporalTimeoutError("timed out", type=None, last_heartbeat_details=[]), "internal_error"),
    ],
)
def test_failure_codes_come_from_the_error_type_only(cause, code):
    assert workflows.failure_code(_with_cause(cause)) == code


def test_every_failure_code_is_accepted_by_the_repository():
    assert set(workflows.FAILURE_CODES.values()) | {workflows.INTERNAL_ERROR} <= PARSE_ERROR_CODES


def test_a_cancellation_cause_is_recognised():
    assert isinstance(_with_cause(CancelledError("c")).cause, CancelledError)


async def test_registration_lists_the_parse_drive_import_and_cleanup_parts():
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/none")
    built = registration.build_registration(engine, InMemoryObjectStore(app_env="local"))

    assert built.workflows == [
        workflows.ParseDocumentWorkflow,
        DriveImportWorkflow,
        SourceCleanupWorkflow,
    ]
    names = {activity._Definition.must_from_callable(fn).name for fn in built.activities}
    assert names == {
        "parse_document",
        "mark_document_failed",
        "drive_list",
        "drive_next_batch",
        "drive_import_item",
        "drive_finish_item",
        "drive_wait_document",
        "drive_complete",
        "drive_fail_import",
        "sweep_source_objects",
    }
    await built.close()


async def test_close_disposes_the_engine_and_stops_the_executor():
    disposed: list[bool] = []

    class FakeEngine:
        async def dispose(self) -> None:
            disposed.append(True)

    engine = FakeEngine()
    built = registration.build_registration(cast(AsyncEngine, engine), InMemoryObjectStore(app_env="local"))
    executor = built.activities[0].__self__._executor

    await built.close()

    assert disposed == [True]
    with pytest.raises(RuntimeError):
        executor.submit(int)


async def test_the_worker_registry_loads_the_parse_registration(valid_env, monkeypatch):
    monkeypatch.setattr(
        "storage.factory.build_object_store", lambda _settings, **kw: InMemoryObjectStore(app_env=kw["app_env"])
    )
    settings = load_settings("worker", valid_env, queues=["parse"])

    loaded = load_registration("parse", settings)

    assert isinstance(loaded, Registration)
    assert loaded.workflows == [
        workflows.ParseDocumentWorkflow,
        DriveImportWorkflow,
        SourceCleanupWorkflow,
    ]
    assert loaded.close is not None
    await loaded.close()


def test_parse_activities_are_async_so_the_queue_needs_no_sync_executor():
    engine = create_async_engine("postgresql+asyncpg://u:p@localhost:1/none")
    built = registration.build_registration(engine, InMemoryObjectStore(app_env="local"))

    assert not has_sync_activities(built)
