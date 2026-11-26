"""The diagrams.net importer through the REAL ``ImportWriter`` over an offline stand-in of the database layer.

The stand-in keeps what the writer reads (the source lock, the items, the documents) and runs the same content-hash
dedupe as ``db.sources.insert_connection_document`` (one document per hash in a project), so a page that shared its
original with another would be dropped here as it is in Postgres. Objects go to the in-memory store.

Sequence::

    DrawioImporter -> writer.declare(page) -> writer.add(page) -> store.put(original) -> insert (hash dedupe) -> item
    removed source -> lock finds nothing -> SourceGoneError, the object of that write is deleted
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from api.imports import ImportJob, ImportWriter, SourceGoneError
from api.imports.drawio import DrawioImport, DrawioImporter
from db import imports as imports_db
from db import sources as sources_db
from file_parsers import diagrams
from storage.memory import InMemoryObjectStore

FIXTURES = Path(__file__).parent / "fixtures" / "drawio"
PROJECT, SOURCE, USER = uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3)


class FakeDatabase:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.importing = True
        self.hashes: dict[str, uuid.UUID] = {}
        self.documents: list[dict[str, Any]] = []
        self.items: dict[str, str] = {}
        self.gone_after: int | None = None
        monkeypatch.setattr(sources_db, "lock_importing_connection", self.lock)
        monkeypatch.setattr(imports_db, "declare_item", self.declare)
        monkeypatch.setattr(sources_db, "insert_connection_document", self.insert)
        monkeypatch.setattr(sources_db, "finish_item", self.finish)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        yield object()

    async def lock(self, _session: object, _project: uuid.UUID, _source: uuid.UUID) -> object | None:
        if self.gone_after is not None and len(self.documents) >= self.gone_after:
            self.importing = False
        return SimpleNamespace(id=SOURCE) if self.importing else None

    async def declare(self, _session: object, _project: uuid.UUID, _source: uuid.UUID, **item: Any) -> uuid.UUID:
        self.items.setdefault(item["external_id"], "pending")
        return uuid.uuid5(SOURCE, item["external_id"])

    async def insert(self, _session: object, *, file: Any, **values: Any) -> tuple[SimpleNamespace, bool]:
        known = self.hashes.get(file.content_hash)
        if known is not None:
            return SimpleNamespace(id=known), False
        self.hashes[file.content_hash] = file.document_id
        self.documents.append({"id": file.document_id, "name": file.filename, "hash": file.content_hash, **values})
        return SimpleNamespace(id=file.document_id), True

    async def finish(self, _session: object, item_id: uuid.UUID, state: str, **_: Any) -> bool:
        self.items[str(item_id)] = state
        return True


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> FakeDatabase:
    return FakeDatabase(monkeypatch)


def writer_of(database: FakeDatabase, store: InMemoryObjectStore) -> ImportWriter:
    job = ImportJob(PROJECT, SOURCE, USER, "drawio")
    return ImportWriter(job, transaction=database.transaction, store=store)


async def test_the_pages_of_a_diagram_are_one_document_each_and_none_is_deduped(database) -> None:
    store = InMemoryObjectStore(app_env="local")
    found = diagrams.decode((FIXTURES / "structure.drawio").read_bytes(), hint="structure.drawio")
    job = ImportJob(PROJECT, SOURCE, USER, "drawio")

    await DrawioImporter(DrawioImport(found))(job, writer_of(database, store))

    assert len(found.pages) == 2
    assert len(database.documents) == len(found.pages) == len({d["hash"] for d in database.documents})
    assert [d["media_type"] for d in database.documents] == ["application/vnd.jgraph.mxfile"] * 2
    assert all(d["parser"] == "drawio-convert" and d["status"] == "parsed" for d in database.documents)
    assert [d["markdown"].splitlines()[0] for d in database.documents] == [
        f"# diagrams.net diagram · {page.name}" for page in found.pages
    ]


async def test_the_stand_in_dedupes_equal_originals_so_the_test_above_proves_something(database) -> None:
    store = InMemoryObjectStore(app_env="local")
    writer = writer_of(database, store)

    assert await writer.add("a", name="A", original=b"same", markdown="# A") == "added"
    assert await writer.add("b", name="B", original=b"same", markdown="# B") == "duplicate"
    assert len(database.documents) == 1


async def test_a_source_removed_during_the_import_stops_it_and_leaves_no_object_of_the_refused_write(database) -> None:
    store = InMemoryObjectStore(app_env="local")
    found = diagrams.decode((FIXTURES / "structure.drawio").read_bytes(), hint="structure.drawio")
    job = ImportJob(PROJECT, SOURCE, USER, "drawio")
    database.gone_after = 1  # the source is removed once the first page is stored

    with pytest.raises(SourceGoneError):
        await DrawioImporter(DrawioImport(found))(job, writer_of(database, store))

    assert len(database.documents) == 1
    assert len(store.objects) == 1  # the object of the refused second write was deleted
