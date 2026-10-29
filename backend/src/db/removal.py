"""Remove a source: all or nothing, with hand-over (FR-SRC-0016/0017, NFR-REL-0003).

``remove_source`` is the database half of SPECS section 5. It runs in the caller's
transaction, flushes and never commits, and logs nothing; the router starts the cleanup
workflow before the commit and deletes the objects, terminates the Drive import and
starts the re-reads after it, using the ``Removal`` it returns.

Lock order, the same as ``db.batches.add_batch_file`` (R11):
``projects`` row ``FOR NO KEY UPDATE`` (R06) -> connection rows in id order (the source
and every source that lists one of its documents) -> document rows. A deadlock or a
foreign-key race gives ``LockConflictError`` (the router retries once); a lock wait over 20 s
gives ``LockTimeoutError``; a delete blocked by ``previous_revision_id`` gives
``RevisionChainError`` and is never retried (R14).

No cascades (C11): a document that still has a lister is never deleted. It goes to the
oldest lister (the hand-over), and is read again only when it is not Done (SD7).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import any_, bindparam, delete, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.operators import ColumnOperators

from db import repositories as repo
from db.batches import LockConflictError, LockTimeoutError, SourceNotFoundError, conflict_from, sqlstate
from db.models import Connection, ConnectionItem, Document, Project
from db.source_repair import LOCK_PROJECT, adopt_unheld
from storage import keys
from temporal.ids import drive_document_id

LOCK_TIMEOUT: Final = "20s"
_LOCK_NOT_AVAILABLE: Final = "55P03"
_FOREIGN_KEY_VIOLATION: Final = "23503"
_LOCK_ROUNDS: Final = 5
DONE_STATUSES: Final = frozenset({"parsed", "ingested", "partially_ingested"})
# Items whose uuid5 document id may own objects (Drive sources only).
_OBJECT_STATES: Final = ("stored", "processing")


class RevisionChainError(Exception):
    """Deleting a document hit the ``previous_revision_id`` self-FK (R14); not retried."""


@dataclass(frozen=True)
class Removal:
    """What the router needs after the commit (SPECS 3.4 and 5)."""

    kind: str
    file_count: int
    handed_over: list[uuid.UUID]
    reparse: list[uuid.UUID]
    sweep_document_ids: list[uuid.UUID]
    object_keys: list[str]
    drive_import_open: bool


# Pure helpers (offline tests) --------------------------------------------------------


def _hand_over_target(listers: Iterable[tuple[uuid.UUID, datetime]]) -> uuid.UUID | None:
    """The source a document goes to: the lister with the smallest ``(created_at, id)``.

    ``listers`` are ``(connection_id, connection.created_at)``; ``None`` when empty.
    """

    ordered = sorted(listers, key=lambda pair: (pair[1], pair[0]))
    return ordered[0][0] if ordered else None


def _needs_reread(status: str, media_type: str) -> bool:
    """SD7: a handed-over PDF is read again only when it is not Done."""

    return status not in DONE_STATUSES and media_type == repo.PDF_MEDIA_TYPE


def _item_document_ids(
    source_id: uuid.UUID, items: Iterable[tuple[str, str]], states: Collection[str]
) -> list[uuid.UUID]:
    """uuid5 document ids of the Drive items in ``states``; ``items`` are
    ``(external_id, state)``. Order and duplicates of the input are kept out."""

    seen: dict[uuid.UUID, None] = {}
    for external_id, state in items:
        if state in states:
            seen[drive_document_id(source_id, external_id)] = None
    return list(seen)


def _object_keys_for(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    kind: str,
    items: Sequence[tuple[str, str]],
    deleted_original_keys: Iterable[str],
    protected: Collection[uuid.UUID],
) -> list[str]:
    """The object keys to delete after the commit (SPECS 5, key guard R01).

    - the ``original_key`` of every deleted document;
    - ``original.bin`` for a ``stored`` item (Drive only);
    - all three variants for a ``processing`` item (Drive only): its child parse may have
      written any of them.

    A uuid5 id in ``protected`` (handed over, or still with a ``documents`` row after the
    deletes) contributes no key: a processing PDF item's document may have just been
    handed over, and its ``original.pdf`` must survive. Sorted, without duplicates.
    """

    found: set[str] = set(deleted_original_keys)
    if kind == "gdrive":
        pid = str(project_id)
        for external_id, state in items:
            if state not in _OBJECT_STATES:
                continue
            doc_id = drive_document_id(source_id, external_id)
            if doc_id in protected:
                continue
            did = str(doc_id)
            found.add(keys.document_original_bin(pid, did))
            if state == "processing":
                found.add(keys.document_original(pid, did))
                found.add(keys.document_original_json(pid, did))
    return sorted(found)


def _sweep_ids_for(
    source_id: uuid.UUID,
    *,
    kind: str,
    items: Sequence[tuple[str, str]],
    deleted_ids: Iterable[uuid.UUID],
) -> list[uuid.UUID]:
    """Deleted document ids plus the uuid5 ids of ``stored`` and ``processing`` items
    (the sweep re-checks "no document row", so a handed-over id is always kept)."""

    found: dict[uuid.UUID, None] = dict.fromkeys(deleted_ids)
    if kind == "gdrive":
        for doc_id in _item_document_ids(source_id, items, _OBJECT_STATES):
            found[doc_id] = None
    return list(found)


# Queries -----------------------------------------------------------------------------


def _any(column: ColumnOperators, ids: Collection[uuid.UUID]) -> ColumnElement[bool]:
    """``column = ANY(:ids)``: one bind however many ids (no 32k parameter limit)."""

    return column == any_(bindparam("ids", list(ids), type_=ARRAY(PG_UUID(as_uuid=True))))


async def _listers(
    session: AsyncSession, project_id: uuid.UUID, source_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID, datetime]]:
    """``(document_id, lister connection id, lister created_at)`` for every item of
    another source that points to a document this source holds."""

    rows = await session.execute(
        select(Document.id, Connection.id, Connection.created_at)
        .select_from(ConnectionItem)
        .join(Document, Document.id == ConnectionItem.document_id)
        .join(Connection, Connection.id == ConnectionItem.connection_id)
        .where(
            Document.project_id == project_id,
            Document.source_id == source_id,
            ConnectionItem.connection_id != source_id,
        )
    )
    return list(rows.tuples().all())


async def _lock_sources(
    session: AsyncSession, project_id: uuid.UUID, source_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID, datetime]]:
    """Lock the source and every lister in id order; return the final lister rows."""

    locked: set[uuid.UUID] = set()
    for _ in range(_LOCK_ROUNDS):
        listers = await _listers(session, project_id, source_id)
        want = {source_id} | {conn for _, conn, _ in listers}
        got = set(
            await session.scalars(
                select(Connection.id)
                .where(Connection.project_id == project_id, _any(Connection.id, want))
                .order_by(Connection.id)
                .with_for_update()
            )
        )
        if source_id not in got:
            raise SourceNotFoundError  # removed by a concurrent call while we waited
        locked |= got
        listers = await _listers(session, project_id, source_id)
        if {conn for _, conn, _ in listers} <= locked:
            return listers
    raise LockConflictError("the listers of the source kept changing")


# remove_source -----------------------------------------------------------------------


async def remove_source(session: AsyncSession, project_id: uuid.UUID, source_id: uuid.UUID) -> Removal:
    """Delete a source with its items, hand over what others list, count the files.

    Raises ``SourceNotFoundError``, ``LockTimeoutError``, ``LockConflictError`` and ``RevisionChainError``.
    The caller rolls back on any of them (nothing is half done).
    """

    try:
        return await _remove(session, project_id, source_id)
    except DBAPIError as exc:
        code = sqlstate(exc)
        if code == _LOCK_NOT_AVAILABLE:
            raise LockTimeoutError("lock wait over the limit") from exc
        conflict = conflict_from(exc)
        if conflict is None and code == _FOREIGN_KEY_VIOLATION:
            if "previous_revision" in str(exc.orig):
                raise RevisionChainError("a document is the previous revision of another") from exc
            conflict = LockConflictError("a concurrent write referenced a deleted row")
        raise (conflict or exc) from exc


async def _remove(session: AsyncSession, project_id: uuid.UUID, source_id: uuid.UUID) -> Removal:
    await session.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
    await session.execute(text(LOCK_PROJECT), {"pid": project_id})
    await adopt_unheld(session, project_id)

    source = await session.scalar(
        select(Connection).where(Connection.project_id == project_id, Connection.id == source_id)
    )
    if source is None:
        raise SourceNotFoundError
    kind = source.kind

    listers = await _lock_sources(session, project_id, source_id)
    # the state is read with the source locked: a sync that moved it meanwhile must count
    state = await session.scalar(
        select(Connection.state).where(Connection.id == source_id).execution_options(populate_existing=True)
    )
    import_open = kind == "gdrive" and state in ("listing", "importing")
    held = (
        await session.execute(
            select(Document.id, Document.status, Document.media_type, Document.original_key)
            .where(Document.project_id == project_id, Document.source_id == source_id)
            .order_by(Document.id)
            .with_for_update()
        )
    ).all()
    items = [
        (external_id, state)
        for external_id, state in (
            await session.execute(
                select(ConnectionItem.external_id, ConnectionItem.state).where(
                    ConnectionItem.connection_id == source_id
                )
            )
        ).all()
    ]

    by_document: dict[uuid.UUID, list[tuple[uuid.UUID, datetime]]] = defaultdict(list)
    for document_id, connection_id, created_at in listers:
        by_document[document_id].append((connection_id, created_at))

    handed: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)  # target -> documents
    reparse: list[uuid.UUID] = []
    deleted_ids: list[uuid.UUID] = []
    deleted_keys: list[str] = []
    for document_id, status, media_type, original_key in held:
        target = _hand_over_target(by_document.get(document_id, ()))
        if target is None:
            deleted_ids.append(document_id)
            deleted_keys.append(original_key)
            continue
        handed[target].append(document_id)
        if _needs_reread(status, media_type):
            reparse.append(document_id)
    for target, ids in handed.items():
        await session.execute(
            update(Document).where(Document.project_id == project_id, _any(Document.id, ids)).values(source_id=target)
        )
    for document_id in reparse:  # uploaded/failed -> processing; processing stays (SD7)
        await repo.mark_processing(session, project_id, document_id, include_stale=True)

    # items -> documents -> connection (no cascades, C11)
    await session.execute(delete(ConnectionItem).where(ConnectionItem.connection_id == source_id))
    if deleted_ids:
        await session.execute(delete(Document).where(Document.project_id == project_id, _any(Document.id, deleted_ids)))
    await session.execute(delete(Connection).where(Connection.id == source_id))
    await session.execute(
        update(Project)
        .where(Project.id == project_id)
        # updated_at is named so that its onupdate default does not touch the row (the old text() UPDATE did not)
        .values(removed_files=Project.removed_files + len(items), updated_at=Project.updated_at)
    )

    handed_over = [doc for ids in handed.values() for doc in ids]
    candidates = _item_document_ids(source_id, items, _OBJECT_STATES) if kind == "gdrive" else []
    present = (
        set(await session.scalars(select(Document.id).where(_any(Document.id, candidates)))) if candidates else set()
    )
    return Removal(
        kind=kind,
        file_count=len(items),
        handed_over=handed_over,
        reparse=reparse,
        sweep_document_ids=_sweep_ids_for(source_id, kind=kind, items=items, deleted_ids=deleted_ids),
        object_keys=_object_keys_for(
            project_id,
            source_id,
            kind=kind,
            items=items,
            deleted_original_keys=deleted_keys,
            protected=set(handed_over) | present,
        ),
        drive_import_open=import_open,
    )
