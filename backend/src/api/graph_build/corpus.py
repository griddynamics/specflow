"""``CorpusSource`` over Postgres: a project's processed documents (``documents.markdown``).

- Lineage: a Drive document (``origin='connection'``) is ``drive:<connection_id>:<file id>``,
  through the Drive file (``connection_items``) that links it; when several Drive files link it,
  the one of its holder source wins, else the smallest key. Any other document (an upload, or a
  Drive document no Drive file links any more) is ``doc:<document_id>``. Each lineage keeps its
  newest document, so a changed Drive file is an update of the same graph document.
- ``revision_id`` is the content hash, ``version`` the number of documents in the lineage.
- ``path`` is ``<source name>/<file name>`` (the file name alone without a source), made unique
  with a `` (n)`` suffix; ``title`` is the first heading, else the file name without extension.
- Order: by ``path``.

Nothing is recorded, so ``dry_run`` changes nothing. Nothing here logs: names stay in the database.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from typing import Any

from db import sources as repo
from db.sources import CONNECTION_ORIGIN, GraphCorpusRow
from graph.build import CorpusRevision
from graph.build.corpus import first_heading


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
                revision_id=row.content_hash,
                version=version,
                path=_unique(path, taken),
                title=first_heading(row.markdown) or PurePosixPath(row.original_filename).stem or path,
                markdown=row.markdown,
            )
        )
    revisions.sort(key=lambda revision: revision.path)
    return revisions


class PostgresCorpusSource:
    """``CorpusSource`` of the API: one read transaction per snapshot."""

    def __init__(self, session_factory: Callable[[], Any]) -> None:
        self._session_factory = session_factory

    async def snapshot(self, project_id: str, *, dry_run: bool = False) -> list[CorpusRevision]:
        del dry_run  # nothing is recorded, so a dry run reads the same snapshot
        async with self._session_factory() as session, session.begin():
            rows = await repo.list_graph_corpus(session, uuid.UUID(project_id))
        return corpus_revisions(rows)
