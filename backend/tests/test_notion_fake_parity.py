"""One scenario, two gateways: ``FakeNotionGateway`` and ``HttpNotionGateway`` over Notion's recorded JSON must give
the same values and the same closed errors, so tests that use the fake stay true to the real client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from notion.fake import (
    ADR_PAGE_ID,
    EMPTY_BODY_ROW_ID,
    PLAN_TRIAL_DATA_SOURCE_ID,
    PLAN_TRIAL_DATABASE_ID,
    PRD_PAGE_ID,
    REQUIREMENTS_DATA_SOURCE_ID,
    REQUIREMENTS_DATABASE_ID,
    ROOT_PAGE_ID,
    WIKI_PAGE_ID,
    FakeNotionGateway,
)
from notion.gateway import NotionGateway
from notion.model import NotionPage
from notion.reasons import NotionCode, NotionError
from tests.notion_http_support import FixtureNotion, make_rig

MISSING = "423ed336-0645-8100-0000-000000000000"
SECRET_PAGE = WIKI_PAGE_ID


async def outcome(call: Callable[[], Awaitable[Any]]) -> Any:
    """What a call gave: its value, or the one closed code it raised."""

    try:
        return await call()
    except NotionError as error:
        return ("error", error.code)


def shape(page: NotionPage) -> tuple[Any, ...]:
    """A written page without what each side invents (ids, urls, how the properties are spelled)."""

    return (page.title, page.parent.kind, sorted(page.properties), page.in_trash)


async def scenario(gateway: NotionGateway) -> list[Any]:
    seen: list[Any] = []

    async def step(call: Callable[[], Awaitable[Any]]) -> Any:
        seen.append(await outcome(call))
        return seen[-1]

    # Reads of what exists.
    for page_id in (PRD_PAGE_ID, ADR_PAGE_ID, ROOT_PAGE_ID, EMPTY_BODY_ROW_ID):
        await step(lambda page_id=page_id: gateway.page(page_id))
        await step(lambda page_id=page_id: gateway.page_markdown(page_id))
    for database_id in (REQUIREMENTS_DATABASE_ID, PLAN_TRIAL_DATABASE_ID):
        await step(lambda database_id=database_id: gateway.database(database_id))
    for source_id in (REQUIREMENTS_DATA_SOURCE_ID, PLAN_TRIAL_DATA_SOURCE_ID):
        await step(lambda source_id=source_id: gateway.data_source(source_id))

    # Rows, page by page.
    cursor: str | None = None
    for _ in range(5):
        rows = await gateway.query_data_source(PLAN_TRIAL_DATA_SOURCE_ID, page_size=1, start_cursor=cursor)
        seen.append(rows)
        cursor = rows.next_cursor
        if cursor is None:
            break
    seen.append(await gateway.query_data_source(REQUIREMENTS_DATA_SOURCE_ID))

    # Failures: not found, wrong kind, not shared.
    await step(lambda: gateway.page(MISSING))
    await step(lambda: gateway.page_markdown(MISSING))
    await step(lambda: gateway.database(MISSING))
    await step(lambda: gateway.data_source(MISSING))
    await step(lambda: gateway.query_data_source(MISSING))
    await step(lambda: gateway.page(SECRET_PAGE))
    await step(lambda: gateway.page_markdown(SECRET_PAGE))
    await step(lambda: gateway.create_comment(SECRET_PAGE, "x"))
    await step(lambda: gateway.page(REQUIREMENTS_DATABASE_ID))
    await step(lambda: gateway.page_markdown(REQUIREMENTS_DATABASE_ID))
    await step(lambda: gateway.database(PRD_PAGE_ID))
    await step(lambda: gateway.create_database(MISSING, "x", {}))
    await step(lambda: gateway.create_page(MISSING, properties={}))
    await step(lambda: gateway.update_page(MISSING, properties={}))
    await step(lambda: gateway.replace_markdown(MISSING, "x"))
    await step(lambda: gateway.create_comment(MISSING, "x"))

    # Writes: a database, its self-relation, rows, a new body, a comment; read back through the same gateway.
    schema = {"Name": {"title": {}, "type": "title"}, "Points": {"number": {}, "type": "number"}}
    database = await gateway.create_database(ROOT_PAGE_ID, "Parity plan", schema)
    source_id = database.data_sources[0].id
    seen.append((database.title, database.parent, [s.name for s in database.data_sources]))
    source = await gateway.update_data_source(source_id, {"Parent": {"type": "relation", "relation": {}}})
    seen.append((source.title, source.database_id == database.id, sorted(source.properties)))
    props = {"Name": {"type": "title", "title": [{"text": {"content": "Story 1"}}]}}
    story = await gateway.create_page(source_id, properties=props, markdown="# Story\n\nbody")
    seen.append(shape(story))
    seen.append(await gateway.page_markdown(story.id))
    updated = await gateway.update_page(story.id, properties={"Points": {"type": "number", "number": 5}})
    seen.append(shape(updated))
    await gateway.replace_markdown(story.id, "replaced")
    seen.append(await gateway.page_markdown(story.id))
    await gateway.create_comment(story.id, "a question")
    listed = await gateway.query_data_source(source_id)
    seen.append([shape(row) for row in listed.rows])
    return seen


async def test_the_http_client_and_the_fake_give_the_same_values_and_codes() -> None:
    fake = FakeNotionGateway()
    notion = FixtureNotion()
    rig = make_rig(notion.handle)
    fake.unshare(SECRET_PAGE)
    notion.unshare(SECRET_PAGE)

    expected = await scenario(fake)
    actual = await scenario(rig.gateway)

    assert actual == expected
    assert [markdown for _page, markdown in fake.comments] == [markdown for _page, markdown in notion.comments]
    assert len(expected) >= 40  # the scenario is not vacuous
    assert ("error", NotionCode.NOT_A_PAGE) in expected and ("error", NotionCode.LINK_INVALID) in expected
    assert expected.count(("error", NotionCode.NOT_SHARED)) >= 10
    await rig.gateway.aclose()


async def test_an_unshared_page_is_not_shared_on_both_sides() -> None:
    fake = FakeNotionGateway()
    notion = FixtureNotion()
    rig = make_rig(notion.handle)
    fake.unshare(PRD_PAGE_ID)
    notion.unshare(PRD_PAGE_ID)
    not_shared = ("error", NotionCode.NOT_SHARED)

    for gateway in (fake, rig.gateway):
        assert await outcome(lambda g=gateway: g.page(PRD_PAGE_ID)) == not_shared
        assert await outcome(lambda g=gateway: g.page_markdown(PRD_PAGE_ID)) == not_shared
    assert await rig.gateway.query_data_source(REQUIREMENTS_DATA_SOURCE_ID) == await fake.query_data_source(
        REQUIREMENTS_DATA_SOURCE_ID
    )
