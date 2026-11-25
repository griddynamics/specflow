"""The port of the Notion REST API (docs/notion/INDEX.md): ``NotionGateway`` is what the importer and the plan export
call, ``notion.http.HttpNotionGateway`` is the real client and ``notion.fake.FakeNotionGateway`` the test double that
tests and the offline tiers use. Every method acts as one member's connection (the gateway holds the token source), has
the rate limit and the retries of ``docs/notion/oauth.md`` applied, and raises ``notion.reasons.NotionError`` with one
closed code and nothing else: a page that does not exist or is not shared is ``notion_not_shared``.

Ids are Notion ids with or without dashes; the gateway answers them with dashes. Properties and schemas are Notion's
own JSON (API version 2026-03-11).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Protocol

from notion.model import NotionDatabase, NotionDataSource, NotionPage, PageMarkdown, RowsPage

NOTION_VERSION: Final = "2026-03-11"
API_BASE: Final = "https://api.notion.com"
PAGE_SIZE: Final = 100  # rows per ``query_data_source`` page (Notion's maximum)


class NotionGateway(Protocol):
    # Read ------------------------------------------------------------------------------------------------------

    async def page(self, page_id: str) -> NotionPage:
        """One page or database row (``GET /v1/pages/{id}``)."""

    async def database(self, database_id: str) -> NotionDatabase:
        """One database with its data sources (``GET /v1/databases/{id}``)."""

    async def data_source(self, data_source_id: str) -> NotionDataSource:
        """One data source with its schema (``GET /v1/data_sources/{id}``)."""

    async def page_markdown(self, page_id: str) -> PageMarkdown:
        """The page as Markdown v2 (``GET /v1/pages/{id}/markdown?markdown_version=v2``), with the ``truncated`` and
        ``unknown_block_ids`` follow-up already done (see ``PageMarkdown``)."""

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = PAGE_SIZE,
        where: Mapping[str, Any] | None = None,
    ) -> RowsPage:
        """One page of rows (``POST /v1/data_sources/{id}/query``); the caller loops on ``next_cursor``. ``where`` is
        Notion's ``filter`` object."""

    # Write -----------------------------------------------------------------------------------------------------

    async def create_database(self, parent_page_id: str, title: str, properties: Mapping[str, Any]) -> NotionDatabase:
        """A database under a page with one data source of ``properties`` (``POST /v1/databases``)."""

    async def update_data_source(self, data_source_id: str, properties: Mapping[str, Any]) -> NotionDataSource:
        """Add or change schema properties, for example the self-relations that need the data source's own id
        (``PATCH /v1/data_sources/{id}``)."""

    async def create_page(
        self, data_source_id: str, *, properties: Mapping[str, Any], markdown: str = ""
    ) -> NotionPage:
        """A row of a data source with its body as Markdown (``POST /v1/pages``)."""

    async def update_page(self, page_id: str, *, properties: Mapping[str, Any]) -> NotionPage:
        """Change the properties of a row (``PATCH /v1/pages/{id}``)."""

    async def replace_markdown(self, page_id: str, markdown: str) -> None:
        """Replace the body of a page (``PATCH /v1/pages/{id}/markdown``)."""

    async def create_comment(self, page_id: str, markdown: str) -> None:
        """A comment on a page (``POST /v1/comments``)."""
