"""The Notion importer (``api.imports.notion``) over the fixture-backed ``FakeNotionGateway`` and a writer that records
what it stores: a page tree, a database's rows, a page that holds a database, the cap, cycles, the pages a member
cannot see, a page too deep to convert, the failure reasons of the source, the slim original and a second run after a
restart. The converter of stream B is injected (``FakeConverter``): the real one is tested in ``tests/notion/convert``.

Sequence of the tree scenario::

    importer -> gateway of the member the project is bound to -> page(root) -> page_markdown(root)
    importer -> <page> lines: declare, then page_markdown of each (their own <page> lines are followed too)
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from api.imports import ImportFailedError
from api.imports import notion as notion_module
from api.imports.model import SourceGoneError
from api.imports.notion import Converter, NotionImport, NotionImporter, inspect
from notion.convert import ConvertedPage, MarkupTooDeepError
from notion.fake import (
    PRD_PAGE_ID,
    REQUIREMENTS_DATA_SOURCE_ID,
    REQUIREMENTS_DATABASE_ID,
    ROOT_PAGE_ID,
    WIKI_PAGE_ID,
    FakeNotionGateway,
    normal,
)
from notion.links import NotionLink, dashed
from notion.model import DatabaseRef, NotionPage, PageMarkdown, PageRef
from notion.reasons import NotionCode
from notion_support import ACTING, FakeNotionConnections, RecordingWriter, as_writer, notion_job

PAGE_TAG = re.compile(r'<page url="([^"]+)">\s*(.*?)\s*</page>', re.S)
DATABASE_TAG = re.compile(
    r'<database url="([^"]+)" data-source-url="collection://([^"]*)">\s*(.*?)\s*</database>', re.S
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
TOO_DEEP = "<<too-deep>>"
CANARY = "body-canary-6e41"
ONBOARDING, RUNBOOKS, RUNBOOK = (
    "423ed336-0645-8143-b422-d74e5eb9926c",
    "423ed336-0645-8159-9753-d8419c8ae458",
    "423ed336-0645-81b9-83b2-f33eaa8f36e5",
)


def id_of(url: str) -> str:
    return dashed(re.search(r"([0-9a-fA-F]{32})$", url.split("?")[0].rstrip("/"))[1])  # type: ignore[index]


def to_markdown(nfm: str) -> ConvertedPage:
    """A stand-in of ``notion.convert.to_markdown``: ``<page>`` and ``<database>`` lines become references."""

    if TOO_DEEP in nfm:
        raise MarkupTooDeepError
    pages = tuple(PageRef(id_of(url), title, url) for url, title in PAGE_TAG.findall(nfm))
    databases = tuple(DatabaseRef(title, url, source or None) for url, source, title in DATABASE_TAG.findall(nfm))
    text = DATABASE_TAG.sub(lambda m: m[3], PAGE_TAG.sub(lambda m: m[2], nfm))
    return ConvertedPage(f"[converted] {text}".strip(), pages, databases)


def render_row(title: str, properties: Mapping[str, Any], body: ConvertedPage) -> str:
    return f"# {title}\n\n| {' | '.join(sorted(properties))} |\n\n{body.markdown}\n"


def scrub_original(nfm: str) -> str:
    return EMAIL.sub("", nfm)


FAKE_CONVERTER = Converter(to_markdown, render_row, scrub_original)


def importer(connections: FakeNotionConnections, link: NotionLink) -> NotionImporter:
    return NotionImporter(connections, NotionImport(link), FAKE_CONVERTER)


async def run(link: str, connections: FakeNotionConnections | None = None, **kwargs: Any) -> RecordingWriter:
    writer = RecordingWriter()
    connections = connections or FakeNotionConnections()
    await importer(connections, NotionLink(link, **kwargs))(notion_job(uuid.UUID(int=1)), as_writer(writer))
    return writer


def page_line(page_id: str, title: str) -> str:
    return f'<page url="https://app.notion.com/p/{page_id.replace("-", "")}">\n{title}\n</page>'


def add_page(fake: FakeNotionGateway, page_id: str, title: str, nfm: str, parent: str = ROOT_PAGE_ID) -> str:
    template = fake.pages[ROOT_PAGE_ID]
    page_id = normal(page_id)
    fake.pages[page_id] = NotionPage(
        page_id, f"https://app.notion.com/p/{page_id.replace('-', '')}", title, template.parent, None
    )
    fake.markdown[page_id] = PageMarkdown(nfm)
    return page_id


def pid(n: int) -> str:
    return f"fa4e1000-0000-4000-8000-{n:012x}"


async def test_a_page_tree_declares_what_it_finds_and_stores_each_page_with_its_title_as_the_heading() -> None:
    writer = await run(WIKI_PAGE_ID)

    assert writer.declared == [
        (f"page:{ONBOARDING}", "Onboarding"),
        (f"page:{RUNBOOKS}", "Runbooks"),
        (f"page:{RUNBOOK}", "Runbook - Points backfill"),
    ]
    assert list(writer.added) == [f"page:{WIKI_PAGE_ID}", f"page:{ONBOARDING}", f"page:{RUNBOOKS}", f"page:{RUNBOOK}"]
    name, markdown, _original = writer.added[f"page:{WIKI_PAGE_ID}"]
    assert name == "Engineering Wiki"
    assert markdown.startswith("# Engineering Wiki\n\n[converted] Root of a nested wiki.")
    assert "<page" not in markdown and writer.failed == []


async def test_the_original_of_a_page_is_slim_json_with_the_page_fields_and_the_scrubbed_markdown() -> None:
    fake = FakeNotionConnections()
    nfm = f'Owner <mention url="user://u-1/person@example.com">Sample Author</mention> {CANARY}'
    fake.fake.markdown[PRD_PAGE_ID] = PageMarkdown(nfm)

    writer = await run(PRD_PAGE_ID, fake)

    original = writer.added[f"page:{PRD_PAGE_ID}"][2]
    stored = json.loads(original)
    assert set(stored) == {"page", "markdown"} and set(stored["page"]) == {"id", "url", "title", "last_edited_time"}
    assert stored["page"]["id"] == PRD_PAGE_ID and stored["page"]["title"] == "PRD - Checkout Loyalty Points"
    assert stored["page"]["last_edited_time"] is not None
    assert CANARY in stored["markdown"] and "@example.com" not in original.decode()
    for hidden in ("properties", "created_by", "last_edited_by", "person@example.com"):
        assert hidden not in original.decode()


async def test_a_database_link_stores_its_rows_as_properties_table_and_body() -> None:
    writer = await run(REQUIREMENTS_DATABASE_ID)

    assert len(writer.added) == 3 and [d[1] for d in writer.declared][0] == "Show earned points on checkout summary"
    name, markdown, original = writer.added["page:423ed336-0645-81d7-96ef-e0d042cb7f97"]
    assert name == "Show earned points on checkout summary"
    assert markdown.startswith("# Show earned points on checkout summary\n\n| ")
    assert "Owner" in markdown and "[converted]" in markdown
    stored = json.loads(original)
    assert set(stored) == {"page", "markdown"}
    assert "person@example.com" not in original.decode() and "properties" not in stored["page"]
    assert not any(external == f"page:{REQUIREMENTS_DATABASE_ID}" for external in writer.added)


async def test_a_page_is_probed_first_and_a_database_only_when_the_page_read_says_it_is_one() -> None:
    fake = FakeNotionConnections()

    await run(REQUIREMENTS_DATABASE_ID, fake, view="v1")
    await run(PRD_PAGE_ID, fake)

    methods = [c.method for c in fake.fake.calls]
    assert methods[:3] == ["page", "database", "query_data_source"]
    assert methods.count("database") == 1  # a page link never reads a database


async def test_a_page_that_holds_a_database_follows_its_rows_through_the_data_source() -> None:
    fake = FakeNotionConnections()
    database = (
        f'<database url="https://app.notion.com/p/{REQUIREMENTS_DATABASE_ID.replace("-", "")}" '
        f'data-source-url="collection://{REQUIREMENTS_DATA_SOURCE_ID}">\nRequirements DB\n</database>'
    )
    fake.fake.markdown[WIKI_PAGE_ID] = PageMarkdown(f"Intro\n\n{database}\n\n{database}")

    writer = await run(WIKI_PAGE_ID, fake)

    assert len(writer.added) == 1 + 3  # the page and the three rows; the second reference is the same data source
    assert len(fake.fake.called("query_data_source")) == 1


async def test_the_rows_of_a_database_are_read_over_every_result_page(monkeypatch) -> None:
    monkeypatch.setattr(notion_module, "MAX_PAGES", 500)
    fake = FakeNotionConnections()
    ids = [add_page(fake.fake, pid(n), f"Row {n}", "body") for n in range(130)]
    fake.fake.rows[REQUIREMENTS_DATA_SOURCE_ID] = ids

    writer = await run(REQUIREMENTS_DATABASE_ID, fake)

    assert len(writer.added) == 130
    assert [c.kwargs["start_cursor"] for c in fake.fake.called("query_data_source")] == [None, "100"]


async def test_a_row_body_with_sub_pages_and_the_row_itself_linked_twice_are_stored_once() -> None:
    fake = FakeNotionConnections()
    row = "423ed336-0645-81d7-96ef-e0d042cb7f97"
    child = add_page(fake.fake, pid(1), "Row child", "child body")
    fake.fake.markdown[row] = PageMarkdown(f"{page_line(child, 'Row child')}\n{page_line(row, 'Itself')}")

    writer = await run(REQUIREMENTS_DATABASE_ID, fake)

    assert list(writer.added).count(f"page:{child}") == 1 and len(writer.added) == 4
    assert len(fake.fake.called("page_markdown")) == 4


async def test_cycles_between_pages_end_with_each_page_stored_once() -> None:
    fake = FakeNotionConnections()
    a, b = pid(1), pid(2)
    add_page(fake.fake, a, "A", f"{page_line(b, 'B')} {page_line(a, 'A')}")
    add_page(fake.fake, b, "B", f"{page_line(a, 'A')} {page_line(b, 'B')}")

    writer = await run(a, fake)

    assert list(writer.added) == [f"page:{a}", f"page:{b}"] and writer.declared == [(f"page:{b}", "B")]
    assert len(fake.fake.called("page_markdown")) == 2


async def test_the_cap_counts_pages_and_rows_together_and_is_logged_as_counts(monkeypatch, caplog) -> None:
    monkeypatch.setattr(notion_module, "MAX_PAGES", 4)
    fake = FakeNotionConnections()
    root = pid(1)
    kids = [pid(n) for n in range(10, 16)]
    add_page(fake.fake, root, "Root", "\n".join(page_line(k, "Secret title") for k in kids))
    for k in kids:
        add_page(fake.fake, k, f"Kid {k[-2:]}", CANARY)

    with caplog.at_level(logging.INFO):
        writer = await run(root, fake)

    assert list(writer.added) == [f"page:{root}", *(f"page:{k}" for k in kids[:3])]
    assert "api.imports.page_cap_reached" in caplog.text
    assert CANARY not in caplog.text and "Secret title" not in caplog.text and "Kid" not in caplog.text


async def test_a_database_over_the_cap_stops_paging_at_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(notion_module, "MAX_PAGES", 3)
    fake = FakeNotionConnections()
    fake.fake.rows[REQUIREMENTS_DATA_SOURCE_ID] = [add_page(fake.fake, pid(n), f"Row {n}", "x") for n in range(120)]

    writer = await run(REQUIREMENTS_DATABASE_ID, fake)

    assert len(writer.added) == 3 and len(fake.fake.called("query_data_source")) == 1


async def test_the_default_cap_is_one_hundred_pages_and_rows() -> None:
    assert notion_module.MAX_PAGES == 100


async def test_a_page_the_member_cannot_see_is_skipped_and_counted_and_the_others_are_stored() -> None:
    fake = FakeNotionConnections()
    fake.fake.unshare(ONBOARDING)

    writer = await run(WIKI_PAGE_ID, fake)

    assert writer.failed == [(f"page:{ONBOARDING}", "no_access", "read")]
    assert list(writer.added) == [f"page:{WIKI_PAGE_ID}", f"page:{RUNBOOKS}", f"page:{RUNBOOK}"]


async def test_a_row_and_a_data_source_the_member_cannot_see_are_skipped() -> None:
    fake = FakeNotionConnections()
    del fake.fake.markdown["423ed336-0645-81dc-893f-cc140e89d124"]  # listed by the query, its body is not shared
    database = (
        f'<database url="https://app.notion.com/p/x" data-source-url="collection://{REQUIREMENTS_DATA_SOURCE_ID}">'
        "\nRows\n</database>"
    )
    other = (
        '<database url="https://app.notion.com/p/y" data-source-url="collection://'
        + pid(99)
        + '">\nHidden\n</database>'
    )
    fake.fake.markdown[WIKI_PAGE_ID] = PageMarkdown(f"{database}\n{other}")

    writer = await run(WIKI_PAGE_ID, fake)

    assert ("page:423ed336-0645-81dc-893f-cc140e89d124", "no_access", "read") in writer.failed
    assert (f"database:{pid(99)}", "no_access", "read") in writer.failed
    assert len(writer.added) == 3  # the wiki page and the two rows left


async def test_a_page_too_deep_to_convert_is_one_item_in_error_and_the_import_goes_on() -> None:
    fake = FakeNotionConnections()
    fake.fake.markdown[RUNBOOKS] = PageMarkdown(f"{TOO_DEEP}\n{page_line(RUNBOOK, 'Runbook')}")

    writer = await run(WIKI_PAGE_ID, fake)

    assert writer.failed == [(f"page:{RUNBOOKS}", "convert_failed", "convert")]
    assert list(writer.added) == [f"page:{WIKI_PAGE_ID}", f"page:{ONBOARDING}"]


async def test_an_original_the_scrub_cannot_handle_is_a_convert_failure_of_that_item() -> None:
    def refuse(_nfm: str) -> str:
        raise MarkupTooDeepError

    writer = RecordingWriter()
    converter = Converter(to_markdown, render_row, refuse)
    await NotionImporter(FakeNotionConnections(), NotionImport(NotionLink(PRD_PAGE_ID)), converter)(
        notion_job(uuid.UUID(int=1)), as_writer(writer)
    )

    assert writer.failed == [(f"page:{PRD_PAGE_ID}", "convert_failed", "convert")] and writer.added == {}


async def test_an_empty_title_is_untitled_and_an_empty_row_body_is_stored() -> None:
    fake = FakeNotionConnections()
    empty_row = "423ed336-0645-81ec-b3ae-e35ee7c4de54"
    page = fake.fake.pages[empty_row]
    fake.fake.pages[empty_row] = NotionPage(page.id, page.url, "", page.parent, None, properties=page.properties)

    writer = await run(REQUIREMENTS_DATABASE_ID, fake)

    assert writer.added[f"page:{empty_row}"][0] == "Untitled"


@pytest.mark.parametrize("target", [ROOT_PAGE_ID, REQUIREMENTS_DATABASE_ID])
async def test_a_target_that_cannot_be_read_fails_the_source_with_no_access(target) -> None:
    fake = FakeNotionConnections()
    fake.fake.unshare(target)

    with pytest.raises(ImportFailedError) as caught:
        await run(target, fake)

    assert caught.value.reason == "no_access"


async def test_a_trashed_target_and_an_unknown_one_fail_the_source_with_no_access() -> None:
    fake = FakeNotionConnections()
    page = fake.fake.pages[PRD_PAGE_ID]
    fake.fake.pages[PRD_PAGE_ID] = NotionPage(page.id, page.url, page.title, page.parent, None, in_trash=True)

    for target in (PRD_PAGE_ID, pid(404)):
        with pytest.raises(ImportFailedError) as caught:
            await run(target, fake)
        assert caught.value.reason == "no_access"


async def test_a_target_page_that_cannot_be_read_as_markdown_fails_the_source() -> None:
    fake = FakeNotionConnections()
    fake.fake.fail("page_markdown", NotionCode.NOT_SHARED)

    with pytest.raises(ImportFailedError) as caught:
        await run(PRD_PAGE_ID, fake)

    assert caught.value.reason == "no_access"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (NotionCode.RATE_LIMITED, "unknown_error"),
        (NotionCode.UNREACHABLE, "unknown_error"),
        (NotionCode.AUTH_FAILED, "unknown_error"),
        (NotionCode.PERMISSIONS_MISSING, "unknown_error"),
        (NotionCode.RECONNECT_REQUIRED, "no_access"),
    ],
)
async def test_a_notion_failure_in_the_middle_fails_the_source_with_a_closed_reason(code, reason) -> None:
    fake = FakeNotionConnections()
    fake.fake.fail("page_markdown", code)

    with pytest.raises(ImportFailedError) as caught:
        await run(WIKI_PAGE_ID, fake)

    assert caught.value.reason == reason


async def test_a_member_without_a_connection_or_with_an_expired_one_fails_the_source_with_no_access() -> None:
    fake = FakeNotionConnections()
    fake.expire(ACTING)
    with pytest.raises(ImportFailedError) as expired:
        await run(PRD_PAGE_ID, fake)
    del fake.users[ACTING]
    with pytest.raises(ImportFailedError) as missing:
        await run(PRD_PAGE_ID, fake)

    assert (expired.value.reason, missing.value.reason) == ("no_access", "no_access") and fake.fake.calls == []


async def test_a_row_query_that_fails_for_the_root_database_fails_the_source() -> None:
    fake = FakeNotionConnections()
    fake.fake.fail("query_data_source", NotionCode.UNREACHABLE)

    with pytest.raises(ImportFailedError) as caught:
        await run(REQUIREMENTS_DATABASE_ID, fake)

    assert caught.value.reason == "unknown_error"


async def test_the_member_of_the_job_is_the_one_whose_gateway_is_asked() -> None:
    fake = FakeNotionConnections()

    await run(PRD_PAGE_ID, fake)

    assert fake.gateways_asked == [ACTING]


async def test_a_second_run_after_a_restart_names_the_same_items_and_a_gone_source_stops_it() -> None:
    first, second = await run(WIKI_PAGE_ID), await run(WIKI_PAGE_ID)

    assert first.declared == second.declared and list(first.added) == list(second.added)

    class Gone(RecordingWriter):
        async def declare(self, external_id: str, name: str) -> None:
            raise SourceGoneError

    with pytest.raises(SourceGoneError):
        await importer(FakeNotionConnections(), NotionLink(WIKI_PAGE_ID))(
            notion_job(uuid.UUID(int=1)), as_writer(Gone())
        )


async def test_inspect_probes_a_page_with_one_page_read_and_one_markdown_read(monkeypatch) -> None:
    monkeypatch.setattr("api.imports.notion.default_converter", lambda: FAKE_CONVERTER)
    gateway = FakeNotionConnections().fake

    found = await inspect(gateway, NotionImport(NotionLink(WIKI_PAGE_ID)))

    assert (found.kind, found.title, found.pages, found.capped) == ("page", "Engineering Wiki", 3, False)
    assert [c.method for c in gateway.calls] == ["page", "page_markdown"]


async def test_inspect_probes_a_database_with_one_page_of_rows(monkeypatch) -> None:
    monkeypatch.setattr("api.imports.notion.default_converter", lambda: FAKE_CONVERTER)
    gateway = FakeNotionConnections().fake

    found = await inspect(gateway, NotionImport(NotionLink(REQUIREMENTS_DATABASE_ID)))

    assert (found.kind, found.title, found.pages, found.capped) == ("database", "Requirements DB", 3, False)
    assert [c.method for c in gateway.calls] == ["page", "database", "query_data_source"]


async def test_inspect_says_capped_for_a_database_line_a_next_cursor_and_more_than_the_cap(monkeypatch) -> None:
    monkeypatch.setattr("api.imports.notion.default_converter", lambda: FAKE_CONVERTER)
    fake = FakeNotionConnections()
    line = f'<database url="https://app.notion.com/p/x" data-source-url="collection://{REQUIREMENTS_DATA_SOURCE_ID}">\nD\n</database>'
    fake.fake.markdown[WIKI_PAGE_ID] = PageMarkdown(f"{page_line(ONBOARDING, 'Onboarding')}\n{line}")
    with_database = await inspect(fake.fake, NotionImport(NotionLink(WIKI_PAGE_ID)))
    monkeypatch.setattr(notion_module, "MAX_PAGES", 2)
    over = await inspect(FakeNotionConnections().fake, NotionImport(NotionLink(WIKI_PAGE_ID)))
    fake.fake.rows[REQUIREMENTS_DATA_SOURCE_ID] = [add_page(fake.fake, pid(n), f"R{n}", "x") for n in range(130)]
    monkeypatch.setattr(notion_module, "MAX_PAGES", 500)
    rows = await inspect(fake.fake, NotionImport(NotionLink(REQUIREMENTS_DATABASE_ID)))

    assert (with_database.pages, with_database.capped) == (3, True)
    assert (over.pages, over.capped) == (2, True)
    assert (rows.pages, rows.capped) == (100, True)


async def test_inspect_of_a_page_too_deep_to_convert_counts_the_page_only_and_refuses_an_unseen_target(
    monkeypatch,
) -> None:
    monkeypatch.setattr("api.imports.notion.default_converter", lambda: FAKE_CONVERTER)
    fake = FakeNotionConnections()
    fake.fake.markdown[WIKI_PAGE_ID] = PageMarkdown(TOO_DEEP)

    found = await inspect(fake.fake, NotionImport(NotionLink(WIKI_PAGE_ID)))
    fake.fake.unshare(PRD_PAGE_ID)

    assert found.pages == 1
    with pytest.raises(Exception) as refused:
        await inspect(fake.fake, NotionImport(NotionLink(PRD_PAGE_ID)))
    assert str(refused.value) == "notion_not_shared"


async def test_a_database_line_without_a_data_source_url_reads_the_database_for_its_data_sources() -> None:
    fake = FakeNotionConnections()
    line = f'<database url="https://app.notion.com/p/{REQUIREMENTS_DATABASE_ID.replace("-", "")}" data-source-url="collection://">\nD\n</database>'
    fake.fake.markdown[WIKI_PAGE_ID] = PageMarkdown(line)

    writer = await run(WIKI_PAGE_ID, fake)

    assert len(writer.added) == 1 + 3 and len(fake.fake.called("database")) == 1


async def test_a_database_line_without_a_data_source_url_that_is_not_shared_is_a_skipped_item() -> None:
    fake = FakeNotionConnections()
    fake.fake.unshare(REQUIREMENTS_DATABASE_ID)
    line = f'<database url="https://app.notion.com/p/{REQUIREMENTS_DATABASE_ID.replace("-", "")}" data-source-url="collection://">\nD\n</database>'
    fake.fake.markdown[WIKI_PAGE_ID] = PageMarkdown(line)

    writer = await run(WIKI_PAGE_ID, fake)

    assert list(writer.added) == [f"page:{WIKI_PAGE_ID}"]
    assert [(r, st) for _id, r, st in writer.failed] == [("no_access", "read")]


async def test_a_link_the_gateway_calls_invalid_fails_the_source_with_no_access() -> None:
    fake = FakeNotionConnections()
    fake.fake.fail("page", NotionCode.LINK_INVALID)

    with pytest.raises(ImportFailedError) as caught:
        await run(PRD_PAGE_ID, fake)

    assert caught.value.reason == "no_access"
