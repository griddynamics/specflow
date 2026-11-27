"""The content reads of the Box link importer (docs/box/source.md), on the one read-only transport of ``box.client``.

Each method is one documented Box call below ``api.box.com/2.0`` made with the member's access token and, for an item
reached through a shared link, the ``boxapi: shared_link=...`` header. Nothing here stores, logs or raises a name, link,
token or Box text: a refusal is a ``BoxError`` with a closed code (``box_no_access`` for 403 and 404; the transport
raises ``box_unreachable`` and ``box_token_refused``). The caller (``box.connections.BoxConnections.call``) supplies a
fresh token and repeats once after ``box_token_refused``.

Tested in ``tests/test_box_content.py``, over ``tests/box_support.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final, Literal, override

from pydantic import SecretStr

from box.client import API_URL, BoxClient, Response, WaitBudget, body_of, chunks_of
from box.codes import BoxCode, BoxError

PAGE_LIMIT: Final = 1000  # the most Box answers per page of ``GET /folders/{id}/items``
ITEM_FIELDS: Final = "id,type,name,size,extension,item_status"  # ``fields=``: Box then adds only the mini fields
DOWNLOAD_CHUNK: Final = 1024 * 1024  # bytes per chunk of a download stream
EXTRACTED_TEXT_HINT: Final = "[extracted_text]"  # the ``x-rep-hints`` value for a Box Note
REPRESENTATION: Final = "extracted_text"
REPRESENTATION_POLLS: Final = 3  # asks after the first one for a representation that is not ready
POLL_SECONDS: Final = 10.0  # between two asks: 3 polls take 30 seconds

ItemKind = Literal["file", "folder", "web_link"]


@dataclass(frozen=True)
class BoxItem:
    """One file, folder or web link as the importer needs it. ``size`` is in bytes (0 for a folder or web link);
    ``extension`` is lower case without the dot (``None`` when the name has none). ``active`` is false for an item in
    the trash. The name is shown to members and stored in documents only; it never reaches a log."""

    kind: ItemKind
    id: str
    name: str
    size: int = 0
    extension: str | None = None
    active: bool = True

    @override
    def __repr__(self) -> str:
        return f"BoxItem(kind={self.kind!r}, id={self.id!r})"  # never the name


@dataclass(frozen=True)
class ItemsPage:
    """One page of a folder's items (marker paging). ``next_marker`` is ``None`` on the last page."""

    entries: list[BoxItem]
    next_marker: str | None


def _id_of(value: Any) -> str | None:
    return value if isinstance(value, str) and value else (str(value) if isinstance(value, int) else None)


def _extension(name: str, given: Any) -> str | None:
    if isinstance(given, str) and given:
        return given.lower().lstrip(".")
    dot = name.rfind(".")
    return name[dot + 1 :].lower() if dot > 0 and dot < len(name) - 1 else None


def item_of(entry: Any) -> BoxItem:
    """A ``BoxItem`` of one Box entry; ``BoxError(box_refused)`` when it has no usable id or name. A type that is not a
    file or folder (a web link, an app item) is ``web_link``: it holds no content."""

    if not isinstance(entry, dict):
        raise BoxError(BoxCode.REFUSED)
    item_id, name = _id_of(entry.get("id")), entry.get("name")
    if item_id is None or not isinstance(name, str):
        raise BoxError(BoxCode.REFUSED)
    kind: ItemKind = (
        "file" if entry.get("type") == "file" else "folder" if entry.get("type") == "folder" else "web_link"
    )
    size = entry.get("size")
    return BoxItem(
        kind=kind,
        id=item_id,
        name=name,
        size=size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 0,
        extension=_extension(name, entry.get("extension")) if kind == "file" else None,
        active=entry.get("item_status", "active") == "active",
    )


def _read(response: Response) -> dict[str, Any]:
    """The JSON object of a ``200``; 403, 404 and 410 are ``box_no_access``, any other answer is ``box_refused``."""

    if response.is_success:
        body = body_of(response)
        if body:
            return body
        raise BoxError(BoxCode.REFUSED)
    if response.is_client_error:
        raise BoxError(BoxCode.NO_ACCESS)
    raise BoxError(BoxCode.REFUSED)


def _api_path(url: Any) -> str:
    """The path below ``api.box.com/2.0`` of the ``info.url`` of a representation (an https address on that host)."""

    if isinstance(url, str) and url.startswith(f"{API_URL}/"):
        return url[len(API_URL) :].split("?", 1)[0]
    raise BoxError(BoxCode.REFUSED)


class BoxContent:
    """The content calls of one importer or check, over one ``BoxClient`` (the transport)."""

    def __init__(self, client: BoxClient) -> None:
        self._client = client

    async def folder(
        self, token: SecretStr, folder_id: str, *, shared_link: str | None = None, budget: WaitBudget | None = None
    ) -> BoxItem:
        """``GET /folders/{folder_id}``; 403 or 404 is ``box_no_access``."""

        response = await self._client.get(
            token, f"/folders/{folder_id}", {"fields": ITEM_FIELDS}, shared_link=shared_link, budget=budget
        )
        return item_of(_read(response))

    async def file(
        self, token: SecretStr, file_id: str, *, shared_link: str | None = None, budget: WaitBudget | None = None
    ) -> BoxItem:
        """``GET /files/{file_id}``; 403 or 404 is ``box_no_access``."""

        response = await self._client.get(
            token, f"/files/{file_id}", {"fields": ITEM_FIELDS}, shared_link=shared_link, budget=budget
        )
        return item_of(_read(response))

    async def shared_item(self, token: SecretStr, url: str, *, budget: WaitBudget | None = None) -> BoxItem:
        """``GET /shared_items`` with ``boxapi: shared_link=<url>``: the folder, file or web link a shared link names.
        403 and 404 are ``box_no_access``; a password-protected link is ``box_link_password``: an answer that says
        the link needs a password, or a link that has one set (how Box answers it without the password is unverified:
        docs/box/discovery.md "Open points")."""

        response = await self._client.get(
            token, "/shared_items", {"fields": f"{ITEM_FIELDS},shared_link"}, shared_link=url, budget=budget
        )
        if response.is_client_error and "password" in response.text.lower():
            raise BoxError(BoxCode.LINK_PROTECTED)
        body = _read(response)
        link = body.get("shared_link")
        if isinstance(link, dict) and link.get("is_password_enabled") is True:
            raise BoxError(BoxCode.LINK_PROTECTED)
        return item_of(body)

    async def folder_items(
        self,
        token: SecretStr,
        folder_id: str,
        *,
        marker: str | None = None,
        limit: int = PAGE_LIMIT,
        shared_link: str | None = None,
        budget: WaitBudget | None = None,
    ) -> ItemsPage:
        """One page of ``GET /folders/{folder_id}/items`` (``usemarker=true``, ``fields=ITEM_FIELDS``)."""

        params: dict[str, Any] = {"usemarker": "true", "limit": limit, "fields": ITEM_FIELDS}
        if marker:
            params["marker"] = marker
        response = await self._client.get(
            token, f"/folders/{folder_id}/items", params, shared_link=shared_link, budget=budget
        )
        body = _read(response)
        entries = body.get("entries")
        if not isinstance(entries, list):
            raise BoxError(BoxCode.REFUSED)
        following = body.get("next_marker")
        return ItemsPage(
            [item_of(entry) for entry in entries], following if isinstance(following, str) and following else None
        )

    @asynccontextmanager
    async def download(
        self, token: SecretStr, file_id: str, *, shared_link: str | None = None, budget: WaitBudget | None = None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        """``GET /files/{file_id}/content`` as a stream of chunks of at most ``DOWNLOAD_CHUNK`` bytes (the caller
        hashes and caps them while it reads). ``202`` waits as the transport does; 403 and 404 are ``box_no_access``."""

        async with self._client.stream(
            token, f"/files/{file_id}/content", shared_link=shared_link, budget=budget
        ) as response:
            yield chunks_of(response, DOWNLOAD_CHUNK)

    async def extracted_text(
        self,
        token: SecretStr,
        file_id: str,
        *,
        max_bytes: int,
        shared_link: str | None = None,
        budget: WaitBudget | None = None,
    ) -> str:
        """The ``[extracted_text]`` representation of a Box Note (``GET /files/{id}?fields=representations`` with
        ``x-rep-hints``, then its content URL); the text is cut to ``max_bytes``. ``none`` asks Box to generate it
        (``GET info.url``); ``pending`` is polled at most ``REPRESENTATION_POLLS`` times, ``POLL_SECONDS`` apart. A
        representation that is not ready then (or that failed) is ``box_unreachable``. The waits go through the client's
        sleep and are charged to ``budget`` (one that does not fit ends the read ``box_unreachable``). The text itself
        is read from the ``url_template`` host Box names, ``dl.boxcloud.com``, with the token (``stream_content``)."""

        budget = budget or WaitBudget()
        for poll in range(REPRESENTATION_POLLS + 1):
            if poll:
                await self._client.pause(POLL_SECONDS, budget, BoxCode.UNREACHABLE)
            entry = await self._representation(token, file_id, shared_link, budget)
            status = entry.get("status")
            state = status.get("state") if isinstance(status, dict) else None
            if state == "success":
                return await self._text(token, entry, max_bytes, budget)
            if state == "none":  # not generated yet: asking for its info starts it
                info = entry.get("info")
                await self._client.get(
                    token,
                    _api_path(info.get("url") if isinstance(info, dict) else None),
                    shared_link=shared_link,
                    budget=budget,
                )
            elif state != "pending":
                break
        raise BoxError(BoxCode.UNREACHABLE)

    async def _representation(
        self, token: SecretStr, file_id: str, shared_link: str | None, budget: WaitBudget | None
    ) -> dict[str, Any]:
        response = await self._client.get(
            token,
            f"/files/{file_id}",
            {"fields": "representations"},
            shared_link=shared_link,
            budget=budget,
            headers={"x-rep-hints": EXTRACTED_TEXT_HINT},
        )
        listed = _read(response).get("representations")
        entries = listed.get("entries") if isinstance(listed, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("representation") == REPRESENTATION:
                return entry
        raise BoxError(BoxCode.UNREACHABLE)  # Box offers no text for this file

    async def _text(
        self,
        token: SecretStr,
        entry: dict[str, Any],
        max_bytes: int,
        budget: WaitBudget,
    ) -> str:
        content = entry.get("content")
        template = content.get("url_template") if isinstance(content, dict) else None
        url = template.replace("{+asset_path}", "") if isinstance(template, str) else ""
        data = bytearray()
        try:  # whatever this host answers fails the item (``box_refused``), never the import
            async with self._client.stream_content(token, url, budget=budget) as response:
                async for chunk in chunks_of(response, DOWNLOAD_CHUNK):
                    data.extend(chunk)
                    if len(data) >= max_bytes:
                        break
        except BoxError as error:
            if error.code in (BoxCode.UNREACHABLE, BoxCode.NOT_READY, BoxCode.REFUSED):
                raise
            raise BoxError(BoxCode.REFUSED) from None
        return bytes(data[:max_bytes]).decode("utf-8", errors="replace")
