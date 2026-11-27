"""Shared fakes of the Dropbox importer tests (no tests in here), on top of ``dropbox_support``: a ``FakeDropbox`` whose
shared links are the links ``parse_link`` produces (``dl`` and ``st`` dropped), the ``DropboxConnections`` an importer
uses (one member, one fixed token) and a writer that records what an importer stores. Every name, text and id is made
up."""

from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

from pydantic import SecretStr

from api.imports import ImportJob, ImportWriter
from dropbox.codes import DropboxCode, DropboxError
from dropbox.connections import DropboxConnections
from dropbox.links import parse_link
from dropbox_support import (
    ACCOUNT_PERSONAL,
    FILE_LINK_CANARY,
    FOREIGN_LINK_CANARY,
    SHARED_LINK_CANARY,
    FakeDropbox,
    account_of,
)
from import_writer_support import StreamingFileWriter

MEMBER = uuid.UUID(int=77)
PROJECT, SOURCE = uuid.UUID(int=1), uuid.UUID(int=2)
BEARER = "sl.access-import"
FOLDER_URL = "https://www.dropbox.com/home/Graphico%20Lab"


def dropbox() -> FakeDropbox:
    """A ``FakeDropbox`` that also knows its shared links by the address the importer sends."""

    fake = FakeDropbox()
    for canary in (SHARED_LINK_CANARY, FILE_LINK_CANARY, FOREIGN_LINK_CANARY):
        link = parse_link(canary).shared_link
        assert link is not None
        fake.shared[link] = fake.shared[canary]
    return fake


class FakeConnections:
    """The ``DropboxConnections`` an importer or the routes use: one member, one fixed access token on ``fake``."""

    def __init__(self, fake: FakeDropbox, *, user_id: uuid.UUID = MEMBER, status: str = "active") -> None:
        fake.access[BEARER] = ACCOUNT_PERSONAL
        self.client = fake.client()
        self.user_id = user_id
        self.status = status
        self.workspace_id = f"user:{account_of().account_id}"  # the workspace of the member's connection
        self.expired = False
        self.used = 0

    async def get(self, user_id: uuid.UUID) -> SimpleNamespace | None:
        return SimpleNamespace(status=self.status, workspace_id=self.workspace_id) if user_id == self.user_id else None

    async def call(self, _user_id: uuid.UUID, request: Any) -> Any:
        self.used += 1
        if self.expired:
            raise DropboxError(DropboxCode.RECONNECT_REQUIRED)
        return await request(SecretStr(BEARER))


class DropboxWriter(StreamingFileWriter):
    """What an importer stores, in order: ``declared`` (external id, name), ``listed`` (external id -> size, modified),
    ``skipped``, ``failed`` (external id, reason, step), ``files`` (``add_file``: external id -> name, bytes, media
    type), ``notes`` (``add``: external id -> name, markdown, original, media type) and the files whose parse started.
    ``stored`` maps an external id to the (size, modified) of a document the project already holds (the re-add skip,
    ``linked``). Bytes already seen are ``duplicate``. ``peak`` is the most ``add_file`` calls open at once."""

    def __init__(self) -> None:
        self.declared: list[tuple[str, str]] = []
        self.listed: dict[str, tuple[int | None, datetime | None]] = {}
        self.skipped: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str, str]] = []
        self.files: dict[str, tuple[str, bytes, str]] = {}
        self.notes: dict[str, tuple[str, str, bytes, str]] = {}
        self.parsed: list[str] = []
        self.duplicates: list[str] = []
        self.stored: dict[str, tuple[int, datetime]] = {}
        self.linked: list[str] = []
        self.hashes: set[str] = set()
        self.open = self.peak = 0

    async def declare(
        self,
        external_id: str,
        name: str,
        *,
        size_bytes: int | None = None,
        modified_time: datetime | None = None,
        **_: Any,
    ) -> None:
        self.declared.append((external_id, name))
        self.listed[external_id] = (size_bytes, modified_time)

    async def link_stored(self, external_id: str, _name: str, *, size_bytes: int, modified_time: datetime) -> bool:
        if self.stored.get(external_id) != (size_bytes, modified_time):
            return False
        self.linked.append(external_id)
        return True

    async def skip(self, external_id: str, name: str) -> None:
        self.skipped.append((external_id, name))

    async def fail(self, external_id: str, _name: str, *, reason: str, step: str) -> None:
        self.failed.append((external_id, reason, step))

    async def add(
        self, external_id: str, *, name: str, original: bytes, markdown: str, original_media_type: str = "", **_: Any
    ) -> str:
        self.notes[external_id] = (name, markdown, original, original_media_type)
        return "added"


def as_connections(connections: FakeConnections) -> DropboxConnections:
    """The fake where an importer expects its ``DropboxConnections`` (the same ``client``, ``get`` and ``call``)."""

    return cast(DropboxConnections, connections)


def as_writer(writer: DropboxWriter) -> ImportWriter:
    return cast(ImportWriter, writer)


def job() -> ImportJob:
    return ImportJob(PROJECT, SOURCE, MEMBER, "dropbox")
