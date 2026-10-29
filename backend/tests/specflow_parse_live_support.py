"""Tier-2 fixtures and helpers of the parse live tests (no tests here).

Scratch database, GCS prefix ``dev/local/pytest/<run-id>/``, the production
Temporal client, a ``Stack`` to seed documents and start workflows, and the
history recorder (``SPECFLOW_PARSE_RECORD=1``).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from agents import set_trace_processors, set_tracing_disabled
from google.cloud import storage
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from temporalio.client import WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.worker import Worker

from agent_runtime import tracing
from db import repositories
from db.models import User
from db.session import build_session_factory
from db.sources import DocumentFile
from logger.logger import INFO, get_logger, log_event
from settings import DatabaseSettings, StorageSettings, load_settings
from specflow_live import requires_live
from specflow_parse_support import (
    FIXTURE_DIR,
    find_leaks,
    scrub_history,
)
from storage.gcs import GcsObjectStore
from storage.keys import document_original
from temporal import ids
from temporal.client import connect
from temporal.documents import registration
from temporal.documents.models import ParseDocumentInput
from temporal.documents.workflows import EXECUTION_TIMEOUT, ParseDocumentWorkflow

pytestmark = [requires_live("TEMPORAL"), requires_live("POSTGRES"), requires_live("GCS")]
logger = get_logger("tests.specflow_parse_live")
RECORD = os.environ.get("SPECFLOW_PARSE_RECORD") == "1"
CORRUPT_PDF = b"%PDF-1.4\nthis is not a real pdf\n"
KILL_TEST_WAIT_SECONDS = 150


# ------------------------------------------------------------------- fixtures
def _count(client, bucket: str, prefix: str) -> int:
    return sum(1 for _ in client.list_blobs(bucket, prefix=prefix))


@pytest.fixture(scope="module")
def gcs(request):
    """Store under ``dev/local/pytest/<run-id>/``; teardown deletes it, counts reported."""

    reporter = request.config.pluginmanager.get_plugin("terminalreporter")

    def report(phase: str, prefix: str, objects: int) -> None:
        log_event(logger, INFO, "parse.live.counts", phase=phase, prefix=prefix, objects=objects)
        if reporter:
            reporter.write_line(f"\nparse live GCS objects {phase}: {objects} under {prefix}")

    prefix = f"dev/local/pytest/{uuid.uuid4().hex[:12]}/"
    cfg = StorageSettings(
        STORAGE_BUCKET=os.environ["STORAGE_BUCKET"],
        STORAGE_PREFIX=prefix,
        GOOGLE_CLOUD_PROJECT=os.environ["GOOGLE_CLOUD_PROJECT"],
    )
    client = storage.Client(project=cfg.google_cloud_project)
    report("before", prefix, _count(client, cfg.bucket, prefix))
    yield GcsObjectStore(cfg, client=client), prefix
    for blob in list(client.list_blobs(cfg.bucket, prefix=prefix)):
        blob.delete()
    after = _count(client, cfg.bucket, prefix)
    report("after", prefix, after)
    assert after == 0


@pytest.fixture
def isolated_tracing(monkeypatch):
    """Undo the process-wide tracing switches ``connect`` makes."""

    monkeypatch.setenv(tracing.DISABLE_ENV, "")
    monkeypatch.delenv(tracing.DISABLE_ENV)
    yield
    set_trace_processors([])
    set_tracing_disabled(False)
    # the env flag is restored by monkeypatch (set then unset above)


@dataclass
class Stack:
    client: object
    store: GcsObjectStore
    prefix: str
    database: DatabaseSettings
    engine: AsyncEngine
    sessions: async_sessionmaker
    temporal: object
    user_id: uuid.UUID

    def worker(self) -> tuple[Worker, object]:
        built = registration.build_registration(self.engine, self.store)
        worker = Worker(
            self.client,
            task_queue=self.temporal.task_queue,
            workflows=list(built.workflows),
            activities=list(built.activities),
        )
        return worker, built

    async def seed(self, pdf: bytes | None, *, project_id=None) -> ParseDocumentInput:
        """A project and a ``processing`` document; the original is uploaded when given."""

        document_id = uuid.uuid4()
        async with self.sessions() as session, session.begin():
            if project_id is None:
                project = await repositories.create_project(session, self.user_id, "pytest parse")
                project_id = project.id
            key = document_original(str(project_id), str(document_id))
            await repositories.insert_document(
                session,
                file=DocumentFile(
                    document_id=document_id,
                    content_hash=hashlib.sha256(document_id.bytes).hexdigest(),
                    filename="synthetic.pdf",
                    size_bytes=len(pdf or b""),
                    original_key=key,
                ),
                project_id=project_id,
                uploaded_by=self.user_id,
            )
            await repositories.mark_processing(session, project_id, document_id)
        if pdf is not None:
            await self.upload(project_id, document_id, pdf)
        return ParseDocumentInput(project_id=project_id, document_id=document_id)

    async def upload(self, project_id, document_id, pdf: bytes) -> None:
        key = document_original(str(project_id), str(document_id))
        writer = await self.store.open_writer(key, content_type="application/pdf")
        await writer.write(pdf)
        await writer.close()

    async def row(self, data: ParseDocumentInput):
        async with self.sessions() as session:
            return await repositories.get_document(session, data.project_id, data.document_id)

    async def start(self, data: ParseDocumentInput, **options) -> WorkflowHandle:
        options.setdefault("execution_timeout", EXECUTION_TIMEOUT)
        handle = await self.client.start_workflow(
            ParseDocumentWorkflow.run,
            data,
            id=ids.parse_workflow_id(data.document_id, prefix=self.temporal.id_prefix),
            task_queue=self.temporal.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            **options,
        )
        return self.temporal.track(handle)


@pytest_asyncio.fixture
async def stack(migrated_database, gcs, temporal_live, valid_env, isolated_tracing) -> AsyncIterator[Stack]:
    store, prefix = gcs
    env = {
        **valid_env,
        "TEMPORAL_ADDRESS": os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        "TEMPORAL_NAMESPACE": "specflow-local",
    }
    client = await connect(load_settings("worker", env))
    engine = create_async_engine(migrated_database.url())
    sessions = build_session_factory(engine)
    async with sessions() as session, session.begin():
        user = User(actor_id=f"human:t{uuid.uuid4().hex[:8]}", display_name="T", email="t@example.com")
        session.add(user)
    yield Stack(client, store, prefix, migrated_database, engine, sessions, temporal_live, user.id)
    await engine.dispose()


async def record(handle: WorkflowHandle, name: str, *, stack: Stack, data: ParseDocumentInput) -> str:
    """Raw-history leak scan (AC-8.4); with ``SPECFLOW_PARSE_RECORD=1`` writes the scrubbed fixture."""

    history = await handle.fetch_history()
    raw = history.to_json()
    row = await stack.row(data)
    extras = (
        row.original_filename,
        os.environ.get("STORAGE_BUCKET", ""),
        os.environ.get("DATABASE_PASSWORD", ""),
        (row.markdown or "")[:40].strip(),
    )
    assert find_leaks(raw, extra=extras, patterns=False) == []
    assert len(raw) < 2 * 1024 * 1024
    if RECORD:
        scrubbed = scrub_history(
            raw,
            workflow_id=handle.id,
            task_queues=[stack.temporal.task_queue],
            run_ids=[handle.result_run_id or handle.first_execution_run_id or ""],
        )
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        text = json.dumps(scrubbed, indent=1, sort_keys=True) + "\n"
        assert find_leaks(text, extra=extras) == []
        (FIXTURE_DIR / name).write_text(text)
    return raw


def activity_types(raw_history: str) -> list[str]:
    events = json.loads(raw_history)["events"]
    return [
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in events
        if "activityTaskScheduledEventAttributes" in e
    ]
