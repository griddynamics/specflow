"""G9 (DESIGN 'Graph revision rule'): a re-parsed document is revised once, a legacy one never.

``revision_id`` is the content hash for ``parser`` NULL, ``pymupdf4llm`` and ``drive-convert`` and
``<hash>:<parser>`` for any other engine id, so the deploy rebuilds no existing graph.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from api.graph_build.corpus import LEGACY_PARSERS, corpus_revisions, revision_id
from db.sources import GraphCorpusRow, graph_corpus_statement
from file_parsers.engines.docling import engine_id
from graph.build.changes import change_set
from graph.build.models import DocumentRec
from temporal.sources.activities import CONVERT_PARSER

T0 = datetime(2026, 1, 1, tzinfo=UTC)
HASH = "a" * 64


def row(**values: Any) -> GraphCorpusRow:
    defaults: dict[str, Any] = {
        "document_id": uuid.uuid4(),
        "origin": "upload",
        "content_hash": HASH,
        "original_filename": "spec.pdf",
        "created_at": T0,
        "source_id": None,
        "holder_name": None,
        "drive_connection_id": None,
        "drive_external_id": None,
        "drive_source_name": None,
        "markdown": "# Spec\n\nText.",
        "parser": None,
    }
    return GraphCorpusRow(**(defaults | values))


def persisted(revisions) -> list[DocumentRec]:
    return [
        DocumentRec(document_id=r.lineage_id, revision_id=r.revision_id, version=r.version, path=r.path, title=r.title)
        for r in revisions
    ]


@pytest.mark.parametrize("parser", [None, "pymupdf4llm", "drive-convert"])
def test_a_legacy_parser_keeps_the_bare_content_hash(parser):
    assert revision_id(HASH, parser) == HASH
    assert corpus_revisions([row(parser=parser)])[0].revision_id == HASH


def test_a_new_engine_id_adds_itself_to_the_revision():
    assert revision_id(HASH, engine_id()) == f"{HASH}:{engine_id()}"
    assert corpus_revisions([row(parser=engine_id())])[0].revision_id == f"{HASH}:{engine_id()}"


def test_the_legacy_set_is_exactly_the_three_values_and_matches_the_drive_converter():
    assert frozenset({None, "pymupdf4llm", "drive-convert"}) == LEGACY_PARSERS
    assert CONVERT_PARSER in LEGACY_PARSERS


def test_the_corpus_query_selects_the_parser_column():
    assert "parser" in {column.key for column in graph_corpus_statement(uuid.uuid4()).selected_columns}


def test_g9_a_reparsed_document_is_revised_once_and_a_legacy_document_is_unchanged():
    """
    Scenario: the graph holds three documents built before Docling (parser NULL, pymupdf4llm,
    drive-convert). Then one of them is parsed again with the Docling engine (same row, same content
    hash, new parser) and a new upload arrives.

    Graph build        Plan (change_set)        persisted graph
        |  snapshot 1       |                       |
        |------------------>| all new               |
        |  persist -------------------------------->|
        |  snapshot 2 (one parser changed)          |
        |------------------>| compare revision_id --|
        |<- revised: reparsed; unchanged: 2 legacy; new: upload
        |  persist; snapshot 3 (nothing changes)    |
        |------------------>| unchanged: all four   |  (revised once)
    """

    legacy = [row(original_filename=f"{n}.pdf", parser=p) for n, p in (("a", None), ("b", "pymupdf4llm"))]
    drive = row(original_filename="c.md", parser="drive-convert", content_hash="c" * 64)
    reparsed = row(original_filename="r.pdf", content_hash="d" * 64, parser="pymupdf4llm")
    before = corpus_revisions([*legacy, drive, reparsed])
    graph = persisted(before)
    assert change_set(before, []).new == tuple(r.lineage_id for r in before)

    after = corpus_revisions([*legacy, drive, row(**{**reparsed.__dict__, "parser": engine_id()})])
    changes = change_set(after, graph)

    reparsed_lineage = f"doc:{reparsed.document_id}"
    assert changes.revised == (reparsed_lineage,)
    assert set(changes.unchanged) == {f"doc:{r.document_id}" for r in (*legacy, drive)}
    assert changes.new == () and changes.removed == ()

    again = change_set(after, persisted(after))  # the next build, same snapshot
    assert again.revised == () and set(again.unchanged) == {r.lineage_id for r in after}


def test_a_new_upload_parsed_by_docling_is_a_new_lineage_not_a_revision():
    old = corpus_revisions([row(parser="pymupdf4llm")])
    fresh = row(original_filename="new.docx", parser=engine_id(), content_hash="e" * 64)

    changes = change_set(corpus_revisions([row(document_id=uuid.UUID(old[0].lineage_id[4:])), fresh]), persisted(old))

    assert changes.new == (f"doc:{fresh.document_id}",) and changes.revised == ()
