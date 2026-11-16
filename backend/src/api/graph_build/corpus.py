"""``CorpusSource`` over Postgres: a project's processed documents (``documents.markdown``).

- Lineage: a Drive document (``origin='connection'``) is ``drive:<connection_id>:<file id>``,
  through the Drive file (``connection_items``) that links it; when several Drive files link it,
  the one of its holder source wins, else the smallest key. Any other document (an upload, or a
  Drive document no Drive file links any more) is ``doc:<document_id>``. Each lineage keeps its
  newest document, so a changed Drive file is an update of the same graph document.
- ``revision_id`` is the content hash for a document whose ``parser`` is ``NULL``, ``pymupdf4llm`` or
  ``drive-convert`` (``LEGACY_PARSERS``), else ``<content hash>:<parser>``: only a document parsed with a new
  engine id is revised (once), so a deploy rebuilds no existing graph (DESIGN 'Graph revision rule').
  ``version`` is the number of documents in the lineage.
- ``path`` is ``<source name>/<file name>`` (the file name alone without a source), made unique
  with a `` (n)`` suffix; ``title`` is the first heading, else the file name without extension.
- Order: by ``path``.
- Answers (docs/graph/gap/reconcile.md "Applying amendments", answers.md "Clarifications document"): when the gap
  module is on (``CorpusAnswers``, the gap runner), the snapshot's lineages are reconciled first (the gap runner
  calls ``graph.gap.reconcile_corpus`` with the revisions; a dry run stores nothing and asks no judge; FR-GREC-01),
  then every amendment the answers name is applied to its newest text (``graph.gap.corpus.apply_amendments``) and
  its ``revision_id`` becomes the effective revision id; the stored document is never changed (FR-GCOR-01,
  FR-GCOR-02). An amendment whose anchor line the newest text lacks is left out and logged
  (``api.graph_build.amendment_unanchored``, ids only). ``effective_document`` is the export of one lineage
  ("Download with answers", FR-GCOR-05). The generated clarifications document (title "Clarifications", path
  ``Specflow/Clarifications.md``, lineage ``graph.gap.corpus.clarifications_lineage``, revision id the hash of its
  text) is added whenever a clarification of placement ``document`` is applied or sent to a build (FR-GCOR-03,
  FR-GANS-06).

Only reconcile records (amendment rows, events), and not in a dry run. Logs carry ids and counts only: names and
text stay in the database.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Final, Protocol, runtime_checkable

from db import sources as repo
from db.sources import CONNECTION_ORIGIN, GraphCorpusRow
from graph.build import CorpusRevision
from graph.build.corpus import first_heading
from graph.gap.corpus import (
    CLARIFICATIONS_PATH,
    CLARIFICATIONS_TITLE,
    DocumentClarification,
    apply_amendments,
    clarifications_lineage,
    clarifications_markdown,
    effective_revision_id,
    text_hash,
)
from graph.gap.models import AmendmentRow
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.graph_build.corpus")

LEGACY_PARSERS: Final = frozenset({None, "pymupdf4llm", "drive-convert"})
"""``documents.parser`` values whose revision id stays the bare content hash (all rows before Docling)."""


def revision_id(content_hash: str, parser: str | None) -> str:
    """The graph revision of a document: the content hash, plus the engine id for a new engine."""

    return content_hash if parser in LEGACY_PARSERS else f"{content_hash}:{parser}"


def drive_lineage(connection_id: uuid.UUID, external_id: str) -> str:
    return f"drive:{connection_id}:{external_id}"


def document_lineage(document_id: uuid.UUID) -> str:
    return f"doc:{document_id}"


def _lineage(rows: Sequence[GraphCorpusRow]) -> tuple[str, str | None]:
    """Lineage id and source name of one document (its rows differ only in the Drive file)."""

    first = rows[0]
    drive = [
        (row, row.drive_connection_id, row.drive_external_id)
        for row in rows
        if row.origin == CONNECTION_ORIGIN and row.drive_connection_id is not None and row.drive_external_id is not None
    ]
    if not drive:
        return document_lineage(first.document_id), first.holder_name
    held = [item for item in drive if item[1] == item[0].source_id]
    pick, connection_id, external_id = min(held or drive, key=lambda item: (str(item[1]), item[2]))
    return drive_lineage(connection_id, external_id), pick.drive_source_name


def lineage_of(rows: Sequence[GraphCorpusRow]) -> str | None:
    """The graph lineage of one document's corpus rows (``graph_corpus_document``): ``drive:...`` for a Drive document,
    ``doc:<id>`` otherwise; None when the document is not in the corpus."""

    return _lineage(rows)[0] if rows else None


def _unique(path: str, taken: set[str]) -> str:
    candidate, number = path, 1
    pure = PurePosixPath(path)
    while candidate in taken:
        number += 1
        candidate = str(pure.with_name(f"{pure.stem} ({number}){pure.suffix}"))
    taken.add(candidate)
    return candidate


def corpus_revisions(rows: Sequence[GraphCorpusRow]) -> list[CorpusRevision]:
    """The corpus snapshot of ``list_graph_corpus`` rows (see the module docstring)."""

    by_document: dict[uuid.UUID, list[GraphCorpusRow]] = defaultdict(list)
    for row in rows:
        by_document[row.document_id].append(row)
    lineages: dict[str, list[tuple[GraphCorpusRow, str | None]]] = defaultdict(list)
    for document_rows in by_document.values():
        lineage_id, source_name = _lineage(document_rows)
        lineages[lineage_id].append((document_rows[0], source_name))
    current = []
    for lineage_id, members in lineages.items():
        newest, source_name = max(members, key=lambda member: (member[0].created_at, str(member[0].document_id)))
        filename = newest.original_filename.replace("/", "_") or newest.document_id.hex
        path = f"{source_name.replace('/', '_')}/{filename}" if source_name else filename
        current.append((path, lineage_id, newest, len(members)))
    taken: set[str] = set()
    revisions = []
    for path, lineage_id, row, version in sorted(current, key=lambda item: (item[0], item[1])):
        revisions.append(
            CorpusRevision(
                lineage_id=lineage_id,
                revision_id=revision_id(row.content_hash, row.parser),
                version=version,
                path=_unique(path, taken),
                title=first_heading(row.markdown) or PurePosixPath(row.original_filename).stem or path,
                markdown=row.markdown,
            )
        )
    revisions.sort(key=lambda revision: revision.path)
    return revisions


@dataclass(frozen=True)
class CorpusAnswersRead:
    """The answers a corpus read applies: the ``active`` amendments by lineage and the clarifications of the
    clarifications document."""

    amendments: Mapping[str, Sequence[AmendmentRow]] = field(default_factory=dict)
    clarifications: Sequence[DocumentClarification] = ()


@runtime_checkable
class CorpusAnswers(Protocol):
    """Where the corpus reader gets the answers (host: the gap runner, ``api.gap.runner.GapRunner``)."""

    async def corpus_answers(
        self, project_id: str, revisions: Sequence[CorpusRevision] = (), *, dry_run: bool = False
    ) -> CorpusAnswersRead: ...


def with_answers(
    project_id: str, revisions: Sequence[CorpusRevision], answers: CorpusAnswersRead
) -> list[CorpusRevision]:
    """``revisions`` with their active amendments applied and the clarifications document added (module
    docstring), in path order."""

    result: list[CorpusRevision] = []
    for revision in revisions:
        rows = answers.amendments.get(revision.lineage_id, ())
        if not rows:
            result.append(revision)
            continue
        applied = apply_amendments(revision.markdown, rows)
        for row in applied.unanchored:
            log_event(
                logger,
                WARNING,
                "api.graph_build.amendment_unanchored",
                project_id=project_id,
                lineage_id=revision.lineage_id,
                amendment_id=row.amendment_id,
                clr_id=row.clr_id,
            )
        result.append(
            revision.model_copy(
                update={
                    "markdown": applied.markdown,
                    "revision_id": effective_revision_id(revision.revision_id, applied.applied),
                }
            )
        )
    markdown = clarifications_markdown(answers.clarifications)
    if markdown:
        taken = {revision.path for revision in result}
        result.append(
            CorpusRevision(
                lineage_id=clarifications_lineage(project_id),
                revision_id=text_hash(markdown),
                version=1,
                path=_unique(CLARIFICATIONS_PATH, taken),
                title=CLARIFICATIONS_TITLE,
                markdown=markdown,
            )
        )
    result.sort(key=lambda revision: revision.path)
    return result


class PostgresCorpusSource:
    """``CorpusSource`` of the API: one read transaction per snapshot, then the answers (``answers``, when the gap
    module is on)."""

    def __init__(self, session_factory: Callable[[], Any], *, answers: CorpusAnswers | None = None) -> None:
        self._session_factory = session_factory
        self._answers = answers

    async def snapshot(self, project_id: str, *, dry_run: bool = False) -> list[CorpusRevision]:
        async with self._session_factory() as session, session.begin():
            rows = await repo.list_graph_corpus(session, uuid.UUID(project_id))
        revisions = corpus_revisions(rows)
        if self._answers is None:
            return revisions
        answers = await self._answers.corpus_answers(project_id, revisions, dry_run=dry_run)
        return with_answers(project_id, revisions, answers)


async def effective_document(
    rows: Sequence[GraphCorpusRow], lineage: str, answers: CorpusAnswers | None, project_id: str
) -> CorpusRevision | None:
    """The newest revision of ``lineage`` in the project's corpus rows with its answers applied, as a build reads it
    ("Download with answers", docs/graph/gap/reconcile.md; FR-GCOR-05); None when the lineage is not in the corpus.
    The snapshot is reconciled as a dry run: nothing is stored and no judge is asked, so an undecided answer reads as
    the build read it, and an upload no build has read yet shows the answers that still hold on it."""

    revisions = corpus_revisions(rows)
    revision = next((item for item in revisions if item.lineage_id == lineage), None)
    if revision is None or answers is None:
        return revision
    read = await answers.corpus_answers(project_id, revisions, dry_run=True)
    return with_answers(project_id, [revision], CorpusAnswersRead(amendments=read.amendments))[0]
