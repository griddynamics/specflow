"""The host's corpus reader with answers (docs/graph/gap/reconcile.md "Applying amendments", answers.md "Clarifications
document"; FR-GCOR-01..03): ``with_answers``, ``PostgresCorpusSource(answers=...)``, the gap runner's
``corpus_answers`` and the build runner's wiring."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest

from api.graph_build import corpus as corpus_module
from api.graph_build.corpus import CorpusAnswersRead, PostgresCorpusSource, corpus_revisions, with_answers
from api.graph_build.runner import GraphBuildRunner
from api.graph_build.runner import RunnerDependencies as BuildDependencies
from db.sources import GraphCorpusRow
from gap_host_support import PROJECT, SETTINGS, StubGateway, make_gap_runner
from gap_port_fakes import MemoryAmendments, MemoryGapRecords
from graph.build import CorpusRevision, open_file_stores
from graph.core.reader import GraphFragment
from graph.gap.corpus import DocumentClarification, clarifications_lineage
from graph.gap.models import AmendmentRow
from graph.gap.reconcile import SameMeaningAnswer

AT = datetime(2026, 1, 1, tzinfo=UTC)
DOCUMENT = uuid.UUID(int=7)
LINEAGE = f"doc:{DOCUMENT}"
TEXT = "# Spec\n\n## Rules\nCan it be cancelled?\nOther line.\n"


def _row(**values: Any) -> GraphCorpusRow:
    defaults: dict[str, Any] = {
        "document_id": DOCUMENT,
        "origin": "upload",
        "content_hash": "a" * 64,
        "original_filename": "spec.md",
        "created_at": AT,
        "source_id": None,
        "holder_name": None,
        "drive_connection_id": None,
        "drive_external_id": None,
        "drive_source_name": None,
        "markdown": TEXT,
        "parser": None,
    }
    return GraphCorpusRow(**(defaults | values))


def _amendment(anchor: str = "Can it be cancelled?", **values: Any) -> AmendmentRow:
    defaults: dict[str, Any] = {
        "amendment_id": "AMD::1",
        "clr_id": "CLR::1",
        "lineage": LINEAGE,
        "heading_path": ("Spec", "Rules"),
        "anchor_text": anchor,
        "anchor_hash": "0" * 64,
        "operation": "replace",
        "text": "It can be cancelled.\n\n> Clarified by GAP::1",
        "status": "active",
        "created_at": AT,
        "updated_at": AT,
    }
    return AmendmentRow.model_validate(defaults | values)


CLARIFICATION = DocumentClarification("GAP::2", "CLR::2", "Limits", "Which limit?", "500 EUR.", "2026-01-01")


def test_with_answers_applies_amendments_and_adds_the_clarifications_document() -> None:
    revisions = corpus_revisions([_row(), _row(document_id=uuid.UUID(int=8), original_filename="b.md")])

    read = with_answers(PROJECT, revisions, CorpusAnswersRead({LINEAGE: (_amendment(),)}, (CLARIFICATION,)))

    amended = next(item for item in read if item.lineage_id == LINEAGE)
    other = next(item for item in read if item.lineage_id == f"doc:{uuid.UUID(int=8)}")
    document = next(item for item in read if item.lineage_id == clarifications_lineage(PROJECT))
    assert "It can be cancelled." in amended.markdown and "Can it be cancelled?" not in amended.markdown
    assert amended.revision_id != revisions[0].revision_id
    assert other == next(item for item in revisions if item.lineage_id == other.lineage_id)
    assert (document.title, document.path, document.version) == ("Clarifications", "Specflow/Clarifications.md", 1)
    assert "## Limits (GAP::2)" in document.markdown
    assert [item.path for item in read] == sorted(item.path for item in read)
    assert with_answers(PROJECT, revisions, CorpusAnswersRead()) == list(revisions)


def test_an_amendment_without_its_anchor_is_left_out_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    revisions = corpus_revisions([_row()])

    with caplog.at_level(logging.WARNING):
        read = with_answers(PROJECT, revisions, CorpusAnswersRead({LINEAGE: (_amendment("Gone line."),)}))

    assert read == list(revisions)  # nothing applied: the revision id stays the stored one
    (record,) = [item for item in caplog.records if "amendment_unanchored" in item.getMessage()]
    assert "Gone line" not in record.getMessage()


class _Session:
    @asynccontextmanager
    async def begin(self) -> Any:
        yield self


class _Answers:
    def __init__(self, read: CorpusAnswersRead) -> None:
        self.read = read
        self.asked: list[str] = []
        self.dry_runs: list[tuple[int, bool]] = []

    async def corpus_answers(
        self, project_id: str, revisions: Sequence[CorpusRevision] = (), *, dry_run: bool = False
    ) -> CorpusAnswersRead:
        self.asked.append(project_id)
        self.dry_runs.append((len(revisions), dry_run))
        return self.read


@asynccontextmanager
async def _session() -> Any:
    yield _Session()


async def test_the_postgres_corpus_source_reads_answers_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rows(_session: Any, _project_id: uuid.UUID) -> list[GraphCorpusRow]:
        return [_row()]

    monkeypatch.setattr(corpus_module.repo, "list_graph_corpus", rows)
    answers = _Answers(CorpusAnswersRead({LINEAGE: (_amendment(),)}))

    plain = await PostgresCorpusSource(_session).snapshot(PROJECT)
    amended = await PostgresCorpusSource(_session, answers=answers).snapshot(PROJECT, dry_run=True)

    assert plain[0].markdown == TEXT and answers.asked == [PROJECT] and answers.dry_runs == [(1, True)]
    assert "It can be cancelled." in amended[0].markdown


async def test_the_gap_runner_hands_the_corpus_its_active_amendments_and_document_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    amendments = MemoryAmendments()
    await amendments.put(PROJECT, [_amendment(), _amendment(amendment_id="AMD::2", status="pending")])
    runner = make_gap_runner(tmp_path)
    runner._deps = dataclasses.replace(runner._deps, ports=lambda _transaction: ("records", amendments))
    seen: list[Any] = []

    async def clarifications(executor: Any, project_id: str, ontology: Any) -> tuple[DocumentClarification, ...]:
        seen.append((executor, project_id))
        return (CLARIFICATION,)

    monkeypatch.setattr("api.gap.runner.document_clarifications", clarifications)

    read = await runner.corpus_answers(PROJECT)

    assert {lineage: [row.amendment_id for row in rows] for lineage, rows in read.amendments.items()} == {
        LINEAGE: ["AMD::1"]
    }
    assert read.clarifications == (CLARIFICATION,) and seen == [("executor", PROJECT)]
    await runner.aclose()


def test_the_build_runner_reads_answers_through_the_gap_runner(tmp_path: Path) -> None:
    deps = BuildDependencies(stores=open_file_stores(tmp_path), ontology=object(), gateway_factory=StubGateway)
    gaps = make_gap_runner(tmp_path)

    wired = GraphBuildRunner(SETTINGS, store=object(), session_factory=lambda: None, dependencies=deps, gaps=gaps)
    alone = GraphBuildRunner(SETTINGS, store=object(), session_factory=lambda: None, dependencies=deps)

    assert wired._corpus._answers is gaps
    assert alone._corpus._answers is None


class _Fragments:
    def __init__(self, text: str) -> None:
        self.text = text

    async def fragments(self, _project_id: str, document_id: str) -> list[Any]:
        return [GraphFragment(fragment_id="f1", document_id=document_id, text=self.text, status="active")]


class _JudgeGateway:
    instances: ClassVar[list[_JudgeGateway]] = []

    def __init__(self, _llm: Any) -> None:
        self.closed = False
        self.calls = 0
        _JudgeGateway.instances.append(self)

    async def structured(self, *_args: Any, **_kwargs: Any) -> SameMeaningAnswer:
        self.calls += 1
        return SameMeaningAnswer(verdict="different", confidence=0.9)

    async def aclose(self) -> None:
        self.closed = True


async def _reconciling_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MemoryAmendments, Any]:
    amendments, records = MemoryAmendments(), MemoryGapRecords()
    await amendments.put(PROJECT, [_amendment(last_revision_id="old")])
    runner = make_gap_runner(tmp_path)
    previous = TEXT.replace("Can it be cancelled?", "It can be cancelled.\n\n> Clarified by GAP::1")
    runner._deps = dataclasses.replace(
        runner._deps,
        ports=lambda _transaction: (records, amendments),
        reader=lambda _store, _ontology: _Fragments(previous),
        gateway_factory=_JudgeGateway,
    )

    async def no_gaps(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
        return ()

    async def clarifications(*_args: Any) -> tuple[DocumentClarification, ...]:
        return ()

    monkeypatch.setattr("api.gap.runner.read_gaps", no_gaps)
    monkeypatch.setattr("api.gap.runner.document_clarifications", clarifications)
    _JudgeGateway.instances.clear()
    return runner, amendments, records


async def test_the_gap_runner_reconciles_a_new_revision_before_the_build_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, amendments, _ = await _reconciling_runner(tmp_path, monkeypatch)
    upload = corpus_revisions([_row(markdown=TEXT.replace("Can it be cancelled?", "It is never cancelled."))])

    dry = await runner.corpus_answers(PROJECT, upload, dry_run=True)
    assert _JudgeGateway.instances == [] and dry.amendments[LINEAGE][0].operation == "insert_after"
    assert (await amendments.for_clarification(PROJECT, "CLR::1"))[0].status == "active"

    read = await runner.corpus_answers(PROJECT, upload)

    (gateway,) = _JudgeGateway.instances
    assert gateway.calls == 1 and gateway.closed
    assert read.amendments == {LINEAGE: ()}  # the upload wins (GD3)
    assert (await amendments.for_clarification(PROJECT, "CLR::1"))[0].status == "suspended"
    await runner.aclose()


async def test_the_gap_runner_opens_no_judge_when_nothing_needs_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, amendments, _ = await _reconciling_runner(tmp_path, monkeypatch)

    read = await runner.corpus_answers(PROJECT, corpus_revisions([_row()]))

    assert _JudgeGateway.instances == []
    assert [row.status for row in read.amendments[LINEAGE]] == ["active"]
    assert (await amendments.for_clarification(PROJECT, "CLR::1"))[0].last_revision_id == "a" * 64
    await runner.aclose()
