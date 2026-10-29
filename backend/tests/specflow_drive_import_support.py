"""Tier-2 fixtures and helpers of the Drive import tests (no tests here).

A scratch Postgres database, the in-memory object store, the fake Drive gateway
in the ``worker`` role and the local Temporal namespace with a UNIQUE task queue
(the owner's ``parse`` queue is never used). ``python -m specflow_drive_import_support``
runs a killable worker process (file-backed object store) for the restart test.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from db import repositories, sources
from db.models import Connection, ConnectionItem, Document, User
from db.session import build_session_factory
from drive.fake import FakeDriveGateway
from settings import DatabaseSettings
from specflow_live import (
    LOCAL_NAMESPACE,
    TemporalLive,
    connect_local_temporal,
    live_enabled,
)
from storage.base import ObjectAlreadyExistsError, ObjectNotFoundError, StoredObject
from storage.keys import validate_key
from storage.memory import InMemoryObjectStore
from temporal import ids
from temporal.documents import registration
from temporal.sources.models import DriveImportInput
from temporal.sources.workflows import DriveImportWorkflow

FOLDER_PREFIX = "https://drive.google.com/drive/folders/"


class DirObjectStore:
    """File-backed store with create-only writes: shared by two processes (restart test)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.content_types: dict[str, str] = {}

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    async def open_writer(self, key: str, *, content_type: str):
        self.content_types[key] = content_type
        return _DirWriter(self._path(key), key)

    async def download_to_path(self, key: str, path: Path) -> int:
        try:
            data = await asyncio.to_thread(self._path(key).read_bytes)
        except FileNotFoundError:
            raise ObjectNotFoundError(key) from None
        await asyncio.to_thread(path.write_bytes, data)
        return len(data)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).exists)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def check(self) -> None:
        return None

    def object_keys(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


class _DirWriter:
    def __init__(self, path: Path, key: str) -> None:
        self._path, self._key = path, key
        self._buffer = bytearray()

    async def write(self, chunk: bytes) -> None:
        self._buffer += chunk

    async def close(self) -> StoredObject:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("xb") as handle:  # create-only
                handle.write(bytes(self._buffer))
        except FileExistsError:
            raise ObjectAlreadyExistsError(self._key) from None
        return StoredObject(self._key, len(self._buffer))

    async def abort(self) -> None:
        self._buffer.clear()


@dataclass
class DriveStack:
    client: Client
    engine: AsyncEngine
    sessions: async_sessionmaker
    store: Any
    temporal: TemporalLive
    database: DatabaseSettings
    user_id: uuid.UUID
    project_id: uuid.UUID

    @property
    def queue(self) -> str:
        return self.temporal.task_queue

    def gateway(self, *, delay: float = 0.0, role: str = "worker") -> FakeDriveGateway:
        return FakeDriveGateway(app_env="local", file_delay_s=delay, role=role)

    async def connect(self, folder_id: str, *, name: str = "Folder") -> uuid.UUID:
        """A saved source in ``listing`` (what the save handler leaves)."""

        async with self.sessions() as session, session.begin():
            row, _ = await sources.create_connection(
                session,
                project_id=self.project_id,
                kind="gdrive",
                external_id=folder_id,
                link=FOLDER_PREFIX + folder_id,
                name=name,
                connected_by=self.user_id,
            )
        return row.id

    @asynccontextmanager
    async def worker(self, drive: Any = None, *, store: Any = None, queue: str | None = None):
        """An in-process worker on the test's queue with the REAL registration."""

        built = registration.build_registration(self.engine, store or self.store, drive=drive or self.gateway())
        worker = Worker(
            self.client,
            task_queue=queue or self.queue,
            workflows=list(built.workflows),
            activities=list(built.activities),
        )
        try:
            async with worker:
                yield built
        finally:
            await built.close()

    @asynccontextmanager
    async def session_factory_tx(self):
        async with self.sessions() as session, session.begin():
            yield session

    async def start(self, connection_id: uuid.UUID, **options: Any) -> WorkflowHandle:
        options.setdefault("execution_timeout", sources.DRIVE_IMPORT_EXECUTION_TIMEOUT)
        handle = await self.client.start_workflow(
            DriveImportWorkflow.run,
            DriveImportInput(
                project_id=self.project_id,
                connection_id=connection_id,
                id_prefix=self.temporal.id_prefix,
            ),
            id=ids.drive_import_workflow_id(connection_id, prefix=self.temporal.id_prefix),
            task_queue=self.queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            **options,
        )
        return self.temporal.track(handle)

    # -- reads -----------------------------------------------------------------
    async def connection(self, connection_id: uuid.UUID) -> Connection:
        async with self.sessions() as session:
            row = await sources.get_connection(session, self.project_id, connection_id)
        assert row is not None
        return row

    async def items(self, connection_id: uuid.UUID) -> list[ConnectionItem]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(ConnectionItem)
                    .where(ConnectionItem.connection_id == connection_id)
                    .order_by(ConnectionItem.external_id)
                )
            )

    async def documents(self) -> list[Document]:
        async with self.sessions() as session:
            return list(await session.scalars(select(Document).where(Document.project_id == self.project_id)))

    async def progress(self):
        async with self.sessions() as session:
            return await sources.sources_progress(session, self.project_id)

    async def finished_count(self, connection_id: uuid.UUID) -> int:
        async with self.sessions() as session:
            return await session.scalar(
                select(func.count())
                .select_from(ConnectionItem)
                .where(
                    ConnectionItem.connection_id == connection_id,
                    ConnectionItem.state.not_in(("pending", "processing")),
                )
            )


@pytest_asyncio.fixture
async def drive_stack(migrated_database, temporal_live) -> AsyncIterator[DriveStack]:
    await connect_local_temporal()  # the same namespace guard as every live test
    assert live_enabled("TEMPORAL")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233").strip(),
        namespace=LOCAL_NAMESPACE,
        data_converter=pydantic_data_converter,
    )
    engine = create_async_engine(migrated_database.url())
    sessions = build_session_factory(engine)
    async with sessions() as session, session.begin():
        user = User(
            actor_id=f"human:t{uuid.uuid4().hex[:8]}",
            display_name="T",
            email="t@example.com",
        )
        session.add(user)
        await session.flush()
        project = await repositories.create_project(session, user.id, "pytest drive")
    yield DriveStack(
        client,
        engine,
        sessions,
        InMemoryObjectStore(app_env="local"),
        temporal_live,
        migrated_database,
        user.id,
        project.id,
    )
    await engine.dispose()


# ------------------------------------------------------- worker process (SIGKILL test)
async def _run_killable_worker() -> None:
    database = DatabaseSettings.from_environment().model_copy(update={"name": os.environ["DRIVE_TEST_DATABASE"]})
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"],
        namespace=os.environ["TEMPORAL_NAMESPACE"],
        data_converter=pydantic_data_converter,
    )
    engine = create_async_engine(database.url())
    built = registration.build_registration(
        engine,
        DirObjectStore(Path(os.environ["DRIVE_TEST_STORE"])),
        drive=FakeDriveGateway(app_env="local", file_delay_s=1.0, role="worker"),
    )
    worker = Worker(
        client,
        task_queue=os.environ["DRIVE_TEST_QUEUE"],
        workflows=list(built.workflows),
        activities=list(built.activities),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(_run_killable_worker())
