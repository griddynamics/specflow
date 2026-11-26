"""The Box importer (docs/box/source.md "Import"): the files of a folder tree, or one file, as documents.

Runs in the API process as the member the project's Box is connected by. The whole tree is walked once (D7, decision
B14): folders breadth first with marker paging, at most ``MAX_ENTRIES`` entries (files and folders) and ``MAX_FILES``
importable files; a cap that is hit stops the walk, is logged once (counts only, ``api.imports.box_cap_reached``)
and the check says ``capped``. Every file is declared first (so progress shows its total early), then handled in one
of three lanes (docs/box/source.md "Lanes"):

- a parser format (PDF, DOCX, PPTX, XLSX, HTML, MD, CSV) or ``.txt``: streamed to the originals through
  ``ImportWriter.add_file`` (hashed while it streams, at most ``DOWNLOAD_SLOTS`` downloads at a time), then parsed by
  ``worker-parse`` (which gets bytes, never Box tokens);
- a Box Note (``.boxnote``): its ``[extracted_text]`` representation through ``ImportWriter.add`` (parser
  ``box-convert``);
- anything else: ended ``skipped``, never downloaded.

The shared link of a link-by-share source lives in ``BoxImport.target`` for this one run and is never stored or logged.
The names of items live in the source and its documents only: events carry ids, kinds and counts.

Tested in ``tests/test_api_imports_box.py``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import SecretStr

from api.imports.model import ImportFailedError, ImportJob, OriginalTooLargeError, SourceGoneError
from api.imports.writer import ImportWriter
from box.client import WaitBudget
from box.codes import BoxCode, BoxError
from box.connections import BoxConnections
from box.content import BoxContent, BoxItem, ItemsPage
from box.links import BoxTarget
from file_parsers.formats import FORMATS, DocumentFormat
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.imports.box")

MAX_FILES: Final = 1000  # importable files of one import
MAX_ENTRIES: Final = 5000  # entries (files and folders) one walk lists
DOWNLOAD_SLOTS: Final = 4  # downloads at a time in one import
MAX_PDF_BYTES: Final = 100 * 1024 * 1024  # a larger PDF (by its listed size) is skipped; the cap holds while streaming
MAX_OOXML_BYTES: Final = 50 * 1024 * 1024  # DOCX, PPTX, XLSX
MAX_TEXT_BYTES: Final = 10 * 1024 * 1024  # HTML, MD, CSV, TXT
MAX_NOTE_BYTES: Final = 5 * 1024 * 1024  # the text of a Box Note
NO_ACCESS: Final = "no_access"
UNKNOWN_ERROR: Final = "unknown_error"
CONVERT_FAILED: Final = "convert_failed"
READ_STEP: Final = "read"
DOWNLOAD_STEP: Final = "download"
CONVERT_STEP: Final = "convert"

_NOTE: Final = "boxnote"
_TEXT_MEDIA_TYPE: Final = "text/markdown"  # a ``.txt`` is stored as Markdown, as an upload of it is
_FORMAT_CAPS: Final = {
    DocumentFormat.PDF: MAX_PDF_BYTES,
    DocumentFormat.DOCX: MAX_OOXML_BYTES,
    DocumentFormat.PPTX: MAX_OOXML_BYTES,
    DocumentFormat.XLSX: MAX_OOXML_BYTES,
}
_BY_EXTENSION: Final = {spec.extension.lstrip("."): spec for spec in FORMATS}
# A connection that cannot be used ends the source ``no_access`` (and stops its other downloads at once).
_NO_ACCESS_CODES: Final = frozenset(
    {
        BoxCode.NO_ACCESS,
        BoxCode.LINK_INVALID,
        BoxCode.LINK_PROTECTED,
        BoxCode.NOT_CONNECTED,
        BoxCode.RECONNECT_REQUIRED,
        BoxCode.AUTH_FAILED,
        BoxCode.ACCESS_REFUSED,
    }
)
_ABORT_CODES: Final = _NO_ACCESS_CODES - {BoxCode.NO_ACCESS}

Lane = Literal["original", "note", "skipped"]


@dataclass(frozen=True)
class Found:
    """One file the walk found: where it is (``path`` inside the source), how it is handled and its limits."""

    item: BoxItem
    path: str
    lane: Lane
    media_type: str = ""
    cap: int = 0

    @property
    def external_id(self) -> str:
        return f"file:{self.item.id}"


@dataclass
class Scan:
    """The result of one walk. ``files`` are the importable files (at most ``MAX_FILES``), ``skipped`` the files of
    other types or over their cap, ``unreadable`` the folders the member cannot read (``(external id, path)``)."""

    root: BoxItem
    files: list[Found]
    skipped: list[Found]
    unreadable: list[tuple[str, str]]
    folders: int = 0
    entries: int = 0
    cap: str | None = None  # ``files`` or ``entries`` when a cap ended the walk


def classify(item: BoxItem, path: str) -> Found:
    """The lane of one file by its extension and listed size (docs/box/source.md "What is imported")."""

    extension = item.extension or ""
    if not item.active:
        return Found(item, path, "skipped")
    if extension == _NOTE:
        return Found(item, path, "note", "text/plain", MAX_NOTE_BYTES)
    if extension == "txt":
        media_type, cap = _TEXT_MEDIA_TYPE, MAX_TEXT_BYTES
    else:
        spec = _BY_EXTENSION.get(extension)
        if spec is None:
            return Found(item, path, "skipped")
        media_type, cap = spec.media_type, _FORMAT_CAPS.get(spec.format, MAX_TEXT_BYTES)
    if item.size > cap:
        return Found(item, path, "skipped")
    return Found(item, path, "original", media_type, cap)


class _Reader:
    """The reads of one run as the bound member: the content calls, the token (one refresh on a 401), the shared link
    of the run (in memory only) and the wait budget of the walk."""

    def __init__(self, connections: BoxConnections, user_id: uuid.UUID, target: BoxTarget) -> None:
        self._connections = connections
        self._user_id = user_id
        self.shared_link = target.shared_link
        self.content = BoxContent(connections.client)
        self.budget = WaitBudget()

    async def call[T](self, request: Callable[[SecretStr], Awaitable[T]]) -> T:
        return await self._connections.call(self._user_id, request)


async def _resolve(reader: _Reader, target: BoxTarget) -> BoxItem:
    """The folder or file a link names; a web link, All Files or an item in the trash cannot be imported."""

    content, budget = reader.content, reader.budget
    if target.kind == "shared" and target.shared_link is not None:
        link = target.shared_link
        item = await reader.call(lambda token: content.shared_item(token, link, budget=budget))
    elif target.kind == "folder" and target.id is not None:
        folder_id = target.id
        item = await reader.call(lambda token: content.folder(token, folder_id, budget=budget))
    elif target.kind == "file" and target.id is not None:
        file_id = target.id
        item = await reader.call(lambda token: content.file(token, file_id, budget=budget))
    else:
        raise BoxError(BoxCode.LINK_INVALID)
    if item.kind == "web_link" or (item.kind == "folder" and item.id == "0"):
        raise BoxError(BoxCode.LINK_INVALID)
    if not item.active:
        raise BoxError(BoxCode.NO_ACCESS)
    return item


async def _page(reader: _Reader, folder_id: str, marker: str | None) -> ItemsPage:
    content, budget, link = reader.content, reader.budget, reader.shared_link
    return await reader.call(
        lambda token: content.folder_items(token, folder_id, marker=marker, shared_link=link, budget=budget)
    )


def _take(scan: Scan, entries: list[BoxItem], prefix: str, queue: deque[tuple[str, str]]) -> None:
    """Sort the entries of one page into ``scan``, until a cap ends the walk (``scan.cap``)."""

    for entry in entries:
        if scan.entries >= MAX_ENTRIES:
            scan.cap = "entries"
            return
        path = f"{prefix}{entry.name}"
        if entry.kind == "folder":
            scan.entries += 1
            scan.folders += 1
            queue.append((entry.id, f"{path}/"))
            continue
        found = classify(entry, path) if entry.kind == "file" else Found(entry, path, "skipped")
        if found.lane != "skipped" and len(scan.files) >= MAX_FILES:
            scan.cap = "files"  # this file is not taken, so it is not counted
            return
        scan.entries += 1
        (scan.skipped if found.lane == "skipped" else scan.files).append(found)


async def _scan(reader: _Reader, root: BoxItem) -> Scan:
    """Walk the tree under ``root`` (breadth first) and sort what is in it, to the caps."""

    scan = Scan(root, [], [], [])
    if root.kind == "file":
        found = classify(root, root.name)
        (scan.files if found.lane != "skipped" else scan.skipped).append(found)
        scan.entries = 1
        return scan
    queue: deque[tuple[str, str]] = deque([(root.id, "")])
    while queue and scan.cap is None:
        folder_id, prefix = queue.popleft()
        marker: str | None = None
        while scan.cap is None:
            try:
                page = await _page(reader, folder_id, marker)
            except BoxError as error:
                if error.code is not BoxCode.NO_ACCESS or not prefix:
                    raise  # the root, or anything but a folder the member cannot read: the run ends
                scan.unreadable.append((f"folder:{folder_id}", prefix.rstrip("/")))
                break
            _take(scan, page.entries, prefix, queue)
            marker = page.next_marker
            if marker is None:
                break
    return scan


def _failure(error: BoxError) -> ImportFailedError:
    return ImportFailedError(NO_ACCESS if error.code in _NO_ACCESS_CODES else UNKNOWN_ERROR)


@dataclass(frozen=True)
class BoxImport:
    """What to import: the parsed link (a folder, a file or a shared item)."""

    target: BoxTarget


@dataclass(frozen=True)
class Inspection:
    """What a link would import, for the check: a folder tree or one file, its name, the ``files`` an import takes (at
    most ``MAX_FILES``), the ``skipped`` items of other types, the ``folders`` walked, and whether a cap ``capped`` the
    walk. ``kind`` and ``resolved_id`` name the item the link resolved to (for ``connections.link``); a shared link
    resolves to its folder or file."""

    kind: Literal["folder", "file"]
    name: str
    resolved_id: str
    files: int
    skipped: int
    folders: int
    capped: bool


def _log_cap(scan: Scan, *, project_id: str | None = None, user_id: str | None = None) -> None:
    if scan.cap is not None:
        log_event(
            logger,
            WARNING,
            "api.imports.box_cap_reached",
            project_id=project_id,
            user_id=user_id,
            cap=scan.cap,
            files=len(scan.files),
            entries=scan.entries,
        )


@dataclass(frozen=True)
class Resolved:
    """The folder or file a link names (``kind``, ``id``, ``name``), without any listing of its items."""

    kind: Literal["folder", "file"]
    id: str
    name: str


async def resolve(connections: BoxConnections, user_id: uuid.UUID, source: BoxImport) -> Resolved:
    """Resolve the link as ``user_id`` (the bound member): one read of the item, no walk. The same ``BoxError``
    refusals as ``inspect`` for a target that cannot be imported."""

    root = await _resolve(_Reader(connections, user_id, source.target), source.target)
    return Resolved("file" if root.kind == "file" else "folder", root.id, root.name)


async def inspect(connections: BoxConnections, user_id: uuid.UUID, source: BoxImport) -> Inspection:
    """Resolve the link and walk the tree as ``user_id`` (the bound member), without downloading: the numbers of the
    check. ``BoxError`` when a read is refused (``box_no_access``, ``box_link_invalid`` for a web link or All Files,
    ``box_link_password``, ``box_unreachable``, ``box_reconnect_required``)."""

    reader = _Reader(connections, user_id, source.target)
    root = await _resolve(reader, source.target)
    scan = await _scan(reader, root)
    _log_cap(scan, user_id=str(user_id))
    kind: Literal["folder", "file"] = "file" if root.kind == "file" else "folder"
    return Inspection(kind, root.name, root.id, len(scan.files), len(scan.skipped), scan.folders, scan.cap is not None)


class BoxImporter:
    """An ``Importer`` (``api.imports.runner``) of one Box link."""

    def __init__(self, connections: BoxConnections, source: BoxImport) -> None:
        self._connections = connections
        self._source = source

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        """Walk, declare, then import each file in its lane. ``ImportFailedError('no_access')`` when the member cannot
        read the target (or the connection expired); ``unknown_error`` when Box is down or throttled past the wait
        budget. An item the member cannot read is ``no_access``/``read``; retries exhausted is ``unknown_error``/
        ``download``; a file over its cap while streaming is ``convert_failed``/``download``."""

        reader = _Reader(self._connections, job.user_id, self._source.target)
        try:
            root = await _resolve(reader, self._source.target)
            scan = await _scan(reader, root)
        except BoxError as error:
            raise _failure(error) from None
        _log_cap(scan, project_id=str(job.project_id))
        run = _Run(reader, writer)
        for found in (*scan.files, *scan.skipped):
            await writer.declare(found.external_id, found.path, media_type=found.media_type or "text/markdown")
        for found in scan.skipped:
            await writer.skip(found.external_id, found.path)
        for external_id, path in scan.unreadable:
            await writer.fail(external_id, path, reason=NO_ACCESS, step=READ_STEP)
        await run.import_all(scan.files)
        log_event(
            logger,
            INFO,
            "api.imports.box_finished",
            project_id=str(job.project_id),
            added=run.added,
            duplicates=run.duplicates,
            failed=run.failed + len(scan.unreadable),
            skipped=len(scan.skipped),
            folders=scan.folders,
        )


class _Run:
    """The files of one import: at most ``DOWNLOAD_SLOTS`` at a time, and what came of them."""

    def __init__(self, reader: _Reader, writer: ImportWriter) -> None:
        self._reader = reader
        self._writer = writer
        self.added = self.duplicates = self.failed = 0

    async def import_all(self, files: list[Found]) -> None:
        """Every file in its lane; the first error that ends the whole import (a connection that cannot be used, a
        removed source) is raised after the files in flight stop."""

        pending = iter(files)
        stopped: list[ImportFailedError | SourceGoneError] = []

        async def worker() -> None:
            for found in pending:
                if stopped:
                    return
                try:
                    await self._one(found)
                except (ImportFailedError, SourceGoneError) as error:
                    stopped.append(error)
                    return

        async with asyncio.TaskGroup() as group:  # any other error cancels the files in flight
            for _ in range(DOWNLOAD_SLOTS):
                _task = group.create_task(worker())
        if stopped:
            raise stopped[0]

    async def _one(self, found: Found) -> None:
        step = READ_STEP if found.lane == "note" else DOWNLOAD_STEP
        try:
            outcome = await (self._note(found) if found.lane == "note" else self._original(found))
        except OriginalTooLargeError:
            self.failed += 1
            await self._writer.fail(found.external_id, found.path, reason=CONVERT_FAILED, step=DOWNLOAD_STEP)
            return
        except BoxError as error:
            if error.code in _ABORT_CODES:
                raise _failure(error) from None
            self.failed += 1
            reason, item_step = (NO_ACCESS, READ_STEP) if error.code is BoxCode.NO_ACCESS else (UNKNOWN_ERROR, step)
            await self._writer.fail(found.external_id, found.path, reason=reason, step=item_step)
            return
        if outcome == "added":
            self.added += 1
        else:
            self.duplicates += 1

    async def _original(self, found: Found) -> Literal["added", "duplicate"]:
        content, link, file_id = self._reader.content, self._reader.shared_link, found.item.id

        async def store(token: SecretStr) -> Literal["added", "duplicate"]:
            async with content.download(token, file_id, shared_link=link, budget=WaitBudget()) as chunks:
                return await self._writer.add_file(
                    found.external_id,
                    name=found.path,
                    chunks=chunks,
                    media_type=found.media_type,
                    max_bytes=found.cap,
                )

        return await self._reader.call(store)

    async def _note(self, found: Found) -> Literal["added", "duplicate"]:
        content, link, file_id = self._reader.content, self._reader.shared_link, found.item.id
        text = await self._reader.call(
            lambda token: content.extracted_text(
                token, file_id, max_bytes=found.cap, shared_link=link, budget=WaitBudget()
            )
        )
        original = json.dumps({"file": {"id": file_id, "name": found.path}, "text": text}, sort_keys=True)
        return await self._writer.add(found.external_id, name=found.path, original=original.encode(), markdown=text)
