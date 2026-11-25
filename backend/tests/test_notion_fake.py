"""The Notion contract files that work in W0: the link parser, the model parsers, the fixture-backed fake and the
fixtures' PII rule. The streams add their own tests beside this one."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from db.models import NOTION_RUN_STATUSES
from notion import fake as fake_module
from notion.fake import (
    ADR_PAGE_ID,
    EMPTY_BODY_ROW_ID,
    PLAN_TRIAL_DATA_SOURCE_ID,
    PRD_PAGE_ID,
    REQUIREMENTS_DATA_SOURCE_ID,
    REQUIREMENTS_DATABASE_ID,
    ROOT_PAGE_ID,
    FakeNotionGateway,
    normal,
)
from notion.links import parse_link
from notion.reasons import NotionCode, NotionError, code_of_status

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@pytest.mark.parametrize(
    ("text", "expected", "view"),
    [
        ("https://www.notion.so/PRD-423ed33606458163a8dde12275fc660d", PRD_PAGE_ID, None),
        ("https://app.notion.com/p/Lab-423ed336064581f3b5eaff40471c93e5?pvs=4", ROOT_PAGE_ID, None),
        ("https://acme.notion.site/423ed336-0645-81f3-b5ea-ff40471c93e5", ROOT_PAGE_ID, None),
        (
            "https://www.notion.so/Req-423ed3360645819b8a30d73a847eb5e6?v=423ed3360645819b8a30d73a847eb5e7",
            REQUIREMENTS_DATABASE_ID,
            "423ed336-0645-819b-8a30-d73a847eb5e7",
        ),
    ],
)
def test_a_notion_link_names_one_object_id(text: str, expected: str, view: str | None) -> None:
    link = parse_link(text)

    assert (link.id, link.view, link.probably_database) == (expected, view, view is not None)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a link",
        "http://www.notion.so/x-423ed33606458163a8dde12275fc660d",
        "https://evil.example/x-423ed33606458163a8dde12275fc660d",
        "https://www.notion.so/Title",
        "https://www.notion.so/" + "a" * 3000,
    ],
)
def test_anything_else_is_not_a_notion_link(text: str) -> None:
    with pytest.raises(NotionError) as raised:
        parse_link(text)

    assert raised.value.code is NotionCode.LINK_INVALID and str(raised.value) == "notion_link_invalid"


def test_the_status_of_a_refusal_maps_to_one_closed_code() -> None:
    assert code_of_status(401) is NotionCode.AUTH_FAILED
    assert code_of_status(404, "object_not_found") is NotionCode.NOT_SHARED
    assert code_of_status(400, "validation_error") is NotionCode.ITEM_INVALID
    assert code_of_status(429) is NotionCode.RATE_LIMITED
    assert code_of_status(502) is NotionCode.UNREACHABLE


async def test_the_fake_reads_the_recorded_sandbox() -> None:
    fake = FakeNotionGateway()

    page = await fake.page(PRD_PAGE_ID.replace("-", ""))
    markdown = await fake.page_markdown(PRD_PAGE_ID)
    rows = await fake.query_data_source(REQUIREMENTS_DATA_SOURCE_ID)
    database = await fake.database(REQUIREMENTS_DATABASE_ID)
    source = await fake.data_source(PLAN_TRIAL_DATA_SOURCE_ID)

    assert page.title == "PRD - Checkout Loyalty Points" and page.parent.id == ROOT_PAGE_ID
    assert "<callout" in markdown.markdown and not markdown.truncated and markdown.unknown_block_ids == ()
    assert rows.rows[0].title == "Show earned points on checkout summary" and rows.next_cursor is None
    assert database.data_sources[0].id == REQUIREMENTS_DATA_SOURCE_ID
    assert {"Parent", "Children", "Blocked by", "Blocks", "Status"} <= set(source.properties)
    assert (await fake.page_markdown(EMPTY_BODY_ROW_ID)).markdown == ""


async def test_the_fake_pages_rows_by_cursor() -> None:
    fake = FakeNotionGateway()

    first = await fake.query_data_source(REQUIREMENTS_DATA_SOURCE_ID, page_size=2)
    second = await fake.query_data_source(REQUIREMENTS_DATA_SOURCE_ID, start_cursor=first.next_cursor, page_size=2)

    assert (len(first.rows), first.next_cursor, len(second.rows), second.next_cursor) == (2, "2", 1, None)


async def test_the_fake_writes_a_database_a_row_a_body_and_a_comment_and_reads_them_back() -> None:
    fake = FakeNotionGateway()

    database = await fake.create_database(
        ROOT_PAGE_ID, "Graphico plan", {"Name": {"title": {}}, "Graphico ID": {"rich_text": {}}}
    )
    source_id = database.data_sources[0].id
    await fake.update_data_source(source_id, {"Parent": {"relation": {"data_source_id": source_id}}})
    row = await fake.create_page(
        source_id,
        properties={
            "Name": {"title": [{"text": {"content": "Story one"}}]},
            "Graphico ID": {"rich_text": [{"plain_text": "S-1", "text": {"content": "S-1"}}]},
        },
        markdown="Brief",
    )
    await fake.update_page(row.id, properties={"Name": {"title": [{"text": {"content": "Story 1"}}]}})
    await fake.replace_markdown(row.id, "New brief")
    await fake.create_comment(row.id, "changed")
    found = await fake.query_data_source(source_id, where={"property": "Graphico ID", "rich_text": {"equals": "S-1"}})

    assert (await fake.page(row.id)).title == "Story 1"
    assert (await fake.page_markdown(row.id)).markdown == "New brief"
    assert fake.comments == [(row.id, "changed")] and [r.id for r in found.rows] == [row.id]
    assert {"Parent", "Name"} <= set((await fake.data_source(source_id)).properties)
    assert [call.method for call in fake.calls][:2] == ["create_database", "update_data_source"]


async def test_a_database_id_on_a_page_route_is_not_a_page_and_a_page_id_on_the_database_route_is_not_a_link() -> None:
    fake = FakeNotionGateway()

    with pytest.raises(NotionError) as page:
        await fake.page(REQUIREMENTS_DATABASE_ID)
    with pytest.raises(NotionError) as markdown:
        await fake.page_markdown(REQUIREMENTS_DATABASE_ID)
    with pytest.raises(NotionError) as database:
        await fake.database(PRD_PAGE_ID)

    assert page.value.code is NotionCode.NOT_A_PAGE and markdown.value.code is NotionCode.NOT_A_PAGE
    assert database.value.code is NotionCode.LINK_INVALID


def test_the_run_statuses_of_the_model_are_the_migrations_and_the_hosts_include_notion_com() -> None:
    text = (Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0011_notion.py").read_text("utf-8")
    declared = re.search(r"RUN_STATUSES = \(([^)]*)\)", text)

    assert declared is not None
    assert tuple(re.findall(r'"(\w+)"', declared.group(1))) == NOTION_RUN_STATUSES
    assert parse_link("https://www.notion.com/Page-423ed33606458163a8dde12275fc660d").id == PRD_PAGE_ID
    assert parse_link("https://notion.com/423ed33606458163a8dde12275fc660d").id == PRD_PAGE_ID


async def test_an_unshared_page_and_an_injected_failure_raise_closed_codes() -> None:
    fake = FakeNotionGateway()
    fake.unshare(ADR_PAGE_ID.replace("-", "").upper())
    fake.fail("page_markdown", NotionCode.UNREACHABLE)

    with pytest.raises(NotionError) as unshared:
        await fake.page(ADR_PAGE_ID)
    with pytest.raises(NotionError) as failed:
        await fake.page_markdown(PRD_PAGE_ID)
    again = await fake.page_markdown(PRD_PAGE_ID)

    assert unshared.value.code is NotionCode.NOT_SHARED and failed.value.code is NotionCode.UNREACHABLE
    assert again.markdown and normal("A" * 32).count("-") == 4


def test_the_fixtures_hold_no_personal_data() -> None:
    texts = [path.read_text(encoding="utf-8") for path in sorted(fake_module.FIXTURES.glob("*.json"))]
    text = "\n".join(texts)

    assert set(EMAIL.findall(text)) <= {"person@example.com"}
    assert "googleusercontent" not in text and "Bearer" not in text and "secret_" not in text
    assert set(re.findall(r"user://([0-9a-f-]+)/", text)) <= {fake_module.FAKE_USER_ID}
    assert all(json.loads(path.read_text(encoding="utf-8")) for path in fake_module.FIXTURES.glob("*.json"))
    assert Path(fake_module.FIXTURES / "bot.json").exists()
