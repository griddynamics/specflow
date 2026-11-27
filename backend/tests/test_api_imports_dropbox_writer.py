"""The re-add skip of the Dropbox importer (decision DB12): ``db.imports.find_stored_document`` (one query), the size
and modified time ``ImportWriter.declare`` keeps on an item, and ``ImportWriter.link_stored``, which ends an item
``duplicate`` linking the document an earlier import stored, without a download. The statements are checked against a
recorder; the row-level behaviour against Postgres is covered by ``test_api_imports_live``."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from api.imports import ImportJob, ImportWriter, SourceGoneError
from db import imports as imports_db
from db import sources as sources_db
from db_recorder_support import Recorder
from storage.memory import InMemoryObjectStore

JOB = ImportJob(uuid.UUID(int=1), uuid.UUID(int=2), uuid.UUID(int=3), "dropbox")
MODIFIED = datetime(2026, 11, 20, 10, 0, 1, tzinfo=UTC)
DOCUMENT = uuid.UUID(int=42)


async def test_the_re_add_skip_is_one_query_by_id_modified_time_and_size_of_a_live_document() -> None:
    fake = Recorder([DOCUMENT])

    found = await imports_db.find_stored_document(
        fake, JOB.project_id, external_id="id:1", modified_time=MODIFIED, size_bytes=100
    )

    assert found == DOCUMENT and len(fake.sql) == 1
    sql = fake.sql[0]
    assert "connection_items.project_id" in sql and "connection_items.external_id" in sql
    assert "connection_items.modified_time" in sql and "connection_items.size_bytes" in sql
    assert "connection_items.state IN" in sql and "JOIN documents" in sql  # an item that links a document that exists
    assert "documents.status !=" in sql and "documents.project_id" in sql and "LIMIT" in sql
    assert "JOIN connections" in sql and "connections.kind" in sql  # only items of Dropbox sources


async def test_no_matching_item_is_none() -> None:
    fake = Recorder([])

    assert (
        await imports_db.find_stored_document(
            fake, JOB.project_id, external_id="id:1", modified_time=MODIFIED, size_bytes=1
        )
        is None
    )


async def test_an_item_is_declared_with_the_listed_size_and_modified_time() -> None:
    fake = Recorder([uuid.UUID(int=9)])

    await imports_db.declare_item(
        fake,
        JOB.project_id,
        JOB.source_id,
        external_id="id:1",
        name="a.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        modified_time=MODIFIED,
    )

    assert "size_bytes" in fake.sql[0] and "modified_time" in fake.sql[0]


class Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, document: uuid.UUID | None = DOCUMENT) -> None:
        self.declared: list[dict[str, Any]] = []
        self.finished: list[tuple[str, dict[str, Any]]] = []
        self.asked: list[dict[str, Any]] = []
        self.gone = False
        self.vanished = False
        self.document = document

        async def lock(_session: Any, _project: uuid.UUID, _source: uuid.UUID) -> object | None:
            return None if self.gone else object()

        async def declare(_session: Any, _project: uuid.UUID, _source: uuid.UUID, **values: Any) -> uuid.UUID:
            self.declared.append(values)
            return uuid.UUID(int=9)

        async def find(_session: Any, _project: uuid.UUID, **values: Any) -> uuid.UUID | None:
            self.asked.append(values)
            return self.document

        async def finish(_session: Any, _item: uuid.UUID, state: str, **values: Any) -> bool:
            if self.vanished:
                raise IntegrityError("UPDATE connection_items", {}, Exception("fk_connection_items_document"))
            self.finished.append((state, values))
            return True

        monkeypatch.setattr(sources_db, "lock_importing_connection", lock)
        monkeypatch.setattr(imports_db, "declare_item", declare)
        monkeypatch.setattr(imports_db, "find_stored_document", find)
        monkeypatch.setattr(sources_db, "finish_item", finish)

        @asynccontextmanager
        async def transaction():
            yield None

        self.writer = ImportWriter(JOB, transaction=transaction, store=InMemoryObjectStore(app_env="local"))


async def test_declare_keeps_the_listed_size_and_modified_time_and_stays_the_same_without_them(monkeypatch) -> None:
    rig = Rig(monkeypatch)

    await rig.writer.declare("id:1", "a.pdf", media_type="application/pdf", size_bytes=100, modified_time=MODIFIED)
    await rig.writer.declare("page:1", "Page")  # an importer that lists no size: nothing is kept

    assert rig.declared[0]["size_bytes"] == 100 and rig.declared[0]["modified_time"] == MODIFIED
    assert rig.declared[1]["size_bytes"] is None and rig.declared[1]["modified_time"] is None


async def test_an_unchanged_file_ends_duplicate_linking_the_stored_document(monkeypatch) -> None:
    rig = Rig(monkeypatch)

    linked = await rig.writer.link_stored("id:1", "a.pdf", size_bytes=100, modified_time=MODIFIED)

    assert linked is True
    assert rig.asked == [{"external_id": "id:1", "modified_time": MODIFIED, "size_bytes": 100}]
    assert rig.finished == [("duplicate", {"document_id": DOCUMENT})]
    assert rig.declared[0]["size_bytes"] == 100 and rig.declared[0]["modified_time"] == MODIFIED


async def test_a_file_the_project_does_not_hold_is_left_pending(monkeypatch) -> None:
    rig = Rig(monkeypatch, document=None)

    assert await rig.writer.link_stored("id:1", "a.pdf", size_bytes=100, modified_time=MODIFIED) is False

    assert rig.finished == [] and rig.declared == []


async def test_a_removed_source_stops_the_re_add_skip(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    rig.gone = True

    with pytest.raises(SourceGoneError):
        await rig.writer.link_stored("id:1", "a.pdf", size_bytes=100, modified_time=MODIFIED)


async def test_a_document_removed_between_the_find_and_the_link_is_downloaded_instead(monkeypatch) -> None:
    rig = Rig(monkeypatch)
    rig.vanished = True

    assert await rig.writer.link_stored("id:1", "a.pdf", size_bytes=100, modified_time=MODIFIED) is False

    assert rig.finished == []
