"""In-memory stand-in for ``db.batches``, ``db.removal`` and ``db.source_files`` (no tests in here).

The API tests run on this model; the real SQL is covered by the live tiers
(``test_db_*_live.py``, ``test_specflow_api_sources_live.py``). The rules are the real
ones where they are pure: ``decide_outcome``, ``file_state``, ``_hand_over_target``,
the cursor codec and the group ranks. ``FakeBatches`` works over a ``FakeRepo``'s
documents (a batch file is a document held by its source).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, TypedDict, Unpack

import pytest

from api import source_views
from db import batches, removal, source_files
from db import repositories as repo
from db import sources as sources_repo
from db.batches import (
    AddOutcome,
    BatchEndedError,
    LockConflictError,
    SourceNotFoundError,
    decide_outcome,
)
from db.removal import Removal, _hand_over_target, _needs_reread
from db.source_files import (
    FILE_STATES,
    FilePage,
    FileRow,
    HolderRef,
    SourceSummary,
    encode_file_cursor,
    file_state,
    state_rank,
)
from db.sources import UPLOAD_STALE_AFTER

OPEN_FOR = timedelta(minutes=15)


class SourceOptions(TypedDict, total=False):
    """Keyword options of ``FakeBatches.add_source`` (defaults in the method)."""

    kind: str
    by: uuid.UUID | None
    name: str | None
    ended: bool
    expected: int | None
    age: timedelta
    suffix: int
    listing: bool
    failure_reason: str | None
    external_id: str | None


def now() -> datetime:
    return datetime.now(UTC)


class FakeBatches:
    """Sources of every kind, their items and the removal of a source, in memory."""

    def __init__(self, fake_repo: Any) -> None:
        self.repo = fake_repo
        self.sources: dict[uuid.UUID, SimpleNamespace] = {}
        self.items: list[SimpleNamespace] = []
        self.removed: dict[uuid.UUID, int] = {}
        self.conflicts = 0  # the next N add / end / start / remove calls raise LockConflictError
        self.raises: dict[str, list[Exception]] = {}  # function name -> exceptions, in order
        self.calls: list[str] = []
        self.write_lock = asyncio.Lock()  # a removal and a second one queue, like the row lock

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for module, names in (
            (batches, "start_batch end_batch legacy_day_batch add_batch_file"),
            (removal, "remove_source"),
            (source_files, "source_summaries source_summary file_page"),
        ):
            for name in names.split():
                monkeypatch.setattr(module, name, getattr(self, name))
        monkeypatch.setattr(source_views, "removed_files", self.removed_files)
        monkeypatch.setattr(sources_repo, "count_processed_documents", self.count_processed)

    # ---- helpers for tests -------------------------------------------------------
    def add_source(self, pid: uuid.UUID, **options: Unpack[SourceOptions]) -> SimpleNamespace:
        kind = options.get("kind", "upload")
        by = options.get("by")
        name = options.get("name")
        ended = options.get("ended", True)
        age = options.get("age", timedelta(0))
        external_id = options.get("external_id")
        source = SimpleNamespace(
            id=uuid.uuid4(),
            project_id=pid,
            kind=kind,
            name=name or ("Local files" if kind == "upload" else "Folder"),
            name_suffix=options.get("suffix", 1),
            connected_by=by or next(iter(self.repo.users.values())).id,
            created_at=now() - age,
            ended_at=(now() - age) if ended and kind == "upload" else None,
            expected=options.get("expected"),
            listing=options.get("listing", False),
            failure_reason=options.get("failure_reason"),
            external_id=external_id or str(uuid.uuid4()),
            state="importing" if kind == "gdrive" else "done",
        )
        self.sources[source.id] = source
        return source

    def add_item(
        self,
        source: SimpleNamespace,
        name: str,
        *,
        state: str = "done",
        reason: str | None = None,
        document: SimpleNamespace | None = None,
        category: str = "pdf",
    ) -> SimpleNamespace:
        """A fixed-state item (Drive-like), or one that follows ``document``."""

        item = SimpleNamespace(
            id=uuid.uuid4(),
            connection_id=source.id,
            project_id=source.project_id,
            external_id=document.content_hash if document else str(uuid.uuid4()),
            name=name,
            category=category,
            document_id=document.id if document else None,
            fixed=None if document else (state, reason),
            state="pending" if document else "processed",
            created_at=now(),
        )
        self.items.append(item)
        return item

    def hold(self, source: SimpleNamespace, document: SimpleNamespace, name: str) -> SimpleNamespace:
        document.source_id = source.id
        return self.add_item(source, name, document=document)

    def items_of(self, source_id: uuid.UUID) -> list[SimpleNamespace]:
        return [i for i in self.items if i.connection_id == source_id]

    def is_open(self, source: SimpleNamespace) -> bool:
        if source.kind != "upload" or source.ended_at is not None:
            return False
        last = max([source.created_at] + [i.created_at for i in self.items_of(source.id)])
        return now() - last < OPEN_FOR

    def hidden(self, source: SimpleNamespace) -> bool:
        return source.kind == "upload" and not self.is_open(source) and not self.items_of(source.id)

    def _failing(self, name: str) -> None:
        self.calls.append(name)
        if self.raises.get(name):
            raise self.raises[name].pop(0)
        if self.conflicts and name in ("add_batch_file", "end_batch", "start_batch", "remove_source"):
            self.conflicts -= 1
            raise LockConflictError("scripted")

    # ---- read model --------------------------------------------------------------
    def _state(self, item: SimpleNamespace) -> tuple[str, str | None]:
        if item.fixed is not None:
            return item.fixed
        doc = self.repo.documents.get(item.document_id)
        holds = doc is not None and doc.source_id == item.connection_id
        age = None if doc is None else now() - doc.updated_at
        state = file_state(
            source_failed=False,
            item_state=item.state,
            item_error_reason=None,
            holds=holds,
            doc_status=None if doc is None else doc.status,
            doc_error=None if doc is None else doc.error,
            doc_age=age,
        )
        return state.state, state.reason

    def _holder(self, item: SimpleNamespace) -> HolderRef | None:
        doc = self.repo.documents.get(item.document_id)
        holder = self.sources.get(doc.source_id) if doc is not None and doc.source_id else None
        if holder is None or holder.id == item.connection_id:
            return None
        return HolderRef(holder.id, holder.kind, holder.name, holder.name_suffix, holder.created_at)

    async def source_summaries(self, _session, project_id) -> list[SourceSummary]:
        mine = sorted(
            (s for s in self.sources.values() if s.project_id == project_id and not self.hidden(s)),
            key=lambda s: (s.created_at, str(s.id)),
        )
        return [self._summary(s) for s in mine]

    async def source_summary(self, _session, project_id, source_id) -> SourceSummary | None:
        source = self.sources.get(source_id)
        if source is None or source.project_id != project_id or self.hidden(source):
            return None
        return self._summary(source)

    def _summary(self, source: SimpleNamespace) -> SourceSummary:
        counts = dict.fromkeys(FILE_STATES, 0)
        items = self.items_of(source.id)
        for item in items:
            counts[self._state(item)[0]] += 1
        if self.is_open(source):
            counts["waiting"] += max((source.expected or 0) - len(items), 0)
        user = next((u for u in self.repo.users.values() if u.id == source.connected_by), None)
        return SourceSummary(
            id=source.id,
            kind=source.kind,
            name=source.name,
            name_suffix=source.name_suffix,
            added_by_id=source.connected_by,
            added_by_name=user.display_name if user else "someone",
            created_at=source.created_at,
            listing_finished=not source.listing,
            open=self.is_open(source),
            total=sum(counts.values()),
            counts=counts,
            failure_reason=source.failure_reason,
            folder_id=source.external_id if source.kind == "gdrive" else None,
            link="https://drive.google.com/x" if source.kind == "gdrive" else None,
        )

    async def file_page(self, _session, _project_id, source_id, cursor, limit=200) -> FilePage:
        rows = []
        for item in self.items_of(source_id):
            state, reason = self._state(item)
            rows.append((state_rank(state), item.name.lower(), item.id, item, state, reason))
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        if cursor is not None:
            rows = [r for r in rows if r[:3] > (cursor.rank, cursor.lower_name, cursor.id)]
        page = rows[:limit]
        files = tuple(
            FileRow(
                r[3].id,
                r[3].name,
                r[3].category,
                r[4],
                r[5],
                self._holder(r[3]) if r[4] == "already_in_project" else None,
            )
            for r in page
        )
        more = encode_file_cursor(page[-1][0], page[-1][1], page[-1][2]) if len(rows) > limit else None
        return FilePage(files, more)

    async def count_processed(self, _session, project_id) -> int:
        parsed = ("parsed", "ingested", "partially_ingested")
        return sum(1 for d in self.repo.documents.values() if d.project_id == project_id and d.status in parsed)

    async def removed_files(self, _session, project_id) -> int:
        return self.removed.get(project_id, 0)

    # ---- batches -----------------------------------------------------------------
    async def start_batch(self, _session, project_id, user_id, expected_files):
        self._failing("start_batch")
        for source in [s for s in self.sources.values() if s.project_id == project_id and self.hidden(s)]:
            del self.sources[source.id]
        minute = now().replace(second=0, microsecond=0)
        taken = [
            s.name_suffix
            for s in self.sources.values()
            if s.project_id == project_id and s.kind == "upload" and s.created_at >= minute
        ]
        return self.add_source(
            project_id, by=user_id, ended=False, expected=expected_files, suffix=1 + max(taken, default=0)
        )

    async def legacy_day_batch(self, _session, project_id, user_id, moment):
        self._failing("legacy_day_batch")
        external = f"legacy-{moment.astimezone(UTC):%Y%m%d}"
        for source in self.sources.values():
            if source.project_id == project_id and source.external_id == external:
                return source
        return self.add_source(project_id, by=user_id, external_id=external)

    async def end_batch(self, _session, project_id, batch_id):
        self._failing("end_batch")
        source = self.sources.get(batch_id)
        if source is None or source.project_id != project_id or source.kind != "upload":
            raise SourceNotFoundError
        if not self.items_of(batch_id):
            del self.sources[batch_id]
            return None
        source.ended_at = source.ended_at or now()
        return source

    async def add_batch_file(self, session, project_id, batch_id, *, file, user_id, allow_ended=False) -> AddOutcome:
        self._failing("add_batch_file")
        batch = self.sources.get(batch_id)
        if batch is None or batch.project_id != project_id:
            raise SourceNotFoundError
        if batch.kind != "upload" or (not allow_ended and not self.is_open(batch)):
            raise BatchEndedError
        content_hash, filename = file.content_hash, file.filename
        same = next((i for i in self.items_of(batch_id) if i.external_id == content_hash), None)
        if same is not None:
            return AddOutcome("duplicate", same, self.repo.documents.get(same.document_id), None, None)
        try:
            doc = await repo.insert_document(
                session,
                project_id=project_id,
                file=file,
                uploaded_by=user_id,
                source_id=batch_id,
            )
        except repo.DuplicateDocumentError as duplicate:
            return await self._existing(session, batch, duplicate.existing, filename)
        item = self.add_item(batch, filename, document=doc)
        await repo.mark_processing(session, project_id, doc.id)
        return AddOutcome("added", item, doc, None, doc.id)

    async def _existing(self, session, batch, doc, filename) -> AddOutcome:
        holder = self.sources.get(doc.source_id) if doc.source_id else None
        stale = doc.status in ("processing", "uploaded") and now() - doc.updated_at > UPLOAD_STALE_AFTER
        outcome = (
            "already_in_project"
            if holder is None
            else decide_outcome(
                holder_is_batch=holder.id == batch.id,
                holder_kind=holder.kind,
                doc_status=doc.status,
                doc_stale=stale,
            )
        )
        if outcome == "duplicate":
            item = next(i for i in self.items_of(batch.id) if i.document_id == doc.id)
            return AddOutcome("duplicate", item, doc, None, None)
        if outcome == "read_again":
            await repo.mark_processing(session, batch.project_id, doc.id, include_stale=True)
            self.items = [i for i in self.items if not (i.connection_id == holder.id and i.document_id == doc.id)]
            doc.source_id = batch.id
            item = self.add_item(batch, filename, document=doc)
            if not self.items_of(holder.id) and not self.is_open(holder):
                del self.sources[holder.id]
            return AddOutcome("read_again", item, doc, None, doc.id)
        item = self.add_item(batch, filename, document=doc)
        item.state = "duplicate"
        return AddOutcome("already_in_project", item, doc, holder, None)

    # ---- removal -----------------------------------------------------------------
    async def remove_source(self, _session, project_id, source_id) -> Removal:
        async with self.write_lock:
            self._failing("remove_source")
            await asyncio.sleep(0)  # a second request can arrive while the first one works
            source = self.sources.get(source_id)
            if source is None or source.project_id != project_id:
                raise SourceNotFoundError
            return self._remove(source)

    def _remove(self, source: SimpleNamespace) -> Removal:
        mine = self.items_of(source.id)
        held = [d for d in self.repo.documents.values() if d.source_id == source.id]
        handed, reparse, deleted, keys = [], [], [], []
        for doc in held:
            listers = [
                (i.connection_id, self.sources[i.connection_id].created_at)
                for i in self.items
                if i.document_id == doc.id and i.connection_id != source.id
            ]
            target = _hand_over_target(listers)
            if target is None:
                deleted.append(doc.id)
                keys.append(doc.original_key)
                continue
            doc.source_id = target
            handed.append(doc.id)
            if _needs_reread(doc.status, doc.media_type):
                reparse.append(doc.id)
                doc.status = "processing"
        for doc_id in deleted:
            del self.repo.documents[doc_id]
        self.items = [i for i in self.items if i.connection_id != source.id]
        del self.sources[source.id]
        self.removed[source.project_id] = self.removed.get(source.project_id, 0) + len(mine)
        return Removal(
            kind=source.kind,
            file_count=len(mine),
            handed_over=handed,
            reparse=reparse,
            sweep_document_ids=deleted,
            object_keys=sorted(keys),
            drive_import_open=source.kind == "gdrive",
        )
