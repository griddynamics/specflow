"""What the Notion gateway hands over: frozen dataclasses of the few fields the integration reads, and the parsers of
Notion's JSON objects (shared by the HTTP client and the fake, so both build the same values). No field is a token."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ParentKind = Literal["workspace", "page", "database", "data_source", "block", "other"]


@dataclass(frozen=True)
class NotionParent:
    kind: ParentKind
    id: str | None = None


@dataclass(frozen=True)
class NotionPage:
    """A page or a database row. ``properties`` is Notion's property map as JSON (a row's values); ``title`` the
    plain text of the title property. ``in_trash`` is true for a deleted or archived page."""

    id: str
    url: str
    title: str
    parent: NotionParent
    last_edited_time: datetime | None
    in_trash: bool = False
    properties: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataSourceRef:
    id: str
    name: str


@dataclass(frozen=True)
class NotionDatabase:
    """A database and its data sources (a database holds one or more since API 2025-09-03)."""

    id: str
    url: str
    title: str
    parent: NotionParent
    data_sources: tuple[DataSourceRef, ...]
    in_trash: bool = False


@dataclass(frozen=True)
class NotionDataSource:
    """A data source and its schema: ``properties`` is Notion's schema map (a ``status`` property lists its options
    and groups there)."""

    id: str
    database_id: str | None
    title: str
    properties: Mapping[str, Any]
    in_trash: bool = False


@dataclass(frozen=True)
class RowsPage:
    """One page of ``query_data_source``: the rows and the cursor of the next page (``None`` at the end)."""

    rows: tuple[NotionPage, ...]
    next_cursor: str | None


SearchKind = Literal["page", "data_source"]


@dataclass(frozen=True)
class SearchPage:
    """One page of ``search``: the pages or the data sources the connection can read (one of the two, by the kind
    asked for) and the cursor of the next page (``None`` at the end)."""

    pages: tuple[NotionPage, ...] = ()
    data_sources: tuple[NotionDataSource, ...] = ()
    next_cursor: str | None = None


@dataclass(frozen=True)
class PageMarkdown:
    """A page as Notion-flavored Markdown (v2). The gateway has already read every ``unknown_block_ids`` subtree and
    appended its Markdown after a blank line, up to its bound; ``truncated`` stays true and ``unknown_block_ids`` lists
    what is still unread when that bound was hit."""

    markdown: str
    truncated: bool = False
    unknown_block_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageRef:
    """A sub-page the Markdown names (a ``<page>`` line): the import follows it."""

    id: str
    title: str
    url: str


@dataclass(frozen=True)
class DatabaseRef:
    """A child database the Markdown names (a ``<database>`` line): ``data_source_id`` comes from its
    ``data-source-url`` (``None`` when the line has none)."""

    title: str
    url: str
    data_source_id: str | None = None


# Parsers ---------------------------------------------------------------------------------------------------------


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def plain_text(rich: Any) -> str:
    """The plain text of a Notion rich-text array."""

    if not isinstance(rich, list):
        return ""
    return "".join(str(part.get("plain_text", "")) for part in rich if isinstance(part, dict))


def parent_of(raw: Any) -> NotionParent:
    if not isinstance(raw, dict):
        return NotionParent("other")
    kind = str(raw.get("type", ""))
    mapping: dict[str, ParentKind] = {
        "workspace": "workspace",
        "page_id": "page",
        "database_id": "database",
        "data_source_id": "data_source",
        "block_id": "block",
    }
    if kind not in mapping:
        return NotionParent("other")
    return NotionParent(mapping[kind], None if kind == "workspace" else str(raw.get(kind)))


def page_from_json(raw: Mapping[str, Any]) -> NotionPage:
    properties = raw.get("properties") or {}
    title = ""
    for value in properties.values():
        if isinstance(value, dict) and value.get("type") == "title":
            title = plain_text(value.get("title"))
            break
    return NotionPage(
        id=str(raw["id"]),
        url=str(raw.get("url", "")),
        title=title,
        parent=parent_of(raw.get("parent")),
        last_edited_time=parse_time(raw.get("last_edited_time")),
        in_trash=bool(raw.get("in_trash") or raw.get("archived")),
        properties=properties,
    )


def database_from_json(raw: Mapping[str, Any]) -> NotionDatabase:
    sources = tuple(
        DataSourceRef(str(source["id"]), str(source.get("name", ""))) for source in raw.get("data_sources") or ()
    )
    return NotionDatabase(
        id=str(raw["id"]),
        url=str(raw.get("url", "")),
        title=plain_text(raw.get("title")),
        parent=parent_of(raw.get("parent")),
        data_sources=sources,
        in_trash=bool(raw.get("in_trash") or raw.get("archived")),
    )


def data_source_from_json(raw: Mapping[str, Any]) -> NotionDataSource:
    parent = parent_of(raw.get("parent"))
    return NotionDataSource(
        id=str(raw["id"]),
        database_id=parent.id if parent.kind == "database" else None,
        title=plain_text(raw.get("title")),
        properties=raw.get("properties") or {},
        in_trash=bool(raw.get("in_trash") or raw.get("archived")),
    )
