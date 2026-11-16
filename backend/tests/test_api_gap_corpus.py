"""The host's corpus reader with answers (docs/graph/gap/reconcile.md "Applying amendments", answers.md "Clarifications
document"; FR-GCOR-01..03): ``with_answers``, ``PostgresCorpusSource(answers=...)``, the gap runner's
``corpus_answers`` and the build runner's wiring."""

from __future__ import annotations

import dataclasses
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from api.graph_build import corpus as corpus_module
from api.graph_build.corpus import CorpusAnswersRead, PostgresCorpusSource, corpus_revisions, with_answers
from api.graph_build.runner import GraphBuildRunner
from api.graph_build.runner import RunnerDependencies as BuildDependencies
from db.sources import GraphCorpusRow
from gap_host_support import PROJECT, SETTINGS, StubGateway, make_gap_runner
from gap_port_fakes import MemoryAmendments
from graph.build import open_file_stores
from graph.gap.corpus import DocumentClarification, clarifications_lineage
from graph.gap.models import AmendmentRow

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

    async def corpus_answers(self, project_id: str) -> CorpusAnswersRead:
        self.asked.append(project_id)
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

    assert plain[0].markdown == TEXT and answers.asked == [PROJECT]
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
