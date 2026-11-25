"""The real ``NotionGateway`` over Notion's REST API (docs/notion/oauth.md). Stream A fills this file.

Rules to implement: ``Notion-Version: 2026-03-11`` and the member's bearer token on every request (``token`` is called
per request, so a refresh is seen); every request takes a place from the bot's ``RateLimiter`` first; ``429`` and
``529`` are retried always (``Retry-After`` through ``RateLimiter.hold``), ``5xx`` for ``GET`` only, five attempts
with exponential backoff and jitter; ``401`` is ``notion_auth_failed`` (the caller refreshes once); a ``404`` is
``notion_not_shared``; any other refusal is one ``NotionCode`` from ``notion.reasons.code_of_status`` and never
carries Notion's text. ``page_markdown`` follows ``unknown_block_ids`` (see ``PageMarkdown``). Pagination is the
caller's: ``query_data_source`` answers one page. The tests are ``test_notion_http.py`` (respx),
``test_notion_fake_parity.py`` and the live ``test_notion_http_live.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from notion.gateway import API_BASE, NOTION_VERSION, PAGE_SIZE
from notion.limiter import RateLimiter, WaitBudget
from notion.model import NotionDatabase, NotionDataSource, NotionPage, PageMarkdown, RowsPage

TokenSource = Callable[[], Awaitable[str]]


class HttpNotionGateway:
    """``NotionGateway`` over ``httpx``; one per pass or import (it holds the pass's ``WaitBudget``)."""

    def __init__(
        self,
        token: TokenSource,
        limiter: RateLimiter,
        *,
        budget: WaitBudget | None = None,
        base_url: str = API_BASE,
        version: str = NOTION_VERSION,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._limiter = limiter
        self._budget = budget or WaitBudget()
        self._base_url = base_url
        self._version = version
        self._client = client
        self._sleep = sleep

    async def aclose(self) -> None:
        raise NotImplementedError("stream A fills HttpNotionGateway.aclose")

    async def page(self, page_id: str) -> NotionPage:
        raise NotImplementedError("stream A fills HttpNotionGateway.page")

    async def database(self, database_id: str) -> NotionDatabase:
        raise NotImplementedError("stream A fills HttpNotionGateway.database")

    async def data_source(self, data_source_id: str) -> NotionDataSource:
        raise NotImplementedError("stream A fills HttpNotionGateway.data_source")

    async def page_markdown(self, page_id: str) -> PageMarkdown:
        raise NotImplementedError("stream A fills HttpNotionGateway.page_markdown")

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        start_cursor: str | None = None,
        page_size: int = PAGE_SIZE,
        where: Mapping[str, Any] | None = None,
    ) -> RowsPage:
        raise NotImplementedError("stream A fills HttpNotionGateway.query_data_source")

    async def create_database(self, parent_page_id: str, title: str, properties: Mapping[str, Any]) -> NotionDatabase:
        raise NotImplementedError("stream A fills HttpNotionGateway.create_database")

    async def update_data_source(self, data_source_id: str, properties: Mapping[str, Any]) -> NotionDataSource:
        raise NotImplementedError("stream A fills HttpNotionGateway.update_data_source")

    async def create_page(
        self, data_source_id: str, *, properties: Mapping[str, Any], markdown: str = ""
    ) -> NotionPage:
        raise NotImplementedError("stream A fills HttpNotionGateway.create_page")

    async def update_page(self, page_id: str, *, properties: Mapping[str, Any]) -> NotionPage:
        raise NotImplementedError("stream A fills HttpNotionGateway.update_page")

    async def replace_markdown(self, page_id: str, markdown: str) -> None:
        raise NotImplementedError("stream A fills HttpNotionGateway.replace_markdown")

    async def create_comment(self, page_id: str, markdown: str) -> None:
        raise NotImplementedError("stream A fills HttpNotionGateway.create_comment")
