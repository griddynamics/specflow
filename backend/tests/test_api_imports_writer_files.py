"""``ImportWriter.add_file`` and ``skip`` (docs/box/source.md, decision B12) over the in-memory object store and a
recorder of the database calls: the original is streamed to the store while hashed, the document is inserted
``processing`` with no parser, the item ends ``processed`` or ``duplicate``, the parse starts once per added file and
never for a duplicate, an overflow or a gone source leaves no object, and a skipped item ends ``skipped``.
The row-level behaviour against Postgres is covered by ``test_api_imports_live``.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from api.imports import ImportJob, ImportWriter, OriginalTooLargeError, SourceGoneError
from db import imports as imports_db
from db import sources as sources_db
from storage.memory import InMemoryObjectStore

JOB = ImportJob(uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3), "box")
PDF = b"%PDF-1.7 " + b"x" * 100


class Db:
    def __init__(self) -> None:
        self.documents: dict[str, SimpleNamespace] = {}
        self.inserted: list[dict[str, Any]] = []
        self.finished: list[tuple[str, dict[str, Any]]] = []
        self.declared: list[dict[str, Any]] = []
        self.gone = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def lock(_session: Any, _project: uuid.UUID, _source: uuid.UUID) -> object | None:
            return None if self.gone else object()

        async def declare(_session: Any, _project: uuid.UUID, _source: uuid.UUID, **values: Any) -> uuid.UUID:
            self.declared.append(values)
            return uuid.UUID(int=9)

        async def insert(_session: Any, **values: Any) -> tuple[SimpleNamespace, bool]:
            file = values["file"]
            existing = self.documents.get(file.content_hash)
            if existing is not None:
                return existing, False
            self.inserted.append(values)
            row = SimpleNamespace(id=file.document_id)
            self.documents[file.content_hash] = row
            return row, True

        async def finish(_session: Any, _item: uuid.UUID, state: str, **values: Any) -> bool:
            self.finished.append((state, values))
            return True

        monkeypatch.setattr(sources_db, "lock_importing_connection", lock)
        monkeypatch.setattr(imports_db, "declare_item", declare)
        monkeypatch.setattr(sources_db, "insert_connection_document", insert)
        monkeypatch.setattr(sources_db, "finish_item", finish)


@asynccontextmanager
async def transaction():
    yield None


async def chunks(*pieces: bytes):
    for piece in pieces:
        yield piece


class Rig:
    def __init__(self, db: Db) -> None:
        self.db = db
        self.store = InMemoryObjectStore(app_env="local")
        self.started: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.writer = ImportWriter(JOB, transaction=transaction, store=self.store, start_parse=self.start_parse)

    async def start_parse(self, project_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        self.started.append((project_id, document_id))
        return True

    @property
    def objects(self) -> list[str]:
        return sorted(self.store.objects)


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    db = Db()
    db.install(monkeypatch)
    return Rig(db)


async def add(
    rig: Rig, external_id: str = "file:1", data: bytes = PDF, *, max_bytes: int = 10_000, media="application/pdf"
):
    half = len(data) // 2
    return await rig.writer.add_file(
        external_id, name="Docs/a.pdf", chunks=chunks(data[:half], data[half:]), media_type=media, max_bytes=max_bytes
    )


async def test_a_file_is_streamed_hashed_inserted_processing_without_a_parser_and_its_parse_started(rig) -> None:
    assert await add(rig) == "added"

    (inserted,) = rig.db.inserted
    file = inserted["file"]
    assert file.content_hash == hashlib.sha256(PDF).hexdigest() and file.size_bytes == len(PDF)
    assert (inserted["status"], inserted["parser"], inserted["markdown"]) == ("processing", None, None)
    assert inserted["media_type"] == "application/pdf" and inserted["source_id"] == JOB.source_id
    assert file.original_key.endswith("/original.pdf") and file.filename == "Docs/a.pdf"
    assert file.original_key in rig.objects
    assert rig.db.finished == [("processed", {"document_id": file.document_id})]
    assert rig.started == [(JOB.project_id, file.document_id)]
    assert rig.db.declared[0]["external_id"] == "file:1" and rig.db.declared[0]["mime_type"] == "application/pdf"


async def test_markdown_text_is_stored_with_the_markdown_extension(rig) -> None:
    await add(rig, data=b"# title", media="text/markdown")

    assert rig.db.inserted[0]["file"].original_key.endswith("/original.md")


async def test_the_same_bytes_again_are_a_duplicate_the_object_is_dropped_and_no_parse_starts(rig) -> None:
    await add(rig, "file:1")
    first = rig.db.inserted[0]["file"]

    assert await add(rig, "file:2") == "duplicate"

    assert len(rig.db.inserted) == 1 and len(rig.started) == 1  # the parse started once, for the added file
    assert rig.db.finished[-1] == ("duplicate", {"document_id": first.document_id})
    assert rig.objects == [first.original_key]  # the second object is gone


async def test_bytes_past_the_cap_raise_and_nothing_is_stored(rig) -> None:
    with pytest.raises(OriginalTooLargeError):
        await add(rig, max_bytes=10)

    assert rig.db.inserted == [] and rig.db.finished == [] and rig.started == []
    assert rig.objects == []


async def test_a_stream_that_breaks_aborts_the_object(rig) -> None:
    async def broken():
        yield b"abc"
        raise ConnectionError

    with pytest.raises(ConnectionError):
        await rig.writer.add_file("file:1", name="a.pdf", chunks=broken(), media_type="application/pdf", max_bytes=100)

    assert rig.db.inserted == [] and rig.objects == []


async def test_a_source_that_is_gone_drops_the_object_and_raises(rig) -> None:
    rig.db.gone = True

    with pytest.raises(SourceGoneError):
        await add(rig)

    assert rig.started == [] and rig.objects == []


async def test_without_a_parse_starter_the_document_is_still_stored(monkeypatch) -> None:
    db = Db()
    db.install(monkeypatch)
    writer = ImportWriter(JOB, transaction=transaction, store=InMemoryObjectStore(app_env="local"))

    assert (
        await writer.add_file("file:1", name="a.pdf", chunks=chunks(PDF), media_type="application/pdf", max_bytes=1000)
        == "added"
    )


async def test_a_skipped_item_ends_skipped(rig) -> None:
    await rig.writer.skip("file:7", "pic.png")

    assert rig.db.finished == [("skipped", {})] and rig.db.declared[0]["name"] == "pic.png"
    assert rig.db.inserted == []


async def test_a_skip_in_a_source_that_is_gone_raises(rig) -> None:
    rig.db.gone = True

    with pytest.raises(SourceGoneError):
        await rig.writer.skip("file:7", "pic.png")


async def test_a_failing_insert_drops_the_stored_object_and_raises(rig, monkeypatch) -> None:
    async def broken(_session: Any, **_values: Any) -> tuple[SimpleNamespace, bool]:
        raise RuntimeError("insert failed")

    monkeypatch.setattr(sources_db, "insert_connection_document", broken)

    with pytest.raises(RuntimeError):
        await add(rig)

    assert rig.started == [] and rig.objects == []
