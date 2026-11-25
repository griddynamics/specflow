"""``FakeNotionGateway``: the in-memory ``NotionGateway`` of the tests and the offline tiers.

It starts from the fixtures of ``notion/fixtures/`` recorded from the "Specflow Notion Lab" sandbox with
``Notion-Version: 2026-03-11`` (the PRD, the ADR, the wiki tree, the Requirements database, the plan trial database;
every e-mail is ``person@example.com``, every person ``Sample Author``, every user id one fixed fake uuid) and keeps
what the tests write: created databases, pages and comments are readable at once, and every call is recorded in
``calls``. ``unshare`` makes an id answer ``notion_not_shared`` as a page the member did not tick in the picker does;
``fail`` makes the next call of a method raise a given ``NotionError``. Stream A's parity test runs the same scenario
against this fake and the HTTP client.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from notion.gateway import PAGE_SIZE
from notion.model import (
    NotionDatabase,
    NotionDataSource,
    NotionPage,
    PageMarkdown,
    RowsPage,
    data_source_from_json,
    database_from_json,
    page_from_json,
)
from notion.reasons import NotionCode, NotionError

FIXTURES: Final = Path(__file__).parent / "fixtures"
ROOT_PAGE_ID: Final = "423ed336-0645-81f3-b5ea-ff40471c93e5"  # "Specflow Notion Lab"
PRD_PAGE_ID: Final = "423ed336-0645-8163-a8dd-e12275fc660d"
ADR_PAGE_ID: Final = "423ed336-0645-816a-aff4-ffe7862445e1"
WIKI_PAGE_ID: Final = "423ed336-0645-8137-b924-dd97aa16e0a9"
REQUIREMENTS_DATABASE_ID: Final = "423ed336-0645-819b-8a30-d73a847eb5e6"
REQUIREMENTS_DATA_SOURCE_ID: Final = "423ed336-0645-819d-aec9-000b2cecb51b"
PLAN_TRIAL_DATABASE_ID: Final = "423ed336-0645-8136-a09f-e7d3e03939ab"
PLAN_TRIAL_DATA_SOURCE_ID: Final = "423ed336-0645-8162-9b9a-000bbce2d0e4"
EMPTY_BODY_ROW_ID: Final = "423ed336-0645-81ec-b3ae-e35ee7c4de54"  # a Requirements row with an empty body
FAKE_USER_ID: Final = "00000000-0000-4000-8000-0000000000a1"
FAKE_WORKSPACE_ID: Final = "00000000-0000-4000-8000-0000000000b1"
FAKE_BOT_ID: Final = FAKE_USER_ID
ID_DIGITS: Final = 32  # hex digits of a Notion id


def normal(object_id: str) -> str:
    """A Notion id with dashes (``8-4-4-4-12``), lower case; anything else comes back unchanged."""

    plain = object_id.replace("-", "").lower()
    if len(plain) != ID_DIGITS:
        return object_id
    return f"{plain[:8]}-{plain[8:12]}-{plain[12:16]}-{plain[16:20]}-{plain[20:]}"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Call:
    """One call of the fake: the method and its arguments (ids and Notion JSON, as passed)."""

    method: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


class FakeNotionGateway:
    """The fixtures in memory; fill ``calls`` and honour ``unshare`` and ``fail``."""

    def __init__(self) -> None:
        self.pages: dict[str, NotionPage] = {k: page_from_json(v) for k, v in _load("pages").items()}
        self.databases: dict[str, NotionDatabase] = {k: database_from_json(v) for k, v in _load("databases").items()}
        self.data_sources: dict[str, NotionDataSource] = {
            k: data_source_from_json(v) for k, v in _load("data_sources").items()
        }
        self.markdown: dict[str, PageMarkdown] = {
            k: PageMarkdown(v["markdown"], bool(v.get("truncated")), tuple(v.get("unknown_block_ids") or ()))
            for k, v in _load("markdown").items()
        }
        self.rows: dict[str, list[str]] = {k: [row["id"] for row in v] for k, v in _load("queries").items()}
        self.comments: list[tuple[str, str]] = []
        self.calls: list[Call] = []
        self._unshared: set[str] = set()
        self._failures: dict[str, list[NotionError]] = {}
        self._created = 0

    # Test controls -------------------------------------------------------------------------------------------------

    def unshare(self, *object_ids: str) -> None:
        """These ids answer ``notion_not_shared`` from now on."""

        self._unshared.update(normal(i) for i in object_ids)

    def fail(self, method: str, code: NotionCode, *, times: int = 1) -> None:
        """The next ``times`` calls of ``method`` raise ``NotionError(code)``."""

        self._failures.setdefault(method, []).extend(NotionError(code) for _ in range(times))

    def called(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]

    # Inside --------------------------------------------------------------------------------------------------------

    def _enter(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append(Call(method, args, kwargs))
        queue = self._failures.get(method)
        if queue:
            raise queue.pop(0)

    def _visible(self, object_id: str) -> str:
        found = normal(object_id)
        if found in self._unshared:
            raise NotionError(NotionCode.NOT_SHARED)
        return found

    def _new_id(self) -> str:
        self._created += 1
        return normal(f"fa4e0000{self._created:024x}")

    # Read ----------------------------------------------------------------------------------------------------------

    async def page(self, page_id: str) -> NotionPage:
        self._enter("page", page_id)
        found = self._visible(page_id)
        if found not in self.pages:
            raise NotionError(NotionCode.NOT_SHARED)
        return self.pages[found]

    async def database(self, database_id: str) -> NotionDatabase:
        self._enter("database", database_id)
        found = self._visible(database_id)
        if found not in self.databases:
            raise NotionError(NotionCode.NOT_SHARED)
        return self.databases[found]

    async def data_source(self, data_source_id: str) -> NotionDataSource:
        self._enter("data_source", data_source_id)
        found = self._visible(data_source_id)
        if found not in self.data_sources:
            raise NotionError(NotionCode.NOT_SHARED)
        return self.data_sources[found]

    async def page_markdown(self, page_id: str) -> PageMarkdown:
        self._enter("page_markdown", page_id)
        found = self._visible(page_id)
        if found not in self.markdown:
            raise NotionError(NotionCode.NOT_SHARED)
        return self.markdown[found]

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = PAGE_SIZE,
        where: Mapping[str, Any] | None = None,
    ) -> RowsPage:
        self._enter("query_data_source", data_source_id, start_cursor=start_cursor, page_size=page_size, where=where)
        found = self._visible(data_source_id)
        if found not in self.rows:
            raise NotionError(NotionCode.NOT_SHARED)
        ids = [i for i in self.rows[found] if normal(i) not in self._unshared]
        if where is not None:  # the one filter the export uses: a rich-text property equals a value
            ids = [i for i in ids if self._matches(self.pages[normal(i)], where)]
        start = int(start_cursor or 0)
        chunk = ids[start : start + page_size]
        more = start + page_size < len(ids)
        return RowsPage(tuple(self.pages[normal(i)] for i in chunk), str(start + page_size) if more else None)

    @staticmethod
    def _matches(page: NotionPage, where: Mapping[str, Any]) -> bool:
        name = where.get("property")
        wanted = (where.get("rich_text") or {}).get("equals")
        value = page.properties.get(str(name)) or {}
        text = "".join(part.get("plain_text", "") for part in value.get("rich_text") or [])
        return wanted is None or text == wanted

    # Write ---------------------------------------------------------------------------------------------------------

    async def create_database(self, parent_page_id: str, title: str, properties: Mapping[str, Any]) -> NotionDatabase:
        self._enter("create_database", parent_page_id, title, properties)
        parent = self._visible(parent_page_id)
        if parent not in self.pages:
            raise NotionError(NotionCode.NOT_SHARED)
        database_id, source_id = self._new_id(), self._new_id()
        raw_properties = {name: {"id": name, **dict(value)} for name, value in properties.items()}
        database = database_from_json(
            {
                "id": database_id,
                "url": f"https://www.notion.so/{database_id.replace('-', '')}",
                "title": [{"plain_text": title}],
                "parent": {"type": "page_id", "page_id": parent},
                "data_sources": [{"id": source_id, "name": title}],
            }
        )
        self.databases[database_id] = database
        self.data_sources[source_id] = data_source_from_json(
            {
                "id": source_id,
                "parent": {"type": "database_id", "database_id": database_id},
                "title": [{"plain_text": title}],
                "properties": raw_properties,
            }
        )
        self.rows[source_id] = []
        return database

    async def update_data_source(self, data_source_id: str, properties: Mapping[str, Any]) -> NotionDataSource:
        self._enter("update_data_source", data_source_id, properties)
        found = self._visible(data_source_id)
        if found not in self.data_sources:
            raise NotionError(NotionCode.NOT_SHARED)
        before = self.data_sources[found]
        merged = {**before.properties, **{name: {"id": name, **dict(value)} for name, value in properties.items()}}
        updated = NotionDataSource(before.id, before.database_id, before.title, merged, before.in_trash)
        self.data_sources[found] = updated
        return updated

    async def create_page(
        self, data_source_id: str, *, properties: Mapping[str, Any], markdown: str = ""
    ) -> NotionPage:
        self._enter("create_page", data_source_id, properties=properties, markdown=markdown)
        source = self._visible(data_source_id)
        if source not in self.rows:
            raise NotionError(NotionCode.NOT_SHARED)
        page_id = self._new_id()
        page = NotionPage(
            id=page_id,
            url=f"https://www.notion.so/{page_id.replace('-', '')}",
            title=self._title(properties),
            parent=page_from_json(
                {"id": page_id, "parent": {"type": "data_source_id", "data_source_id": source}}
            ).parent,
            last_edited_time=None,
            properties={name: {"id": name, **dict(value)} for name, value in properties.items()},
        )
        self.pages[page_id] = page
        self.markdown[page_id] = PageMarkdown(markdown)
        self.rows[source].append(page_id)
        return page

    async def update_page(self, page_id: str, *, properties: Mapping[str, Any]) -> NotionPage:
        self._enter("update_page", page_id, properties=properties)
        found = self._visible(page_id)
        if found not in self.pages:
            raise NotionError(NotionCode.NOT_SHARED)
        before = self.pages[found]
        merged = {**before.properties, **{name: {"id": name, **dict(value)} for name, value in properties.items()}}
        updated = NotionPage(
            before.id, before.url, self._title(merged) or before.title, before.parent, before.last_edited_time,
            before.in_trash, merged,
        )  # fmt: skip
        self.pages[found] = updated
        return updated

    async def replace_markdown(self, page_id: str, markdown: str) -> None:
        self._enter("replace_markdown", page_id, markdown)
        found = self._visible(page_id)
        if found not in self.pages:
            raise NotionError(NotionCode.NOT_SHARED)
        self.markdown[found] = PageMarkdown(markdown)

    async def create_comment(self, page_id: str, markdown: str) -> None:
        self._enter("create_comment", page_id, markdown)
        found = self._visible(page_id)
        if found not in self.pages:
            raise NotionError(NotionCode.NOT_SHARED)
        self.comments.append((found, markdown))

    @staticmethod
    def _title(properties: Mapping[str, Any]) -> str:
        for value in properties.values():
            parts = value.get("title") if isinstance(value, dict) else None
            if parts:
                return "".join(
                    str((part.get("text") or {}).get("content", part.get("plain_text", ""))) for part in parts
                )
        return ""
