"""What a member shared with Graphico (docs/notion/source.md "Picking a page"): the pages and databases their connection
can read, for the pickers of the source and plan forms. Database rows and trashed objects are left out; the list is
sorted by title and capped at ``MAX_SHARED`` (``capped`` says so). Titles are scrubbed as the import scrubs them. Each
object comes with a link the source and plan routes parse (``notion.links.parse_link``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from notion.convert.scrub import scrub_text
from notion.gateway import NotionGateway
from notion.model import NotionDataSource, NotionPage, SearchKind

MAX_SHARED: Final = 300  # objects listed at most
MAX_READS: Final = 4  # search pages read per kind (100 objects each)
UNTITLED: Final = "Untitled"
_ROW_PARENTS: Final = frozenset({"database", "data_source"})


@dataclass(frozen=True)
class SharedObject:
    kind: Literal["page", "database"]
    id: str
    title: str
    link: str
    parent_title: str | None = None


@dataclass(frozen=True)
class SharedObjects:
    items: tuple[SharedObject, ...]
    capped: bool


def link_of(object_id: str) -> str:
    return f"https://www.notion.so/{object_id.replace('-', '').lower()}"


async def _read(gateway: NotionGateway, kind: SearchKind) -> tuple[list[NotionPage], list[NotionDataSource], bool]:
    pages: list[NotionPage] = []
    sources: list[NotionDataSource] = []
    cursor: str | None = None
    for _ in range(MAX_READS):
        found = await gateway.search(kind, start_cursor=cursor)
        pages.extend(found.pages)
        sources.extend(found.data_sources)
        cursor = found.next_cursor
        if cursor is None:
            return pages, sources, False
    return pages, sources, True


async def shared_objects(gateway: NotionGateway) -> SharedObjects:
    """The pages (not rows) and databases ``gateway``'s connection can read. ``NotionError`` when Notion refuses."""

    pages, _, pages_cut = await _read(gateway, "page")
    _, sources, sources_cut = await _read(gateway, "data_source")
    titles = {page.id: scrub_text(page.title).strip() or UNTITLED for page in pages}
    items: list[SharedObject] = [
        SharedObject(
            "page",
            page.id,
            titles[page.id],
            link_of(page.id),
            titles.get(page.parent.id) if page.parent.kind == "page" and page.parent.id else None,
        )
        for page in pages
        if not page.in_trash and page.parent.kind not in _ROW_PARENTS
    ]
    databases: dict[str, SharedObject] = {}
    for source in sources:
        if source.in_trash or source.database_id is None or source.database_id in databases:
            continue
        title = scrub_text(source.title).strip() or UNTITLED
        databases[source.database_id] = SharedObject("database", source.database_id, title, link_of(source.database_id))
    items.extend(databases.values())
    items.sort(key=lambda item: (item.title.casefold(), (item.parent_title or "").casefold()))
    capped = pages_cut or sources_cut or len(items) > MAX_SHARED
    return SharedObjects(tuple(items[:MAX_SHARED]), capped)
