"""Acceptance tests of graph.gap: Corpus integration (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from api.graph_build.corpus import PostgresCorpusSource, effective_document
from db.sources import GraphCorpusRow
from gap_acceptance_support import Planted, corpus_text
from gap_answer_support import MEMBER, ORDERS_LINEAGE, answer_world
from gap_close_support import (
    PLACE_HEADING,
    PLACE_STORY,
    answer_and_apply,
    cancel_answered_and_built,
    corpus_answers,
    effective_corpus,
    option_text,
    plan_update,
    sections,
)
from graph.gap.corpus import clarifications_lineage
from graph.gap.gaps import clarification_uuid
from graph.gap.models import AnswerInput

ORDERS = "orders-spec.md"


def _orders(revisions):
    return next(revision for revision in revisions if revision.lineage_id == ORDERS_LINEAGE)


async def test_fr_gcor_01_corpus_reader_apply_active_amendment_lineage_newest() -> None:
    """Given an amendment on `orders-spec.md`, then the build reads the amended text and the stored document is
    byte-identical.
    """

    rig = await answer_world()
    gap = await rig.cancel_gap()
    stored = (Path(__file__).parent / "fixtures/graph/gap/corpus" / ORDERS).read_bytes()

    await answer_and_apply(rig, gap, option_rank=1)
    read = _orders(await effective_corpus(rig.world))

    assert option_text(gap, 1) in read.markdown and Planted.STATED_QUESTION_LINE not in read.markdown
    assert (Path(__file__).parent / "fixtures/graph/gap/corpus" / ORDERS).read_bytes() == stored


async def test_fr_gcor_02_effective_revision_id_hash_source_revision_id() -> None:
    """Given an unchanged document and unchanged amendments, then two corpus reads give the same effective revision
    id.
    """

    rig = await answer_world()
    before = _orders(await effective_corpus(rig.world))
    await answer_and_apply(rig, await rig.cancel_gap(), option_rank=1)

    first, second = _orders(await effective_corpus(rig.world)), _orders(await effective_corpus(rig.world))

    assert first.revision_id == second.revision_id
    assert first.revision_id != before.revision_id == "REV::ORDERS_SPEC::1"


async def test_fr_gcor_03_clarifications_document_generated_applied_pending_clarifications_placement() -> None:
    """Given one reverted and one applied clarification, then the document holds only the applied one's section."""

    rig = await answer_world()
    review, cancel = await rig.review_gap(), await rig.cancel_gap()
    await rig.service.answer_gap(rig.project_id, AnswerInput(gap_id=review.gap_id, option_rank=3, answered_by=MEMBER))
    dropped = await rig.service.answer_gap(
        rig.project_id,
        AnswerInput(gap_id=cancel.gap_id, own_text="Support decides.", placement="document", answered_by=MEMBER),
    )
    await rig.service.apply_now(rig.project_id, by=MEMBER)
    rig.world.executor.nodes[("Clarification", clarification_uuid(rig.project_id, dropped.clr_id))]["status"] = (
        "reverted"  # revert_clarification comes with advanced inference; its outcome is this status
    )

    (document,) = [
        item for item in await effective_corpus(rig.world) if item.lineage_id == clarifications_lineage(rig.project_id)
    ]

    assert [section.heading_path[-1] for section in sections(document.markdown)] == [
        f"{review.title} ({review.gap_id})"
    ]
    assert "Support decides." not in document.markdown


async def test_fr_gcor_04_after_apply_build_amended_fragments_re_extracted(tmp_path: Path) -> None:
    """Given one amended section, then the build's change report touches only that fragment's nodes and the plan
    re-plans only their stories.

    The update build is simulated (``gap_close_support.apply_build``): only the Place order section's text changed,
    so only its fragment is new; the plan runs ``update`` over that build's change report.
    """

    rig, _, _, rebuilt = await cancel_answered_and_built(1)
    planned = await plan_update(rig.world, rebuilt, tmp_path)

    fragment = rebuilt.fragment(ORDERS_LINEAGE, PLACE_HEADING)
    assert dict(rebuilt.report.fragments_changed) == {ORDERS_LINEAGE: (fragment,)}
    nodes = {node.spec_id: node for node in await rig.world.reader.nodes(rig.project_id)}
    touched = {*rebuilt.report.added, *rebuilt.report.changed_evidence}
    assert all(any(item.fragment_id == fragment for item in nodes[spec_id].provenance) for spec_id in touched)
    assert planned.stories == {PLACE_STORY}
    assert corpus_text(ORDERS).count(Planted.STATED_QUESTION_LINE) == 1


async def test_fr_gcor_05_download_answers_export_effective_text_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given an amended document, then the export equals the text the build read.

    The host's corpus reader and the export (``api.graph_build.corpus.effective_document``, the route's helper) read
    the same stored document and the world's applied answer."""

    rig = await answer_world()
    await answer_and_apply(rig, await rig.cancel_gap(), option_rank=1)
    document = uuid.UUID(int=11)
    rows = [
        GraphCorpusRow(
            document_id=document,
            origin="upload",
            content_hash="c" * 64,
            original_filename=ORDERS,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_id=None,
            holder_name=None,
            drive_connection_id=None,
            drive_external_id=None,
            drive_source_name=None,
            markdown=corpus_text(ORDERS),
            parser=None,
        )
    ]
    lineage = f"doc:{document}"
    stored = await rig.world.amendments.for_lineage(rig.project_id, ORDERS_LINEAGE)
    await rig.world.amendments.put(rig.project_id, [row.model_copy(update={"lineage": lineage}) for row in stored])

    class Answers:
        async def corpus_answers(self, project_id: str, revisions=(), *, dry_run: bool = False):
            del project_id  # the host's project id; the world keeps its own
            return await corpus_answers(rig.world, revisions, dry_run=dry_run)

    async def listed(_session: object, _project: uuid.UUID) -> list[GraphCorpusRow]:
        return rows

    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        class Session:
            @asynccontextmanager
            async def begin(self) -> AsyncIterator[None]:
                yield None

        yield Session()

    monkeypatch.setattr("api.graph_build.corpus.repo.list_graph_corpus", listed)
    host_project = str(uuid.UUID(int=12))
    (read,) = await PostgresCorpusSource(session, answers=Answers()).snapshot(host_project)
    exported = await effective_document(rows, lineage, Answers(), host_project)

    assert exported is not None and exported.markdown == read.markdown
    assert exported.revision_id == read.revision_id
    assert "> Clarified by " in exported.markdown and Planted.STATED_QUESTION_LINE not in exported.markdown
