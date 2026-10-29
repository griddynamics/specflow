"""Tier 1: the API's graph build wiring: the Postgres corpus and the in-process runner.

The corpus is checked on hand-made rows (plus the SQL text of its query); the runner on a stub
``run_build``, a stub gateway and real file stores under ``tmp_path``. No Neo4j, no model.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest
from sqlalchemy.dialects import postgresql

from api.graph_build import runner as runner_module
from api.graph_build.corpus import PostgresCorpusSource, corpus_revisions
from api.graph_build.runner import GraphBuildRunner, RunnerDependencies
from db import sources as sources_repo
from db.sources import GraphCorpusRow, graph_corpus_statement
from graph.build import BuildOptions, BuildRecord, BuildResult, SpecChanges, VerificationSummary, open_file_stores
from graph.core import ConfigurationError
from logger_harness import assert_json_plain, fields
from settings import GraphBuildSettings

T0 = datetime(2026, 10, 1, tzinfo=UTC)
PROJECT = str(uuid.UUID(int=1))
OTHER = str(uuid.UUID(int=2))


# Corpus ----------------------------------------------------------------------------


def row(**values: Any) -> GraphCorpusRow:
    defaults: dict[str, Any] = {
        "document_id": uuid.uuid4(),
        "origin": "upload",
        "content_hash": "a" * 64,
        "original_filename": "spec.pdf",
        "created_at": T0,
        "source_id": None,
        "holder_name": None,
        "drive_connection_id": None,
        "drive_external_id": None,
        "drive_source_name": None,
        "markdown": "# Spec\n\nText.",
    }
    return GraphCorpusRow(**(defaults | values))


def test_upload_and_drive_documents_get_their_lineages():
    upload = row(original_filename="notes.pdf", holder_name="Batch 1")
    folder = uuid.uuid4()
    drive = row(
        origin="connection",
        original_filename="api.md",
        source_id=folder,
        drive_connection_id=folder,
        drive_external_id="FILE_123456",
        drive_source_name="Specs",
        content_hash="b" * 64,
    )

    revisions = corpus_revisions([upload, drive])

    by_lineage = {revision.lineage_id: revision for revision in revisions}
    assert set(by_lineage) == {f"doc:{upload.document_id}", f"drive:{folder}:FILE_123456"}
    assert by_lineage[f"doc:{upload.document_id}"].path == "Batch 1/notes.pdf"
    drive_revision = by_lineage[f"drive:{folder}:FILE_123456"]
    assert (drive_revision.path, drive_revision.revision_id, drive_revision.version) == ("Specs/api.md", "b" * 64, 1)


def test_one_document_linked_by_several_drive_files_is_one_lineage_of_its_holder():
    holder, other = uuid.UUID(int=9), uuid.UUID(int=3)
    document = uuid.uuid4()
    rows = [
        row(
            document_id=document,
            origin="connection",
            source_id=holder,
            drive_connection_id=other,
            drive_external_id="OTHER_FILE_1",
            drive_source_name="Other",
        ),
        row(
            document_id=document,
            origin="connection",
            source_id=holder,
            drive_connection_id=holder,
            drive_external_id="HELD_FILE_1",
            drive_source_name="Held",
        ),
    ]

    [revision] = corpus_revisions(rows)

    assert revision.lineage_id == f"drive:{holder}:HELD_FILE_1"
    assert revision.path == "Held/spec.pdf"


def test_only_the_newest_document_of_a_drive_lineage_is_kept():
    folder = uuid.uuid4()
    drive = {
        "origin": "connection",
        "drive_connection_id": folder,
        "drive_external_id": "FILE_123456",
        "drive_source_name": "Specs",
    }
    old = row(**drive, content_hash="1" * 64, markdown="# Old", created_at=T0)
    new = row(**drive, content_hash="2" * 64, markdown="# New", created_at=T0 + timedelta(days=1))

    [revision] = corpus_revisions([new, old])

    assert (revision.revision_id, revision.title, revision.version) == ("2" * 64, "New", 2)


def test_a_drive_document_without_a_drive_file_or_an_upload_linked_by_drive_stays_a_document():
    orphan = row(origin="connection")
    upload = row(origin="upload", drive_connection_id=uuid.uuid4(), drive_external_id="FILE_123456")

    lineages = {revision.lineage_id for revision in corpus_revisions([orphan, upload])}

    assert lineages == {f"doc:{orphan.document_id}", f"doc:{upload.document_id}"}


def test_order_is_by_path_paths_are_unique_and_the_title_falls_back_to_the_file_name():
    rows = [
        row(original_filename="b.pdf", markdown="no heading here"),
        row(original_filename="a.pdf", markdown="```\n# not a heading\n```\n"),
        row(original_filename="b.pdf", markdown="# B two"),
    ]

    revisions = corpus_revisions(rows)

    assert [revision.path for revision in revisions] == ["a.pdf", "b (2).pdf", "b.pdf"]
    assert revisions[0].title == "a"
    assert {revisions[1].title, revisions[2].title} == {"b", "B two"}


def test_the_query_keeps_processed_documents_with_text_of_the_project_only():
    sql = str(
        graph_corpus_statement(uuid.UUID(int=1)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "documents.status IN ('parsed', 'ingested', 'partially_ingested')" in sql
    assert "documents.markdown IS NOT NULL" in sql
    assert "length(btrim(documents.markdown)) > 0" in sql
    assert "documents.project_id = '00000000-0000-0000-0000-000000000001'" in sql
    assert "connection_items.project_id = '00000000-0000-0000-0000-000000000001'" in sql
    assert "connections.kind = 'gdrive'" in sql
    assert "documents.origin = 'connection'" in sql


async def test_the_postgres_source_reads_in_one_transaction(monkeypatch):
    seen: list[uuid.UUID] = []

    async def listed(session, project_id):
        seen.append(project_id)
        return [row()]

    monkeypatch.setattr(sources_repo, "list_graph_corpus", listed)

    class Session:
        began = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def begin(self):
            Session.began = True
            return self

    revisions = await PostgresCorpusSource(Session).snapshot(PROJECT, dry_run=True)

    assert seen == [uuid.UUID(PROJECT)] and Session.began
    assert len(revisions) == 1


# Runner ----------------------------------------------------------------------------


SETTINGS = GraphBuildSettings(
    LLM_GATEWAY_URL="https://gateway.example.test/openai",
    LLM_GATEWAY_API_KEY="gateway-key-3c6e",
    GRAPH_LLM_MODEL="main/model",
    GRAPH_SMALL_MODEL="small/model",
    GRAPH_EMBEDDING_MODEL="embed/model",
    GRAPH_EMBEDDING_DIM="8",
)


class StubUsage:
    calls = 0


class StubGateway:
    instances: ClassVar[list[StubGateway]] = []

    def __init__(self, _llm: Any) -> None:
        self.closed = False
        StubGateway.instances.append(self)

    def usage(self):
        class Snapshot:
            def total(self):
                return StubUsage()

        return Snapshot()

    async def aclose(self) -> None:
        self.closed = True


class StubBuild:
    """Stands in for ``run_build``: stores a record, waits for ``release``, then finishes it."""

    def __init__(self, *, fail: Exception | None = None, block: bool = False, result: dict | None = None) -> None:
        self.contexts: list[Any] = []
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()  # set once the record is stored
        if not block:
            self.release.set()
        self.fail = fail
        self.result = result or {}

    async def __call__(self, ctx) -> BuildResult:
        self.contexts.append(ctx)
        builds = ctx.builds
        build_id = ctx.build_id or uuid.uuid4().hex
        if ctx.build_id is None:
            await builds.create_build(
                BuildRecord(
                    build_id=build_id,
                    project_id=ctx.project_id,
                    mode="first",
                    options=BuildOptions(),
                    ontology_fingerprint="fp",
                    label_set=(),
                    embedding_model="embed/model",
                    started_at=datetime.now(UTC),
                )
            )
        self.waiting.set()
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        values: dict[str, Any] = {
            "project_id": ctx.project_id,
            "build_id": build_id,
            "mode": "first",
            "status": "succeeded",
            "settled": True,
            "spec_changes": SpecChanges(added=("REQ-1", "REQ-2"), modified=("CAP-1",)),
            "verification": VerificationSummary(ok=True),
            "started_at": datetime.now(UTC),
            "finished_at": datetime.now(UTC),
        }
        result = BuildResult(**(values | self.result))
        await builds.finish_build(result)
        return result


def make_runner(tmp_path, build, *, ensured: list | None = None, stores=None) -> GraphBuildRunner:
    async def ensure(*args):
        if ensured is not None:
            ensured.append(args)

    return GraphBuildRunner(
        SETTINGS,
        driver=object(),
        session_factory=lambda: None,
        database="neo4j",
        dependencies=RunnerDependencies(
            build=build,
            stores=stores or open_file_stores(tmp_path),
            ontology=object(),
            ensure=ensure,
            gateway_factory=StubGateway,
        ),
    )


async def finished(runner: GraphBuildRunner, project_id: str) -> None:
    await runner._runs[project_id].task


async def test_idle_then_one_task_per_project_then_succeeded(tmp_path):
    build = StubBuild(block=True)
    ensured: list = []
    runner = make_runner(tmp_path, build, ensured=ensured)
    assert (await runner.status(PROJECT)).status == "idle"

    first = await runner.start(PROJECT)
    await asyncio.wait_for(build.waiting.wait(), 5)
    second = await runner.start(PROJECT)

    assert first.status == second.status == "running"
    assert len(build.contexts) == 1
    running = await runner.status(PROJECT)
    latest = await build.contexts[0].builds.latest_build(PROJECT)
    assert (running.build_id, running.mode) == (latest.build_id, "first")
    build.release.set()
    await finished(runner, PROJECT)

    state = await runner.status(PROJECT)
    assert state.status == "succeeded" and state.settled and state.verification_ok
    assert state.spec_changes == {"added": 2, "modified": 1}
    assert len(ensured) == 1
    assert StubGateway.instances[-1].closed

    await runner.start(PROJECT)
    await finished(runner, PROJECT)
    assert len(build.contexts) == 2 and len(ensured) == 1  # ensure_schema once per process


async def test_projects_wait_for_a_free_slot(tmp_path, monkeypatch):
    build = StubBuild(block=True)
    runner = make_runner(tmp_path, build)

    await runner.start(PROJECT)
    await runner.start(OTHER)
    await asyncio.wait_for(build.waiting.wait(), 5)
    for _ in range(25):
        await asyncio.sleep(0)

    assert runner_module.MAX_CONCURRENT_BUILDS == 1
    assert [ctx.project_id for ctx in build.contexts] == [PROJECT]
    assert (await runner.status(OTHER)).status == "running"
    build.release.set()
    await finished(runner, PROJECT)
    await finished(runner, OTHER)
    assert [ctx.project_id for ctx in build.contexts] == [PROJECT, OTHER]


async def test_shutdown_cancels_and_the_next_start_resumes(tmp_path):
    stores = open_file_stores(tmp_path)
    build = StubBuild(block=True)
    runner = make_runner(tmp_path, build, stores=stores)
    await runner.start(PROJECT)
    await asyncio.wait_for(build.waiting.wait(), 5)
    interrupted_id = (await stores.builds.latest_build(PROJECT)).build_id

    await runner.aclose()

    assert runner._runs[PROJECT].task.cancelled()
    assert StubGateway.instances[-1].closed
    state = await runner.status(PROJECT)
    assert (state.status, state.error_code, state.build_id) == ("failed", "interrupted", interrupted_id)
    with pytest.raises(RuntimeError):
        await runner.start(PROJECT)

    resumed = StubBuild()
    restarted = make_runner(tmp_path, resumed, stores=stores)
    await restarted.start(PROJECT)
    await finished(restarted, PROJECT)
    assert resumed.contexts[0].build_id == interrupted_id
    assert (await restarted.status(PROJECT)).status == "succeeded"


@pytest.mark.parametrize(
    ("error", "code"),
    [(ConfigurationError("rebuild_required"), "rebuild_required"), (RuntimeError("secret text"), "unexpected_error")],
)
async def test_a_failure_becomes_failed_with_a_code(tmp_path, error, code):
    async def broken_ensure(*args):
        raise error

    runner = make_runner(tmp_path, StubBuild())
    runner._ensure = broken_ensure

    await runner.start(PROJECT)
    await finished(runner, PROJECT)

    state = await runner.status(PROJECT)
    assert (state.status, state.error_code) == ("failed", code)


async def test_a_new_build_retries_the_follow_ups_of_the_last_one(tmp_path):
    stores = open_file_stores(tmp_path)
    first = StubBuild(
        result={"verification": VerificationSummary(ok=False, follow_up_lineages=("doc:x",)), "settled": False}
    )
    runner = make_runner(tmp_path, first, stores=stores)
    await runner.start(PROJECT)
    await finished(runner, PROJECT)
    assert (await runner.status(PROJECT)).settled is False

    second = StubBuild()
    runner._build = second
    await runner.start(PROJECT)
    await finished(runner, PROJECT)

    assert first.contexts[0].retry_lineages == ()
    assert second.contexts[0].retry_lineages == ("doc:x",)


async def test_a_failed_step_shows_its_code_not_interrupted(tmp_path):
    runner = make_runner(tmp_path, StubBuild(fail=ConfigurationError("unfinished_build", build_id="b0")))

    await runner.start(PROJECT)
    await finished(runner, PROJECT)

    state = await runner.status(PROJECT)
    assert (state.status, state.error_code) == ("failed", "unfinished_build")


async def test_build_logs_carry_ids_and_counts_only(tmp_path, caplog):
    caplog.set_level("INFO", logger="api.graph_build.runner")
    runner = make_runner(tmp_path, StubBuild())

    await runner.start(PROJECT)
    await finished(runner, PROJECT)

    records = [record for record in caplog.records if record.name == "api.graph_build.runner"]
    assert [record.getMessage() for record in records] == ["api.graph_build.started", "api.graph_build.finished"]
    finished_fields = fields(records[-1])
    assert finished_fields["spec_changes"] == {"added": 2, "modified": 1}
    assert finished_fields["status"] == "succeeded"
    for record in records:
        assert_json_plain(record)
        assert "gateway-key" not in str(fields(record))
