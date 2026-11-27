"""``notion.shared.shared_objects``: what the pickers of the source and plan forms offer, over the fake's fixtures and
a stub search: pages and databases (never rows or trashed objects), the parent page's title, titles scrubbed, links the
routes parse, sorted by title, capped."""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from notion.fake import (
    PLAN_TRIAL_DATABASE_ID,
    PRD_PAGE_ID,
    REQUIREMENTS_DATABASE_ID,
    ROOT_PAGE_ID,
    WIKI_PAGE_ID,
    FakeNotionGateway,
)
from notion.gateway import NotionGateway
from notion.links import parse_link
from notion.model import NotionPage, SearchKind, SearchPage
from notion.shared import MAX_READS, MAX_SHARED, shared_objects


async def test_pages_and_databases_are_offered_by_title_with_their_parent_page_and_no_row() -> None:
    fake = FakeNotionGateway()

    found = await shared_objects(fake)

    titles = [(item.kind, item.title, item.parent_title) for item in found.items]
    assert titles[:3] == [
        ("page", "ADR-007 Use event sourcing for points ledger", "Specflow Notion Lab"),
        ("page", "Engineering Wiki", "Specflow Notion Lab"),
        ("page", "Onboarding", "Engineering Wiki"),
    ]
    assert ("page", "Specflow Notion Lab", None) in titles
    assert {item.id for item in found.items if item.kind == "database"} == {
        REQUIREMENTS_DATABASE_ID,
        PLAN_TRIAL_DATABASE_ID,
    }
    rows = {page.id for page in fake.pages.values() if page.parent.kind in {"database", "data_source"}}
    assert rows and rows.isdisjoint(item.id for item in found.items)
    assert [item.title.casefold() for item in found.items] == sorted(item.title.casefold() for item in found.items)
    assert all(parse_link(item.link).id == item.id for item in found.items)
    assert not found.capped
    assert [call.args[0] for call in fake.called("search")] == ["page", "data_source"]


async def test_an_unshared_or_trashed_object_is_not_offered_and_titles_are_scrubbed() -> None:
    fake = FakeNotionGateway()
    fake.unshare(WIKI_PAGE_ID)
    fake.pages[PRD_PAGE_ID] = replace(fake.pages[PRD_PAGE_ID], in_trash=True)
    fake.pages[ROOT_PAGE_ID] = replace(fake.pages[ROOT_PAGE_ID], title="Lab of person@example.com")

    found = await shared_objects(fake)

    ids = {item.id for item in found.items}
    assert WIKI_PAGE_ID not in ids and PRD_PAGE_ID not in ids
    root = next(item for item in found.items if item.id == ROOT_PAGE_ID)
    assert "@" not in root.title
    assert all("@" not in (item.parent_title or "") for item in found.items)


class EndlessSearch:
    """A search that always has another page of 100 pages."""

    def __init__(self, page: NotionPage) -> None:
        self.page = page
        self.reads: list[SearchKind] = []

    async def search(self, kind: SearchKind, *, start_cursor: str | None = None) -> SearchPage:
        self.reads.append(kind)
        if kind == "data_source":
            return SearchPage()
        n = int(start_cursor or 0) + 1  # the cursor is the number of the page read last
        pages = tuple(replace(self.page, id=f"{n:04d}{i:04d}-0000-0000-0000-000000000000") for i in range(100))
        return SearchPage(pages=pages, next_cursor=str(n))


async def test_a_workspace_with_more_than_the_list_holds_is_capped_after_a_bounded_number_of_reads() -> None:
    stub = EndlessSearch(FakeNotionGateway().pages[ROOT_PAGE_ID])

    found = await shared_objects(cast(NotionGateway, stub))  # only ``search`` is called

    assert found.capped and len(found.items) == MAX_SHARED
    assert stub.reads == ["page"] * MAX_READS + ["data_source"]
