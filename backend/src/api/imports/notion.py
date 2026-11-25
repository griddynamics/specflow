"""The Notion importer (docs/notion/source.md): a page and its sub-pages, or a database and its rows, as documents.
Stream D1 fills ``inspect`` and ``NotionImporter.__call__``; the types and the caps are final.

Runs in the API process as the member the project's Notion is connected by (``connections.gateway(job.user_id)``). A
page is read as Markdown (N3: ``NotionGateway.page_markdown``, converted by ``notion.convert``); the ``<page>`` lines
it holds are followed, and so are the rows of the ``<database>`` lines (``query_data_source``, a row's document is its
properties table and its body). A visited set and ``MAX_PAGES`` (pages and rows together, N4) bound the walk; a cap
that is hit is logged (counts only). A page the member cannot see is an item ``no_access`` (step ``read``); a link
that cannot be read at all fails the source ``no_access``; Notion down or throttled is ``unknown_error``; a page too
deep to convert is an item ``convert_failed`` (step ``convert``) (N6). The original stored per page is JSON ``{page:
{id, url, title, last_edited_time}, markdown: scrub_original(nfm)}`` (N5): never Notion's ``properties``,
``created_by`` or ``last_edited_by``, which carry people and e-mails. The probe order of a link is page first, then
database when the page read says ``notion_parent_not_a_page``. Titles and text live in documents only: events carry
ids, kinds and counts."""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from api.imports.model import ImportFailedError, ImportJob
from api.imports.writer import ImportWriter
from logger.logger import WARNING, get_logger, log_event
from notion import convert
from notion.convert import ConvertedPage, MarkupTooDeepError
from notion.gateway import NotionGateway
from notion.links import NotionLink, dashed, parse_link
from notion.model import NotionPage
from notion.reasons import NotionCode, NotionError

logger = get_logger("api.imports.notion")

MAX_PAGES: Final = 100  # pages and database rows of one import, together
NO_ACCESS: Final = "no_access"
CONVERT_FAILED: Final = "convert_failed"
UNKNOWN_ERROR: Final = "unknown_error"
READ_STEP: Final = "read"
CONVERT_STEP: Final = "convert"
UNTITLED: Final = "Untitled"
# What a member not seeing an item means: that item is skipped. Everything else a read raises stops the source.
_SKIPPABLE: Final = frozenset({NotionCode.NOT_SHARED, NotionCode.PAGE_GONE, NotionCode.DATABASE_GONE})
_NO_ACCESS_CODES: Final = _SKIPPABLE | {
    NotionCode.NOT_CONNECTED,
    NotionCode.RECONNECT_REQUIRED,
    NotionCode.LINK_INVALID,
}
_HEX32: Final = re.compile(r"^[0-9a-fA-F]{32}$")


class Connections(Protocol):
    """The part of ``notion.connections.NotionConnections`` the importer uses."""

    async def gateway(self, user_id: uuid.UUID) -> NotionGateway: ...


@dataclass(frozen=True)
class NotionImport:
    """What to import: the object a pasted link names."""

    link: NotionLink


@dataclass(frozen=True)
class Inspection:
    """What a link would import, for the check: a page with its sub-pages or a database with its rows, its title, how
    many pages (at most ``MAX_PAGES``; ``capped`` when there are more)."""

    kind: Literal["page", "database"]
    title: str
    pages: int
    capped: bool


@dataclass(frozen=True)
class Converter:
    """The three functions of ``notion.convert`` the import calls; a test hands in its own."""

    to_markdown: Callable[[str], ConvertedPage]
    render_row: Callable[[str, Mapping[str, Any], ConvertedPage], str]
    scrub_original: Callable[[str], str]


def default_converter() -> Converter:
    """``notion.convert`` as it is now (looked up on each call, so a replaced function is used)."""

    return Converter(convert.to_markdown, convert.render_row, convert.scrub_original)


def import_failure(error: NotionError) -> ImportFailedError:
    """The closed failure of a source for a Notion refusal: what the member cannot reach is ``no_access``, anything
    else (Notion down, throttled, the server's credentials refused) is ``unknown_error``."""

    return ImportFailedError(NO_ACCESS if error.code in _NO_ACCESS_CODES else UNKNOWN_ERROR)


# The walk ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Found:
    """An item the walk will read (declared, so the progress shows its total early)."""

    external_id: str
    name: str


@dataclass(frozen=True)
class _Doc:
    """A page or row read: its Notion Markdown, its document and the fields kept of it."""

    external_id: str
    name: str
    page: dict[str, Any]  # id, url, title, last_edited_time: all that is ever stored of the object
    nfm: str
    markdown: str


@dataclass(frozen=True)
class _Skipped:
    external_id: str
    name: str
    reason: str
    step: str


_Event = _Found | _Doc | _Skipped


@dataclass(frozen=True)
class _Page:
    id: str
    title: str
    url: str


@dataclass(frozen=True)
class _Row:
    """A row of a data source: its document is its properties table and its body."""

    page: NotionPage


@dataclass(frozen=True)
class _DatabaseRef:
    """A ``<database>`` line without a ``data-source-url``: its data sources are read from the database."""

    url: str
    title: str


@dataclass(frozen=True)
class _Rows:
    data_source_id: str
    title: str
    root: bool


def _key(object_id: str) -> str:
    plain = object_id.replace("-", "")
    return dashed(plain) if _HEX32.match(plain) else object_id.lower()


def _page_record(page: NotionPage | _Page) -> dict[str, Any]:
    """The fields stored of a page. A sub-page has no ``last_edited_time`` (null on purpose): reading it would cost one
    more request per page and the import runs once (D7)."""

    edited = getattr(page, "last_edited_time", None)
    return {"id": page.id, "url": page.url, "title": page.title, "last_edited_time": edited and edited.isoformat()}


class _Walk:
    """The pages and rows of one link in the order they are read: the target, then what it points at, breadth first.
    ``seen`` holds every id taken (at most ``MAX_PAGES``); ``capped`` is true once something was left out."""

    def __init__(self, gateway: NotionGateway, converter: Converter) -> None:
        self._gateway = gateway
        self._converter = converter
        self._queue: deque[_Page | _Rows | _Row | _DatabaseRef | NotionPage] = deque()
        self._seen: set[str] = set()
        self._sources: set[str] = set()
        self.root_sources: tuple[str, ...] = ()  # data sources of a target database
        self._root: str | None = None  # the target page: when it cannot be read the source fails
        self.capped = False

    @property
    def taken(self) -> int:
        return len(self._seen)

    async def open(self, link: NotionLink) -> tuple[Literal["page", "database"], str]:
        """Read the target: a page first, a database when the page read says it is one. Returns its kind and title;
        ``NotionError`` when it cannot be read (a trashed target is as good as not shared)."""

        try:
            page = await self._gateway.page(link.id)
        except NotionError as error:
            if error.code is not NotionCode.NOT_A_PAGE:
                raise
        else:
            if page.in_trash:
                raise NotionError(NotionCode.NOT_SHARED)
            self._root = _key(page.id)
            self._seen.add(self._root)
            self._queue.append(page)
            return "page", page.title
        database = await self._gateway.database(link.id)
        if database.in_trash:
            raise NotionError(NotionCode.NOT_SHARED)
        self.root_sources = tuple(source.id for source in database.data_sources)
        for source in database.data_sources:
            if _key(source.id) not in self._sources:
                self._sources.add(_key(source.id))
                self._queue.append(_Rows(source.id, database.title, root=True))
        return "database", database.title

    async def events(self) -> AsyncIterator[_Event]:
        while self._queue:
            item = self._queue.popleft()
            if isinstance(item, _Rows):
                async for event in self._rows(item):
                    yield event
            elif isinstance(item, _DatabaseRef):
                async for event in self._database(item):
                    yield event
            else:
                async for event in self._read(item):
                    yield event

    def _take(self, object_id: str) -> bool:
        """Claim an id for this import: false when it was claimed already or the cap is reached."""

        key = _key(object_id)
        if key in self._seen:
            return False
        if len(self._seen) >= MAX_PAGES:
            self.capped = True
            return False
        self._seen.add(key)
        return True

    async def _rows(self, source: _Rows) -> AsyncIterator[_Event]:
        cursor: str | None = None
        try:
            while True:
                found = await self._gateway.query_data_source(source.data_source_id, start_cursor=cursor)
                for row in found.rows:
                    if self._take(row.id):
                        yield _Found(f"page:{row.id}", row.title or UNTITLED)
                        self._queue.append(_Row(row))
                cursor = found.next_cursor
                if cursor is None or self.capped:
                    return
        except NotionError as error:
            if source.root or error.code not in _SKIPPABLE:
                raise
            yield _Skipped(f"database:{source.data_source_id}", source.title or UNTITLED, NO_ACCESS, READ_STEP)

    async def _database(self, ref: _DatabaseRef) -> AsyncIterator[_Event]:
        """The data sources of a database the Markdown names without one: queued as rows to read."""

        try:
            database = await self._gateway.database(parse_link(ref.url).id)
        except NotionError as error:
            if error.code not in _SKIPPABLE | {NotionCode.LINK_INVALID}:
                raise
            yield _Skipped(f"database:{_key(ref.url)}", ref.title or UNTITLED, NO_ACCESS, READ_STEP)
            return
        if database.in_trash:
            yield _Skipped(f"database:{database.id}", ref.title or UNTITLED, NO_ACCESS, READ_STEP)
            return
        for source in database.data_sources:
            if _key(source.id) not in self._sources:
                self._sources.add(_key(source.id))
                self._queue.append(_Rows(source.id, ref.title or database.title, root=False))

    async def _read(self, item: _Page | _Row | NotionPage) -> AsyncIterator[_Event]:
        row = item.page if isinstance(item, _Row) else None
        item = row or item
        title = item.title or UNTITLED
        external_id = f"page:{item.id}"
        try:
            nfm = (await self._gateway.page_markdown(item.id)).markdown
        except NotionError as error:
            if error.code not in _SKIPPABLE or _key(item.id) == self._root:
                raise
            yield _Skipped(external_id, title, NO_ACCESS, READ_STEP)
            return
        try:
            converted = self._converter.to_markdown(nfm)
            markdown = (
                self._converter.render_row(title, row.properties, converted)
                if row is not None
                else f"# {title}\n\n{converted.markdown}".rstrip() + "\n"
            )
        except MarkupTooDeepError:  # one page nested too deep to convert: its item is an error, the walk goes on
            yield _Skipped(external_id, title, CONVERT_FAILED, CONVERT_STEP)
            return
        yield _Doc(external_id, title, _page_record(item), nfm, markdown)
        for ref in converted.sub_pages:
            if self._take(ref.id):
                yield _Found(f"page:{ref.id}", ref.title or UNTITLED)
                self._queue.append(_Page(ref.id, ref.title, ref.url))
        for database in converted.databases:
            if database.data_source_id is None:
                self._queue.append(_DatabaseRef(database.url, database.title))
            elif _key(database.data_source_id) not in self._sources:
                self._sources.add(_key(database.data_source_id))
                self._queue.append(_Rows(database.data_source_id, database.title, root=False))


async def open_link(gateway: NotionGateway, source: NotionImport) -> tuple[Literal["page", "database"], str]:
    """What the link names and its title, from one read (two for a database): a page first, then a database.
    ``NotionError`` when it cannot be read."""

    return await _Walk(gateway, default_converter()).open(source.link)


async def inspect(gateway: NotionGateway, source: NotionImport) -> Inspection:
    """Probe the target for the check, cheaply: a page is one ``page`` and one ``page_markdown`` (the page, its
    ``<page>`` and ``<database>`` lines; ``capped`` when a database line may hold more), a database is one page of rows
    (``capped`` when there is another). The count is a lower bound; the import walks the tree once. ``NotionError``
    when a read is refused (``notion_not_shared`` for a page or database the member cannot see)."""

    walk = _Walk(gateway, default_converter())
    kind, title = await walk.open(source.link)
    if kind == "database":
        if not walk.root_sources:
            return Inspection(kind, title, 0, False)
        found = await gateway.query_data_source(walk.root_sources[0])
        return Inspection(
            kind, title, min(len(found.rows), MAX_PAGES), found.next_cursor is not None or len(found.rows) > MAX_PAGES
        )
    found_pages, found_databases = 0, 0
    try:
        converted = default_converter().to_markdown((await gateway.page_markdown(source.link.id)).markdown)
        found_pages, found_databases = len(converted.sub_pages), len(converted.databases)
    except MarkupTooDeepError:  # the page is one item in error in the import; its links are not followed
        pass
    total = 1 + found_pages + found_databases
    return Inspection(kind, title, min(total, MAX_PAGES), total > MAX_PAGES or found_databases > 0)


class NotionImporter:
    """An ``Importer`` (``api.imports.runner``) of one Notion link."""

    def __init__(self, connections: Connections, source: NotionImport, converter: Converter | None = None) -> None:
        self._connections = connections
        self._source = source
        self._converter = converter or default_converter()

    async def __call__(self, job: ImportJob, writer: ImportWriter) -> None:
        try:
            walk = _Walk(await self._connections.gateway(job.user_id), self._converter)
            await walk.open(self._source.link)
            async for event in walk.events():
                await self._store(writer, event)
        except NotionError as error:
            raise import_failure(error) from None
        if walk.capped:
            log_event(
                logger,
                WARNING,
                "api.imports.page_cap_reached",
                project_id=str(job.project_id),
                cap=MAX_PAGES,
                taken=walk.taken,
            )

    async def _store(self, writer: ImportWriter, event: _Event) -> None:
        if isinstance(event, _Found):
            await writer.declare(event.external_id, event.name)
        elif isinstance(event, _Skipped):
            await writer.fail(event.external_id, event.name, reason=event.reason, step=event.step)
        else:
            try:
                scrubbed = self._converter.scrub_original(event.nfm)
            except MarkupTooDeepError:
                await writer.fail(event.external_id, event.name, reason=CONVERT_FAILED, step=CONVERT_STEP)
                return
            original = json.dumps({"page": event.page, "markdown": scrubbed}, sort_keys=True).encode()
            await writer.add(event.external_id, name=event.name, original=original, markdown=event.markdown)
