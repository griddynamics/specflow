"""The content reads of the Dropbox link importer (docs/dropbox/source.md): the root namespace, a shared link, a path,
the pages of a recursive listing, a file's bytes and a Paper export, all through ``dropbox.client.DropboxClient`` (the
allowlisted transport) as one user's token. No HTTP of its own; no log.

Every call takes ``path_root``, the member's root namespace id (``Dropbox-API-Path-Root``, DB6), read once per check and
per import with ``account_root``; a ``DropboxCode.INVALID_ROOT`` asks the caller to read it once more.

W0 froze ``DropboxEntry``, ``FolderPage``, ``entry_of`` and the signatures; stream S2 writes the bodies and
``tests/test_dropbox_content.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, override

from pydantic import SecretStr

from dropbox.client import (
    GET_METADATA,
    GET_SHARED_LINK_METADATA,
    LIST_FOLDER,
    LIST_FOLDER_CONTINUE,
    DropboxAccount,
    DropboxClient,
    WaitBudget,
)
from dropbox.codes import DropboxCode, DropboxError

LIST_LIMIT: Final = 2000  # ``limit`` of ``files/list_folder`` (approximate, Dropbox's own cap)
EXPORT_FORMAT: Final = "markdown"  # the one Paper export format the importer asks for (DB10)
MAX_EXPORT_BYTES: Final = 5 * 1024 * 1024  # the Markdown of one Paper doc

DOWNLOAD_CHUNK: Final = 1024 * 1024  # bytes per chunk of a download stream

EntryKind = Literal["file", "folder", "deleted"]


@dataclass(frozen=True)
class DropboxEntry:
    """One file or folder of a listing or a metadata answer. ``path_lower`` and ``path_display`` are ``None`` for an
    item that is not in the caller's Dropbox (a link of someone else, DB7). ``id`` is ``id:...`` (stable, case
    sensitive). ``is_downloadable`` false with ``export_options`` is a Paper doc (or another exportable file)."""

    kind: EntryKind
    id: str
    name: str
    path_lower: str | None
    path_display: str | None
    size: int = 0
    server_modified: str | None = None
    is_downloadable: bool = True
    export_as: str | None = None
    export_options: tuple[str, ...] = ()

    @override
    def __repr__(self) -> str:
        return f"DropboxEntry(kind={self.kind!r}, id={self.id!r})"  # never a name or a path


@dataclass(frozen=True)
class FolderPage:
    """One page of ``files/list_folder`` (or ``/continue``): its entries, and the cursor of the next page when
    ``has_more``. The cursor is never stored (D7, DB15)."""

    entries: list[DropboxEntry]
    cursor: str | None
    has_more: bool


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def entry_of(value: Any) -> DropboxEntry | None:
    """The entry of one metadata object (``.tag`` ``file``, ``folder`` or ``deleted``); ``None`` for a shape this code
    does not know (no ``.tag``, no ``id`` on a file or folder)."""

    if not isinstance(value, dict):
        return None
    tag = value.get(".tag")
    name = _text(value.get("name")) or ""
    if tag == "deleted":
        return DropboxEntry("deleted", "", name, _text(value.get("path_lower")), _text(value.get("path_display")))
    entry_id = _text(value.get("id"))
    if tag not in ("file", "folder") or entry_id is None:
        return None
    kind: EntryKind = "file" if tag == "file" else "folder"
    size = value.get("size")
    info = value.get("export_info")
    info = info if isinstance(info, dict) else {}
    options = info.get("export_options")
    downloadable = value.get("is_downloadable")
    return DropboxEntry(
        kind=kind,
        id=entry_id,
        name=name,
        path_lower=_text(value.get("path_lower")),
        path_display=_text(value.get("path_display")),
        size=size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0,
        server_modified=_text(value.get("server_modified")),
        is_downloadable=downloadable if isinstance(downloadable, bool) else True,
        export_as=_text(info.get("export_as")),
        export_options=tuple(o for o in options if isinstance(o, str)) if isinstance(options, list) else (),
    )


class ExportTooLargeError(Exception):
    """A Paper export past its cap (``MAX_EXPORT_BYTES``): the item ends ``convert_failed``/``convert``."""


def _known(answer: dict[str, Any]) -> DropboxEntry:
    """The file or folder of a metadata answer; ``DropboxError(dropbox_refused)`` for a shape this code does not know
    (a ``deleted`` entry or a link to something else)."""

    entry = entry_of(answer)
    if entry is None or entry.kind == "deleted":
        raise DropboxError(DropboxCode.REFUSED)
    return entry


def _page(answer: dict[str, Any]) -> FolderPage:
    """One page of a listing: its known entries (an entry of another shape is dropped) and the cursor."""

    listed = answer.get("entries")
    cursor = _text(answer.get("cursor"))
    if not isinstance(listed, list) or cursor is None:
        raise DropboxError(DropboxCode.REFUSED)
    entries = [entry for entry in map(entry_of, listed) if entry is not None]
    return FolderPage(entries, cursor, answer.get("has_more") is True)


class DropboxContent:
    """The reads of the importer over one ``DropboxClient``; the token is passed per call (``DropboxConnections.call``
    refreshes it on a 401)."""

    def __init__(self, client: DropboxClient) -> None:
        self._client = client

    async def account_root(self, token: SecretStr) -> DropboxAccount:
        """The account of the token, for its ``root_namespace_id`` (DB6)."""

        return await self._client.get_current_account(token)

    async def shared_link(
        self, token: SecretStr, url: str, *, path_root: str | None, budget: WaitBudget | None = None
    ) -> DropboxEntry:
        """``sharing/get_shared_link_metadata`` of a pasted shared link: the folder or file it names (``path_lower``
        only when it is in the member's Dropbox). A password link or a refused link is a ``DropboxApiError``."""

        answer = await self._client.rpc(
            token, GET_SHARED_LINK_METADATA, {"url": url}, path_root=path_root, budget=budget
        )
        return _known(answer)

    async def metadata(
        self, token: SecretStr, path: str, *, path_root: str | None, budget: WaitBudget | None = None
    ) -> DropboxEntry:
        """``files/get_metadata`` of a path (``/home/<path>`` links) or an ``id:``."""

        answer = await self._client.rpc(
            token, GET_METADATA, {"path": path, "include_deleted": False}, path_root=path_root, budget=budget
        )
        return _known(answer)

    async def list_folder(
        self, token: SecretStr, path: str, *, path_root: str | None, budget: WaitBudget | None = None
    ) -> FolderPage:
        """The first page of ``files/list_folder`` by path: ``recursive=true``, ``include_non_downloadable_files=true``,
        ``limit=LIST_LIMIT``."""

        body = {
            "path": path,
            "recursive": True,
            "include_deleted": False,
            "include_non_downloadable_files": True,
            "limit": LIST_LIMIT,
        }
        return _page(await self._client.rpc(token, LIST_FOLDER, body, path_root=path_root, budget=budget))

    async def list_folder_continue(
        self, token: SecretStr, cursor: str, *, path_root: str | None, budget: WaitBudget | None = None
    ) -> FolderPage:
        """The next page (``files/list_folder/continue``)."""

        answer = await self._client.rpc(
            token, LIST_FOLDER_CONTINUE, {"cursor": cursor}, path_root=path_root, budget=budget
        )
        return _page(answer)

    @asynccontextmanager
    async def _chunks(
        self, token: SecretStr, file_id: str, path_root: str | None, budget: WaitBudget | None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        async with self._client.download(token, {"path": file_id}, path_root=path_root, budget=budget) as opened:
            yield opened.chunks(DOWNLOAD_CHUNK)

    def download(
        self, token: SecretStr, file_id: str, *, path_root: str | None, budget: WaitBudget | None = None
    ) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        """The bytes of a file by ``id`` (``files/download``), in chunks; the answer is closed on exit. Dropbox always
        says ``application/octet-stream``: the format comes from the extension and the parse guard's magic bytes."""

        return self._chunks(token, file_id, path_root, budget)

    async def export(
        self,
        token: SecretStr,
        file_id: str,
        *,
        max_bytes: int = MAX_EXPORT_BYTES,
        path_root: str | None,
        budget: WaitBudget | None = None,
    ) -> str:
        """The Markdown of a Paper doc (``files/export``, ``export_format=EXPORT_FORMAT``), at most ``max_bytes``
        (``ExportTooLargeError`` beyond: this package never imports ``api``). Only for an entry whose
        ``export_options`` list ``markdown`` (DB10); the shape is unverified until L0 records a live export."""

        arg = {"path": file_id, "export_format": EXPORT_FORMAT}
        data = bytearray()
        async with self._client.export(token, arg, path_root=path_root, budget=budget) as opened:
            async for chunk in opened.chunks(DOWNLOAD_CHUNK):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ExportTooLargeError
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise DropboxError(DropboxCode.REFUSED) from None
