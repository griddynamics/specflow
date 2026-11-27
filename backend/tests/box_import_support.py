"""Shared fakes of the Box importer tests (no tests in here), on top of ``box_support``: a Box that also answers the
``[extracted_text]`` representation of a Box Note, the ``Connections`` an importer uses (one member, one fixed token)
and a writer that records what an importer stores. Every name, text and id is made up."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import httpx
from pydantic import SecretStr

from api.imports import ImportJob, ImportWriter
from box.codes import BoxCode, BoxError
from box.connections import BoxConnections
from box_support import ENTERPRISE_ID, USERS_ME_ENTERPRISE, FakeBox
from import_writer_support import StreamingFileWriter

MEMBER = uuid.UUID(int=77)
PROJECT, SOURCE = uuid.UUID(int=1), uuid.UUID(int=2)
BEARER = "access-test"
NOTE_URL = "https://api.box.com/2.0/internal_files/{id}/representations/extracted_text"
NOTE_CONTENT = (
    "https://dl.boxcloud.com/api/2.0/internal_files/{id}/representations/extracted_text/content/{{+asset_path}}"
)


def file_entry(file_id: str, name: str, size: int = 10, **more: Any) -> dict[str, Any]:
    extension = name.rsplit(".", 1)[1].lower() if "." in name else None
    return {"type": "file", "id": file_id, "name": name, "size": size, "extension": extension, **more}


def folder_entry(folder_id: str, name: str) -> dict[str, Any]:
    return {"type": "folder", "id": folder_id, "name": name, "item_status": "active"}


class NoteBox(FakeBox):
    """``FakeBox`` plus Box Notes: ``states[file id]`` is the ``status.state`` the representation has at each ask (the
    last one repeats); asking ``info.url`` is recorded in ``started``; ``hints`` is every ``x-rep-hints`` header."""

    def __init__(self) -> None:
        super().__init__()
        self.states: dict[str, list[str]] = {}
        self.started: list[str] = []
        self.hints: list[str] = []
        self.asks = 0
        self.content_auth: list[str] = []  # the ``Authorization`` header of each Note content request
        self.content_status: int | None = None  # every Note content request answers this status

    def _rest(self, request: httpx.Request, user: dict[str, Any]) -> httpx.Response:
        parts = request.url.path.removeprefix("/2.0/").split("/")
        if parts[0] == "internal_files":  # only the info URL lives on api.box.com
            return self._internal(parts)
        if len(parts) == 2 and parts[0] == "files" and "x-rep-hints" in request.headers:
            self.hints.append(request.headers["x-rep-hints"])
            return self._representations(parts[1])
        return super()._rest(request, user)

    def _representations(self, file_id: str) -> httpx.Response:
        self.asks += 1
        order = self.states.get(file_id, ["success"])
        state = order.pop(0) if len(order) > 1 else order[0]
        entry = {
            "representation": "extracted_text",
            "properties": {},
            "info": {"url": NOTE_URL.format(id=file_id)},
            "content": {"url_template": NOTE_CONTENT.format(id=file_id)},
            "status": {"state": state},
        }
        return httpx.Response(200, json={"id": file_id, "type": "file", "representations": {"entries": [entry]}})

    def _internal(self, parts: list[str]) -> httpx.Response:
        file_id = parts[1]
        if parts[-1] != "extracted_text":  # the content is not served here: Box documents dl.boxcloud.com for it
            return httpx.Response(404, json={"code": "not_found"})
        self.started.append(file_id)  # the info URL: asking starts the generation
        return httpx.Response(200, json={"id": file_id})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "dl.boxcloud.com" and request.url.path.startswith("/api/2.0/internal_files/"):
            return self._note_content(request)
        return await super().handle(request)

    def _note_content(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.method != "GET":
            self.violations.append((request.method, request.url.path))
            return httpx.Response(405)
        self.content_auth.append(request.headers.get("Authorization", ""))
        if self.content_status is not None:
            return httpx.Response(self.content_status)
        if request.headers.get("Authorization") != f"Bearer {BEARER}":
            return httpx.Response(401)
        text = self.representations.get(request.url.path.split("/")[4])
        if text is None:
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, content=text.encode())


def add_folder(box: FakeBox, folder_id: str, entries: list[dict[str, Any]], name: str = "Folder") -> None:
    box.folders[folder_id] = {"type": "folder", "id": folder_id, "name": name, "item_status": "active"}
    box.items[folder_id] = entries


def add_file(box: FakeBox, entry: dict[str, Any], data: bytes | None = None) -> None:
    box.files[entry["id"]] = entry
    if data is not None:
        box.content[entry["id"]] = data


class FakeConnections:
    """The ``BoxConnections`` an importer or the routes use: one member, one fixed access token on ``box``."""

    def __init__(self, box: FakeBox, *, user_id: uuid.UUID = MEMBER, status: str = "active") -> None:
        box.access[BEARER] = USERS_ME_ENTERPRISE
        self.client = box.client()
        self.user_id = user_id
        self.status = status
        self.workspace_id = ENTERPRISE_ID  # the workspace of the member's connection (the account they connected)
        self.expired = False
        self.used = 0

    async def get(self, user_id: uuid.UUID) -> SimpleNamespace | None:
        return SimpleNamespace(status=self.status, workspace_id=self.workspace_id) if user_id == self.user_id else None

    async def call(self, _user_id: uuid.UUID, request: Any) -> Any:
        self.used += 1
        if self.expired:
            raise BoxError(BoxCode.RECONNECT_REQUIRED)
        return await request(SecretStr(BEARER))


class BoxWriter(StreamingFileWriter):
    """What an importer stores, in order: ``declared``, ``skipped``, ``failed`` (external id, reason, step), ``files``
    (``add_file``: external id -> name, bytes, media type), ``notes`` (``add``: external id -> name, markdown, original)
    and the files whose parse started. Bytes already seen are ``duplicate``. ``peak`` is the most ``add_file`` calls
    open at once."""

    def __init__(self, *, gone: bool = False) -> None:
        self.declared: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str, str]] = []
        self.files: dict[str, tuple[str, bytes, str]] = {}
        self.notes: dict[str, tuple[str, str, bytes]] = {}
        self.parsed: list[str] = []
        self.duplicates: list[str] = []
        self.hashes: set[str] = set()
        self.open = self.peak = 0
        self.gone = gone

    async def declare(self, external_id: str, name: str, **_: Any) -> None:
        self.declared.append((external_id, name))

    async def skip(self, external_id: str, name: str) -> None:
        self.skipped.append((external_id, name))

    async def fail(self, external_id: str, _name: str, *, reason: str, step: str) -> None:
        self.failed.append((external_id, reason, step))

    async def add(self, external_id: str, *, name: str, original: bytes, markdown: str, **_: Any) -> str:
        self.notes[external_id] = (name, markdown, original)
        return "added"


def as_connections(connections: FakeConnections) -> BoxConnections:
    """The fake where an importer expects its ``BoxConnections`` (the same ``client``, ``get`` and ``call``)."""

    return cast(BoxConnections, connections)


def as_writer(writer: BoxWriter) -> ImportWriter:
    return cast(ImportWriter, writer)


def job() -> ImportJob:
    return ImportJob(PROJECT, SOURCE, MEMBER, "box")
