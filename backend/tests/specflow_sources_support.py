"""Fixtures for the sources API tests (no tests in here).

``FakeSources`` stands in for ``db.sources`` (the real SQL is covered by
the Postgres tier); the Drive gateway is the real ``FakeDriveGateway`` without
delays, so no socket is ever opened.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api import workflows
from db import sources as sources_repo
from db.session import build_engine, build_session_factory
from drive.fake import FakeDriveGateway
from settings import DatabaseSettings
from specflow_api_support import bearer
from specflow_sources_live_support import SourcesWorld, make_project, make_user

_TICK = itertools.count()
PROJECT_FOLDER = "https://drive.google.com/drive/folders/"
SYNTHETIC = PROJECT_FOLDER + "fake-synthetic-30"


class FakeSources:
    def __init__(self) -> None:
        self.rows: dict[uuid.UUID, SimpleNamespace] = {}
        self.progress: dict[uuid.UUID, sources_repo.ConnectionProgress] = {}
        self.uploads: sources_repo.UploadsProgress | None = None
        self.processed = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in [
            "find_connection",
            "create_connection",
            "delete_connection_if_empty",
            "list_connections",
            "sources_progress",
            "set_connection_state",
            "count_processed_documents",
        ]:
            monkeypatch.setattr(sources_repo, name, getattr(self, name))

    async def find_connection(self, _session, project_id, kind, external_id):
        for row in self.rows.values():
            if (row.project_id, row.kind, row.external_id) == (
                project_id,
                kind,
                external_id,
            ):
                return row
        return None

    async def create_connection(self, session, *, project_id, kind, external_id, link, name, connected_by):
        existing = await self.find_connection(session, project_id, kind, external_id)
        if existing is not None:
            return existing, False
        row = SimpleNamespace(
            id=uuid.uuid4(),
            project_id=project_id,
            kind=kind,
            external_id=external_id,
            link=link,
            name=name,
            state="listing",
            failure_reason=None,
            connected_by=connected_by,
            last_synced_at=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=next(_TICK)),
        )
        self.rows[row.id] = row
        return row, True

    async def delete_connection_if_empty(self, _session, project_id, connection_id):
        row = self.rows.get(connection_id)
        if row is None or row.project_id != project_id:
            return False
        del self.rows[connection_id]
        return True

    async def set_connection_state(self, _session, connection_id, state, *, failure_reason=None):
        row = self.rows.get(connection_id)
        if row is None:
            return False
        row.state, row.failure_reason = state, failure_reason
        return True

    async def list_connections(self, _session, project_id, kind=None):
        return sorted(
            (r for r in self.rows.values() if r.project_id == project_id and kind in (None, r.kind)),
            key=lambda r: r.created_at,
        )

    async def count_processed_documents(self, _session, _project_id):
        return self.processed

    async def sources_progress(self, session, project_id):
        connections = tuple(
            self.progress.get(
                row.id,
                sources_repo.ConnectionProgress(
                    row.id,
                    row.state,
                    row.failure_reason,
                    sources_repo.Progress(row.state != "listing", 0, 0),
                ),
            )
            for row in await self.list_connections(session, project_id)
        )
        parts = [c.progress for c in connections]
        if self.uploads:
            parts.append(self.uploads.progress)
        return sources_repo.SourcesProgress(
            progress=sources_repo.Progress(
                all(p.listing_finished for p in parts),
                sum(p.total for p in parts),
                sum(p.finished for p in parts),
            ),
            connections=connections,
            uploads=self.uploads,
        )


class Sources:
    """The calls a sources test makes, for one project at a time."""

    def __init__(self, harness: Any, repo: FakeSources, started: list[tuple]) -> None:
        self.harness, self.repo, self.started = harness, repo, started
        self.pid = ""

    @property
    def client(self):
        return self.harness.client

    async def post(self, path: str, link: str | None = None, *, email=None, **kw):
        body = kw.pop("json", {"link": link})
        return await self.client.post(
            f"/api/v1/projects/{self.pid}/sources/gdrive{path}",
            json=body,
            headers=bearer(email or "alice@example.com"),
            **kw,
        )

    async def test(self, link, **kw):
        return await self.post("/test", link, **kw)

    async def save(self, link, **kw):
        return await self.post("", link, **kw)

    async def status(self, email="alice@example.com"):
        return await self.client.get(f"/api/v1/projects/{self.pid}/sources", headers=bearer(email))


@pytest.fixture
def sources(harness, monkeypatch):
    fake = FakeSources()
    fake.install(monkeypatch)
    harness.runtime.drive = FakeDriveGateway(app_env="local", file_delay_s=0)
    started: list[tuple] = []

    async def hook(runtime, project_id, connection_id):
        started.append((project_id, connection_id))
        return f"drive-import-{connection_id}"

    monkeypatch.setattr(workflows, "start_drive_import", hook)
    return Sources(harness, fake, started)


async def new_project(ctx: Sources, email: str = "alice@example.com") -> str:
    response = await ctx.client.post("/api/v1/projects", json={"name": "p"}, headers=bearer(email))
    ctx.pid = response.json()["id"]
    return ctx.pid


@pytest_asyncio.fixture
async def sources_world(
    migrated_database: DatabaseSettings,
) -> AsyncIterator[SourcesWorld]:
    engine = build_engine(migrated_database)
    factory = build_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            user = await make_user(session, "u")
            other = await make_user(session, "o")
            a = await make_project(session, user, "A")
            b = await make_project(session, user, "B")
        yield SourcesWorld(factory, user, other, a, b)
    finally:
        await engine.dispose()
