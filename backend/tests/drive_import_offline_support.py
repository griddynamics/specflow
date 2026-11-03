"""Fakes of the Drive import activity tests (no tests here): sessions, a repository stand-in, a flaky Drive.

Shared by ``test_drive_import_offline.py`` and ``test_drive_import_formats.py``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from temporalio.testing import ActivityEnvironment

from db import repositories
from db import sources as sources_repo
from drive.categories import categorize
from drive.fake import FakeDriveGateway
from temporal.sources import activities as acts
from temporal.sources.models import ImportItemInput

PID, CID, OTHER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class Session:
    def begin(self):
        return contextlib.nullcontext()


class Sessions:
    def __call__(self):
        @contextlib.asynccontextmanager
        async def manager():
            yield Session()

        return manager()


def make_item(
    *,
    mime="application/pdf",
    file_id="file0123456",
    category="pdf",
    name="Quarterly plan.pdf",
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        connection_id=CID,
        project_id=PID,
        external_id=file_id,
        name=name,
        mime_type=mime,
        category=category,
        state="pending",
    )


class Repo:
    """Stands in for the database functions the activities call."""

    def __init__(self, monkeypatch, item, *, state="importing", existing_doc=None):
        self.item = item
        self.connection = SimpleNamespace(id=CID, state=state, external_id="folder01234")
        self.finished: list[dict] = []
        self.inserted: list[dict] = []
        self.existing_doc = existing_doc
        self.duplicate_of = None
        self.removed_at_fence = False  # the source row is gone when the insert would run
        self.claims = 0
        monkeypatch.setattr(sources_repo, "claim_item", self.claim_item)
        monkeypatch.setattr(sources_repo, "lock_importing_connection", self.lock_importing_connection)
        monkeypatch.setattr(sources_repo, "get_connection", self.get_connection)
        monkeypatch.setattr(sources_repo, "get_item", self.get_item)
        monkeypatch.setattr(sources_repo, "finish_item", self.finish_item)
        monkeypatch.setattr(sources_repo, "insert_connection_document", self.insert)
        monkeypatch.setattr(repositories, "get_document_state", self.get_document_state)

    async def get_connection(self, _session, _project_id, _connection_id):
        return self.connection

    async def get_item(self, _session, _item_id):
        return self.item

    async def claim_item(self, _session, _project_id, _connection_id, _item_id):
        if (
            self.connection is None
            or self.connection.state != "importing"
            or self.item.state not in ("pending", "processing")
        ):
            return None
        self.claims += 1
        self.item.state = "processing"
        return self.item

    async def lock_importing_connection(self, _session, _project_id, _connection_id):
        if self.removed_at_fence or self.connection is None:
            return None
        return self.connection if self.connection.state == "importing" else None

    async def get_document_state(self, _session, _project_id, _document_id):
        return self.existing_doc

    async def finish_item(self, _session, _item_id, state, **kw):
        self.finished.append({"state": state, **kw})
        self.item.state = state
        return True

    async def insert(self, _session, *, file, **kw):
        kw = dataclasses.asdict(file) | kw
        self.inserted.append(kw)
        if self.duplicate_of is not None:
            return self.duplicate_of, False
        return (
            SimpleNamespace(id=kw["document_id"], status=kw["status"], source_id=kw["source_id"]),
            True,
        )


class Flaky(FakeDriveGateway):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.download_error: Exception | None = None
        self.read_error: Exception | None = None
        self.payload = None

    def iter_download(self, file_id, *, chunk_size=8 * 1024 * 1024):
        if self.download_error:
            raise self.download_error
        return super().iter_download(file_id, chunk_size=chunk_size)

    def read_google(self, file_id, mime_type):
        if self.read_error:
            raise self.read_error
        if self.payload is not None:
            return self.payload
        return super().read_google(file_id, mime_type)


def build(store, pool, drive=None, **kw):
    drive = drive or Flaky(app_env="local", file_delay_s=0, role="worker")
    return acts.DriveImportActivities(Sessions(), store, drive, pool, **kw), drive


def data(item, kind):
    return ImportItemInput(project_id=PID, connection_id=CID, item_id=item.id, kind=kind)


def fake_item_of(drive, category_mime):
    return next(i for i in drive.list_all("fake-synthetic-30").items if i.mime_type == category_mime)


async def run_activity(activities, function, arg, *, attempt=1):
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, attempt=attempt)
    return await env.run(function, arg)


class Served(Flaky):
    """Every file of the listing has the same bytes: ``content`` (a file copied into two folders, a hostile file)."""

    content = b""

    def iter_download(self, file_id, *, chunk_size=8 * 1024 * 1024):
        super().iter_download(file_id, chunk_size=chunk_size)  # the id must exist
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]


def mixed_drive(tmp_path: Path, mimes: list[str], cls: type[Flaky] = Flaky) -> Flaky:
    """A fake Drive whose one folder lists one file per MIME type (``fake-mixed-i0``, ``-i1``, ...)."""

    folder = tmp_path / "recordings"
    folder.mkdir()
    listing = {
        "recorded": "2026-11-01",
        "folders": [{"key": "f0", "depth": 0, "parent": None, "name": "Mixed"}],
        "items": [
            {"name": f"file {n}", "mimeType": mime, "size": None, "modifiedTime": None, "folder": "f0"}
            for n, mime in enumerate(mimes)
        ],
    }
    (folder / "mixed.json").write_text(json.dumps(listing), encoding="utf-8")
    return cls(app_env="local", file_delay_s=0, role="worker", fixtures=tmp_path)


def row_of(index: int, mime: str):
    """The item row the listing would store: category and class by MIME, as ``upsert_items`` does."""

    category, klass = categorize(mime)
    row = make_item(mime=mime, file_id=f"fake-mixed-i{index}", category=category.value)
    row.processing_class = klass.value
    return row


def kind_of(row) -> str | None:
    return acts.DriveImportActivities._kind(row)
