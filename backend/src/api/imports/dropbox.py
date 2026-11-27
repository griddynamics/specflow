"""The Dropbox importer (docs/dropbox/source.md "Import"): the files of a folder tree, or one file, as documents.

Runs in the API process as the member the project's Dropbox is connected by (R1). One recursive ``files/list_folder``
by path (D7, DB9, DB15), at most ``MAX_ENTRIES`` listed entries, ``MAX_FILES`` importable files and ``MAX_BYTES`` of
importable files by listed size (DB11); a hit cap stops the listing, is logged once (counts only,
``api.imports.dropbox_cap_reached``) and the check says ``capped``. The files the member unchecked (``exclude``) are not
imported but still count toward the caps, so the import takes at most what the check showed. Every file is declared
first, then handled in one of three lanes (docs/dropbox/source.md "Lanes"):

- a parser format (PDF, DOCX, PPTX, XLSX, HTML, MD, CSV) or ``.txt`` (the shared lane table ``api.imports.files``):
  downloaded by ``id`` and streamed to the originals through ``ImportWriter.add_file`` (at most ``DOWNLOAD_SLOTS`` at
  a time), then parsed by ``worker-parse``; an entry the project already holds unchanged (DB12) is linked as a
  ``duplicate`` without a download;
- a Paper doc (``.paper``, ``export_options`` with ``markdown``): ``files/export`` through ``ImportWriter.add`` (parser
  ``dropbox-convert``, DB10; ``skipped`` until a live export is recorded, task L0, or without a Markdown export);
- anything else: ended ``skipped``, never downloaded.

The pasted shared link lives in ``DropboxImport.target`` for this one run and is never stored or logged (DB8). Names
and paths live in the source and its documents only: events carry ids, kinds and counts (DB17).

W0 froze the constants, the dataclasses and the signatures; stream S2 writes the bodies and
``tests/test_api_imports_dropbox.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, override

from pydantic import SecretStr

from api.imports.files import MAX_OOXML_BYTES, MAX_PDF_BYTES, MAX_TEXT_BYTES, extension_of
from api.imports.files import classify as classify_file
from api.imports.model import ImportFailedError, ImportJob, ImportRun, OriginalTooLargeError
from api.imports.writer import MARKDOWN_MEDIA_TYPE, ImportWriter
from dropbox.client import DropboxApiError
from dropbox.codes import DropboxCode, DropboxError
from dropbox.connections import DropboxConnections
from dropbox.content import (
    EXPORT_FORMAT,
    MAX_EXPORT_BYTES,
    DropboxContent,
    DropboxEntry,
    ExportTooLargeError,
    FolderPage,
)
from dropbox.links import DropboxTarget
from integrations.pacing import WaitBudget
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("api.imports.dropbox")

MAX_FILES: Final = 1000  # importable files of one import (and files listed by the check)
MAX_ENTRIES: Final = 5000  # entries (files and folders) one listing takes
MAX_BYTES: Final = 2 * 1024 * 1024 * 1024  # importable files of one import, by listed size
DOWNLOAD_SLOTS: Final = 4  # downloads at a time in one import
EXCLUDE_MAX: Final = MAX_FILES  # ``DropboxLinkIn.exclude`` (asserted in the tests)
PAPER_EXTENSION: Final = "paper"
# The Paper gate (DB10, task L0): ``True`` only once a live Paper export is recorded in ``tests/dropbox_support.py``
# (``PAPER_EXPORT_INFO`` and ``PAPER_EXPORT``). While it is ``False`` every ``.paper`` entry is ``skipped``.
PAPER_EXPORT_RECORDED: Final = False
NO_ACCESS: Final = "no_access"
UNKNOWN_ERROR: Final = "unknown_error"
CONVERT_FAILED: Final = "convert_failed"
READ_STEP: Final = "read"
DOWNLOAD_STEP: Final = "download"
CONVERT_STEP: Final = "convert"
CONVERT_PARSER: Final = "dropbox-convert"  # the parser of a Paper doc's exported Markdown

# The ``category`` of a listed file in the check (``DropboxCheckOut.files``): the shared file categories
# (``api.imports.files.CATEGORIES``) or a Paper doc.
Category = Literal["pdf", "word", "powerpoint", "excel", "html", "markdown", "csv", "text", "paper"]

__all__ = [
    "DOWNLOAD_SLOTS",
    "MAX_BYTES",
    "MAX_ENTRIES",
    "MAX_FILES",
    "MAX_OOXML_BYTES",
    "MAX_PDF_BYTES",
    "MAX_TEXT_BYTES",
    "CheckedFile",
    "DropboxImport",
    "DropboxImporter",
    "Inspection",
    "Resolved",
    "inspect",
    "resolve",
]


@dataclass(frozen=True)
class DropboxImport:
    """What to import: the parsed link and the ids (``id:...``) of the listed files the member unchecked."""

    target: DropboxTarget
    exclude: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class CheckedFile:
    """One importable file of the check: ``id`` (``id:...``), ``name``, ``path`` (the folders below the source root,
    joined with ``/``; ``""`` in the root) and its ``category``."""

    id: str
    name: str
    path: str
    category: Category


@dataclass(frozen=True)
class Inspection:
    """What a link would import, for the check: a folder tree or one file, its ``name``, the importable ``files`` (at
    most ``MAX_FILES``, by path then name; ``files_capped`` when more were found), the ``skipped`` files of other
    types or over their cap, the ``folders`` listed, the listed ``bytes`` of the importable files, and whether a cap
    ``capped`` the listing. ``path_display`` of the resolved target builds ``connections.link``."""

    kind: Literal["folder", "file"]
    name: str
    path_display: str
    files: list[CheckedFile]
    files_capped: bool
    skipped: int
    folders: int
    bytes: int
    capped: bool


@dataclass(frozen=True)
class Resolved:
    """The folder or file a link names, without any listing: ``kind``, ``id``, ``name``, ``path_display``."""

    kind: Literal["folder", "file"]
    id: str
    name: str
    path_display: str


# A connection that cannot be used ends the source ``no_access`` (and stops its other downloads at once).
_NO_ACCESS_CODES: Final = frozenset(
    {
        DropboxCode.NO_ACCESS,
        DropboxCode.LINK_INVALID,
        DropboxCode.NOT_CONNECTED,
        DropboxCode.RECONNECT_REQUIRED,
        DropboxCode.AUTH_FAILED,
        DropboxCode.ACCESS_REFUSED,
        DropboxCode.GRANT_INVALID,
    }
)
# Codes that end the whole import, not one item: the above, and a second ``invalid_root``.
_ABORT_CODES: Final = (_NO_ACCESS_CODES - {DropboxCode.NO_ACCESS}) | {DropboxCode.INVALID_ROOT}
_RELATIVE_ROOTS: Final = frozenset({"", "/"})

Lane = Literal["original", "paper", "skipped"]
Cap = Literal["files", "entries", "bytes"]


@dataclass(frozen=True)
class Found:
    """One file the listing found: where it is (``folder`` inside the source), how it is handled and its limits."""

    entry: DropboxEntry
    folder: str
    lane: Lane
    category: Category = "text"
    media_type: str = ""
    cap: int = 0

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.entry.name}" if self.folder else self.entry.name


@dataclass
class Scan:
    """The result of one listing. ``files`` are the importable files (at most ``MAX_FILES``), ``skipped`` the files of
    other types or over their cap; ``bytes`` is the listed size of ``files``."""

    root: DropboxEntry
    depth: int = 0  # folders in the path of the root: the entries below it are named by what follows
    files: list[Found] = field(default_factory=list)
    skipped: list[Found] = field(default_factory=list)
    folders: int = 0
    entries: int = 0
    bytes: int = 0
    excluded: int = 0  # importable files the member unchecked: they count toward the caps (``_place``)
    excluded_bytes: int = 0
    cap: Cap | None = None  # the cap that ended the listing


def classify(entry: DropboxEntry, folder: str) -> Found:
    """The lane of one file by its extension, listed size and downloadability (docs/dropbox/source.md "What is
    imported")."""

    extension = extension_of(entry.name)
    if extension == PAPER_EXTENSION:
        exportable = PAPER_EXPORT_RECORDED and not entry.is_downloadable and EXPORT_FORMAT in entry.export_options
        if not exportable:
            return Found(entry, folder, "skipped")
        return Found(entry, folder, "paper", "paper", MARKDOWN_MEDIA_TYPE, MAX_EXPORT_BYTES)
    lane = None if not entry.is_downloadable else classify_file(extension, entry.size)  # the shared lane table (DB18)
    if lane is None:
        return Found(entry, folder, "skipped")
    return Found(entry, folder, "original", lane.category, lane.media_type, lane.cap)


def _modified(entry: DropboxEntry) -> datetime | None:
    """The ``server_modified`` instant of an entry (the re-add skip compares it); ``None`` when it has none."""

    try:
        return None if entry.server_modified is None else datetime.fromisoformat(entry.server_modified)
    except ValueError:
        return None


class _Budget(WaitBudget):
    """The one wait budget of an import (DB14), which remembers that it refused a wait: from then on a failed call is
    "throttled past the budget" (the source ends ``unknown_error``), not one file's own failure."""

    def __init__(self) -> None:
        super().__init__()
        self.exhausted = False

    @override
    def take(self, wanted: float) -> float | None:
        granted = super().take(wanted)
        self.exhausted = self.exhausted or granted is None
        return granted


class _Reader:
    """The reads of one run as the bound member: the content calls, the token (one refresh on a 401), the root
    namespace of the member (``Dropbox-API-Path-Root``, DB6; read once, once more after a ``422 invalid_root``) and the
    wait budget of the run."""

    def __init__(self, connections: DropboxConnections, user_id: uuid.UUID) -> None:
        self._connections = connections
        self._user_id = user_id
        self._reading = asyncio.Lock()
        self.content = DropboxContent(connections.client)
        self.budget = _Budget()
        self.root: str | None = None

    async def start(self) -> None:
        """Read the root namespace of the member."""

        self.root = await self._root()

    async def _root(self) -> str | None:
        account = await self._connections.call(self._user_id, self.content.account_root)
        return account.root_namespace_id

    async def run[T](self, request: Callable[[SecretStr, str | None], Awaitable[T]]) -> T:
        """``request(token, path_root)``; a ``422 invalid_root`` reads the root once more and asks once more."""

        used = self.root
        try:
            return await self._connections.call(self._user_id, lambda token: request(token, used))
        except DropboxError as error:
            if error.code is not DropboxCode.INVALID_ROOT:
                raise
        async with self._reading:  # one read for the downloads that were refused together
            if self.root == used:
                self.root = await self._root()
        current = self.root
        return await self._connections.call(self._user_id, lambda token: request(token, current))


def _target_error(error: DropboxError) -> DropboxError:
    """The refusal of a read of the link's target (or of its listing) as the code of the check: a password link, a
    malformed one or a link type Dropbox does not resolve (``unsupported_link_type``) is ``dropbox_link_invalid``; an
    expired listing cursor (``reset``) is ``dropbox_unreachable`` (try again); any other refused, unsupported or
    unreadable answer about the target is ``dropbox_no_access``. Never ``dropbox_auth_failed``: that is a setup fault,
    not the member's link. Codes of the connection and the network pass unchanged."""

    tags = set(error.tags) if isinstance(error, DropboxApiError) else set()
    code = error.code
    if code is DropboxCode.RESTRICTED and "shared_link_access_denied" in tags:
        return DropboxError(DropboxCode.LINK_INVALID)
    if code is DropboxCode.UNSUPPORTED and "unsupported_link_type" in tags:
        return DropboxError(DropboxCode.LINK_INVALID)
    if code is DropboxCode.REFUSED and tags & {"malformed_path", "shared_link_malformed"}:
        return DropboxError(DropboxCode.LINK_INVALID)
    if code is DropboxCode.REFUSED and "reset" in tags:
        return DropboxError(DropboxCode.UNREACHABLE)
    if code in (DropboxCode.NOT_FOUND, DropboxCode.RESTRICTED, DropboxCode.UNSUPPORTED, DropboxCode.REFUSED):
        return DropboxError(DropboxCode.NO_ACCESS)
    return error


async def _named(reader: _Reader, target: DropboxTarget) -> DropboxEntry:
    content, budget = reader.content, reader.budget
    if target.kind == "shared" and target.shared_link is not None:
        link = target.shared_link
        return await reader.run(lambda token, root: content.shared_link(token, link, path_root=root, budget=budget))
    if target.path is not None:
        path = target.path
        return await reader.run(lambda token, root: content.metadata(token, path, path_root=root, budget=budget))
    raise DropboxError(DropboxCode.LINK_INVALID)


def _in_dropbox(entry: DropboxEntry) -> DropboxEntry:
    """The entry when it is in the member's Dropbox and is not the Dropbox root; else the refusal."""

    if entry.path_lower is None:
        raise DropboxError(DropboxCode.NO_ACCESS)
    if entry.path_lower in _RELATIVE_ROOTS:
        raise DropboxError(DropboxCode.LINK_INVALID)
    return entry


async def _resolve(reader: _Reader, target: DropboxTarget) -> DropboxEntry:
    """The folder or file a link names, as the listing sees it. It must be in the member's Dropbox (``path_lower``,
    else ``dropbox_no_access``) and not the Dropbox root. A shared link is read once more by id: the answer of the
    link has no ``path_display`` and no export information."""

    content, budget = reader.content, reader.budget
    try:
        entry = _in_dropbox(await _named(reader, target))
        if target.kind == "shared":
            entry_id = entry.id
            entry = await reader.run(
                lambda token, root: content.metadata(token, entry_id, path_root=root, budget=budget)
            )
    except DropboxError as error:
        raise _target_error(error) from None
    return entry


def _relative(scan: Scan, entry: DropboxEntry) -> str | None:
    """The path of a listed entry below the source root; ``None`` for the root itself (or an entry without a path)."""

    path = entry.path_display or entry.path_lower
    parts = (path or "").split("/")[scan.depth + 1 :]
    return "/".join(parts) if parts else None


def _place(scan: Scan, entry: DropboxEntry, folder: str, *, excluded: bool = False) -> Cap | None:
    """Sort one file into ``scan``; the cap that stops the listing instead (the file is then not taken). A file the
    member unchecked (``excluded``) still counts toward the files and bytes caps, so the import stops where the check
    stopped and never takes a file the check did not show; it is neither declared nor in ``files``."""

    found = classify(entry, folder)
    if found.lane == "skipped":
        if not excluded:
            scan.skipped.append(found)
        return None
    if len(scan.files) + scan.excluded >= MAX_FILES:
        return "files"
    if scan.bytes + scan.excluded_bytes + entry.size > MAX_BYTES:
        return "bytes"
    if excluded:
        scan.excluded += 1
        scan.excluded_bytes += entry.size
    else:
        scan.files.append(found)
        scan.bytes += entry.size
    return None


def _take(scan: Scan, entries: list[DropboxEntry], exclude: frozenset[str]) -> None:
    """Sort the entries of one page into ``scan``, until a cap ends the listing (``scan.cap``)."""

    for entry in entries:
        relative = _relative(scan, entry)
        if entry.kind == "deleted" or relative is None:
            continue
        if scan.entries >= MAX_ENTRIES:
            scan.cap = "entries"
            return
        if entry.kind == "file":
            scan.cap = _place(scan, entry, relative.rpartition("/")[0], excluded=entry.id in exclude)
            if scan.cap is not None:
                return
        scan.entries += 1
        if entry.kind == "folder":
            scan.folders += 1


async def _more(reader: _Reader, cursor: str) -> FolderPage:
    content, budget = reader.content, reader.budget
    return await reader.run(
        lambda token, root: content.list_folder_continue(token, cursor, path_root=root, budget=budget)
    )


async def _listing(reader: _Reader, scan: Scan, exclude: frozenset[str]) -> None:
    content, budget, path = reader.content, reader.budget, scan.root.path_lower or ""
    page = await reader.run(lambda token, root: content.list_folder(token, path, path_root=root, budget=budget))
    while True:
        _take(scan, page.entries, exclude)
        if scan.cap is not None or not page.has_more or page.cursor is None:
            return
        page = await _more(reader, page.cursor)


async def _scan(reader: _Reader, root: DropboxEntry, exclude: frozenset[str]) -> Scan:
    """List the tree under ``root`` (one recursive listing) and sort what is in it, to the caps."""

    scan = Scan(root, depth=len((root.path_lower or "").strip("/").split("/")))
    if root.kind == "file":
        scan.cap = _place(scan, root, "", excluded=root.id in exclude)
        scan.entries = 1
        return scan
    try:
        await _listing(reader, scan, exclude)
    except DropboxError as error:
        raise _target_error(error) from None
    return scan


def _failure(error: DropboxError) -> ImportFailedError:
    return ImportFailedError(NO_ACCESS if error.code in _NO_ACCESS_CODES else UNKNOWN_ERROR)


def _log_cap(scan: Scan, *, project_id: str | None = None, user_id: str | None = None) -> None:
    if scan.cap is not None:
        log_event(
            logger,
            WARNING,
            "api.imports.dropbox_cap_reached",
            project_id=project_id,
            user_id=user_id,
            cap=scan.cap,
            files=len(scan.files),
            entries=scan.entries,
            bytes=scan.bytes,
        )


def _kind(entry: DropboxEntry) -> Literal["folder", "file"]:
    return "file" if entry.kind == "file" else "folder"


def _display(entry: DropboxEntry) -> str:
    return entry.path_display or entry.path_lower or ""


async def resolve(connections: DropboxConnections, user_id: uuid.UUID, source: DropboxImport) -> Resolved:
    """Resolve the link as ``user_id`` (the bound member): the root namespace (Path-Root, DB6) and one metadata read.
    ``DropboxError``: ``dropbox_link_invalid`` (a password link, the root), ``dropbox_no_access`` (not readable, or
    not in the member's Dropbox: no ``path_lower``), ``dropbox_unreachable``, ``dropbox_reconnect_required``."""

    reader = _Reader(connections, user_id)
    await reader.start()
    root = await _resolve(reader, source.target)
    return Resolved(_kind(root), root.id, root.name, _display(root))


async def inspect(connections: DropboxConnections, user_id: uuid.UUID, source: DropboxImport) -> Inspection:
    """Resolve the link and list the tree as ``user_id`` without downloading: the numbers and the file list of the
    check. The same refusals as ``resolve``."""

    reader = _Reader(connections, user_id)
    await reader.start()
    root = await _resolve(reader, source.target)
    scan = await _scan(reader, root, frozenset())  # the check lists every file: the member unchecks in this list
    _log_cap(scan, user_id=str(user_id))
    files = sorted(scan.files, key=lambda f: (f.folder.casefold(), f.entry.name.casefold()))
    return Inspection(
        kind=_kind(root),
        name=root.name,
        path_display=_display(root),
        files=[CheckedFile(f.entry.id, f.entry.name, f.folder, f.category) for f in files],
        files_capped=scan.cap == "files",
        skipped=len(scan.skipped),
        folders=scan.folders,
        bytes=scan.bytes,
        capped=scan.cap is not None,
    )


class DropboxImporter:
    """An ``Importer`` (``api.imports.runner``) of one Dropbox link."""

    def __init__(self, connections: DropboxConnections, source: DropboxImport) -> None:
        self._connections = connections
        self._source = source

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        """List, declare, then import each file in its lane (module docstring). ``ImportFailedError('no_access')`` when
        the member cannot read the target or the connection expired; ``unknown_error`` when Dropbox is down or
        throttled past the wait budget (DB14). Item failures: ``no_access``/``read`` (409 ``not_found``,
        ``restricted_content``), ``unknown_error``/``download`` (retries exhausted), ``convert_failed``/``download``
        (over its cap while streaming)."""

        reader = _Reader(self._connections, job.user_id)
        try:
            await reader.start()
            root = await _resolve(reader, self._source.target)
            scan = await _scan(reader, root, self._source.exclude)
        except DropboxError as error:
            raise _failure(error) from None
        _log_cap(scan, project_id=str(job.project_id))
        for found in (*scan.files, *scan.skipped):
            await writer.declare(
                found.entry.id,
                found.path,
                media_type=found.media_type or MARKDOWN_MEDIA_TYPE,
                size_bytes=found.entry.size,
                modified_time=_modified(found.entry),
            )
        for found in scan.skipped:
            await writer.skip(found.entry.id, found.path)
        run = _Run(reader, writer)
        await run.import_all(scan.files)
        log_event(
            logger,
            INFO,
            "api.imports.dropbox_finished",
            project_id=str(job.project_id),
            added=run.added,
            duplicates=run.duplicates,
            failed=run.failed,
            skipped=len(scan.skipped),
            folders=scan.folders,
        )


class _Run(ImportRun[_Reader, ImportWriter, Found]):
    """The files of one import: at most ``DOWNLOAD_SLOTS`` at a time, and what came of them."""

    slots = DOWNLOAD_SLOTS

    @override
    async def _one(self, found: Found) -> None:
        try:
            outcome = await self._lane(found)
        except OriginalTooLargeError:
            await self._fail(found, CONVERT_FAILED, DOWNLOAD_STEP)
        except ExportTooLargeError:
            await self._fail(found, CONVERT_FAILED, CONVERT_STEP)
        except DropboxError as error:
            await self._refused(found, error)
        else:
            if outcome == "added":
                self.added += 1
            else:
                self.duplicates += 1

    async def _fail(self, found: Found, reason: str, step: str) -> None:
        self.failed += 1
        await self._writer.fail(found.entry.id, found.path, reason=reason, step=step)

    async def _refused(self, found: Found, error: DropboxError) -> None:
        """One file's refusal: it ends the import (see ``_ABORT_CODES``, or throttled past the budget) or only the
        item."""

        code = error.code
        if code in _ABORT_CODES or (code is DropboxCode.UNREACHABLE and self._reader.budget.exhausted):
            raise _failure(error) from None
        if code is DropboxCode.UNSUPPORTED and found.lane == "original":
            await self._writer.skip(found.entry.id, found.path)  # Dropbox cannot download it: not an error
        elif code in (DropboxCode.NOT_FOUND, DropboxCode.RESTRICTED):
            await self._fail(found, NO_ACCESS, READ_STEP)
        elif found.lane == "paper" and code in (DropboxCode.UNSUPPORTED, DropboxCode.REFUSED):
            await self._fail(found, CONVERT_FAILED, CONVERT_STEP)
        else:
            await self._fail(found, UNKNOWN_ERROR, DOWNLOAD_STEP)

    async def _lane(self, found: Found) -> Literal["added", "duplicate"]:
        if await self._stored(found):
            return "duplicate"
        return await (self._paper(found) if found.lane == "paper" else self._original(found))

    async def _stored(self, found: Found) -> bool:
        """The re-add skip (DB12): the file is linked ``duplicate`` to the document it already has, with no download."""

        modified = _modified(found.entry)
        if modified is None:
            return False
        return await self._writer.link_stored(
            found.entry.id, found.path, size_bytes=found.entry.size, modified_time=modified
        )

    async def _original(self, found: Found) -> Literal["added", "duplicate"]:
        content, budget, file_id = self._reader.content, self._reader.budget, found.entry.id

        async def store(token: SecretStr, root: str | None) -> Literal["added", "duplicate"]:
            async with content.download(token, file_id, path_root=root, budget=budget) as chunks:
                return await self._writer.add_file(
                    file_id, name=found.path, chunks=chunks, media_type=found.media_type, max_bytes=found.cap
                )

        return await self._reader.run(store)

    async def _paper(self, found: Found) -> Literal["added", "duplicate"]:
        content, budget, file_id = self._reader.content, self._reader.budget, found.entry.id
        text = await self._reader.run(
            lambda token, root: content.export(token, file_id, max_bytes=found.cap, path_root=root, budget=budget)
        )
        return await self._writer.add(
            file_id,
            name=found.path,
            original=text.encode(),
            markdown=text,
            original_media_type=MARKDOWN_MEDIA_TYPE,
        )
