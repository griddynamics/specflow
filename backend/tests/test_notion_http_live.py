"""``HttpNotionGateway`` against the real Notion API with the internal connection of the "Specflow Notion Lab" sandbox
(``NOTION_API_TOKEN``, skipped without it). Read-only: nothing is created, changed or commented. Run it with
``uv run --env-file ../.env.local pytest tests/test_notion_http_live.py``."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from notion.fake import (
    PRD_PAGE_ID,
    REQUIREMENTS_DATA_SOURCE_ID,
    REQUIREMENTS_DATABASE_ID,
    ROOT_PAGE_ID,
    FakeNotionGateway,
)
from notion.http import HttpNotionGateway
from notion.limiter import RateLimiter
from notion.reasons import NotionCode, NotionError

pytestmark = pytest.mark.skipif(not os.environ.get("NOTION_API_TOKEN"), reason="set NOTION_API_TOKEN to run")

UNKNOWN_ID = "00000000-0000-4000-8000-0000000000ff"


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[HttpNotionGateway]:
    async def token() -> str:
        return os.environ["NOTION_API_TOKEN"]

    live = HttpNotionGateway(token, RateLimiter(150))
    yield live
    await live.aclose()


async def test_the_sandbox_reads_like_the_recorded_fixtures(gateway: HttpNotionGateway) -> None:
    fake = FakeNotionGateway()

    page = await gateway.page(PRD_PAGE_ID)
    database = await gateway.database(REQUIREMENTS_DATABASE_ID)
    source = await gateway.data_source(REQUIREMENTS_DATA_SOURCE_ID)
    markdown = await gateway.page_markdown(PRD_PAGE_ID)
    rows = await gateway.query_data_source(REQUIREMENTS_DATA_SOURCE_ID)

    recorded = await fake.page(PRD_PAGE_ID)
    assert (page.id, page.title, page.parent) == (recorded.id, recorded.title, recorded.parent)
    assert (database.id, database.title) == (REQUIREMENTS_DATABASE_ID, "Requirements DB")
    assert [s.id for s in database.data_sources] == [REQUIREMENTS_DATA_SOURCE_ID]
    assert source.database_id == REQUIREMENTS_DATABASE_ID and source.properties
    assert markdown.markdown.strip() and not markdown.truncated
    assert len(rows.rows) == len((await fake.query_data_source(REQUIREMENTS_DATA_SOURCE_ID)).rows)
    assert (await gateway.page_markdown(ROOT_PAGE_ID)).markdown.strip()


async def test_wrong_kind_and_unknown_ids_are_closed_codes(gateway: HttpNotionGateway) -> None:
    with pytest.raises(NotionError) as database_as_page:
        await gateway.page(REQUIREMENTS_DATABASE_ID)
    with pytest.raises(NotionError) as database_as_markdown:
        await gateway.page_markdown(REQUIREMENTS_DATABASE_ID)
    with pytest.raises(NotionError) as page_as_database:
        await gateway.database(PRD_PAGE_ID)
    with pytest.raises(NotionError) as unknown:
        await gateway.page(UNKNOWN_ID)

    assert database_as_page.value.code is NotionCode.NOT_A_PAGE
    assert database_as_markdown.value.code is NotionCode.NOT_A_PAGE
    assert page_as_database.value.code is NotionCode.LINK_INVALID
    assert unknown.value.code is NotionCode.NOT_SHARED


async def test_a_bad_token_is_auth_failed_and_never_retried() -> None:
    async def token() -> str:
        return "secret_not-a-real-token"

    gateway = HttpNotionGateway(token, RateLimiter(150))
    try:
        with pytest.raises(NotionError) as refused:
            await gateway.page(PRD_PAGE_ID)
    finally:
        await gateway.aclose()

    assert refused.value.code is NotionCode.AUTH_FAILED
