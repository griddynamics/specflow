"""``HttpNotionGateway`` and ``RateLimiter`` against Notion's recorded JSON (``notion/fixtures/``) on a mock transport:
reads, writes, pagination, the wrong-kind confirmation, closed errors, retries and the wait budget."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from notion import http as notion_http
from notion.fake import (
    ADR_PAGE_ID,
    PLAN_TRIAL_DATA_SOURCE_ID,
    PRD_PAGE_ID,
    REQUIREMENTS_DATA_SOURCE_ID,
    REQUIREMENTS_DATABASE_ID,
    ROOT_PAGE_ID,
    FakeNotionGateway,
)
from notion.http import HttpNotionGateway
from notion.limiter import RateLimiter, RateLimiters, WaitBudget
from notion.reasons import NotionCode, NotionError
from tests.notion_http_support import TOKEN, Clock, FixtureNotion, error, make_rig, reply

NOT_FOUND = "object_not_found"


@pytest.fixture(autouse=True)
def _no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff is the full delay (1, 2, 4, 8 s) so waits can be asserted."""

    monkeypatch.setattr(notion_http.random, "uniform", lambda _low, high: high)


def served() -> tuple[FixtureNotion, Any]:
    notion = FixtureNotion()
    return notion, make_rig(notion.handle)


async def code_of(awaitable: Any) -> NotionCode:
    with pytest.raises(NotionError) as raised:
        await awaitable
    return raised.value.code


# Reads --------------------------------------------------------------------------------------------------------------


async def test_reads_equal_what_the_fake_answers() -> None:
    _, rig = served()
    fake = FakeNotionGateway()

    assert await rig.gateway.page(PRD_PAGE_ID) == await fake.page(PRD_PAGE_ID)
    assert await rig.gateway.database(REQUIREMENTS_DATABASE_ID) == await fake.database(REQUIREMENTS_DATABASE_ID)
    assert await rig.gateway.data_source(PLAN_TRIAL_DATA_SOURCE_ID) == await fake.data_source(PLAN_TRIAL_DATA_SOURCE_ID)
    assert await rig.gateway.page_markdown(ADR_PAGE_ID) == await fake.page_markdown(ADR_PAGE_ID)


async def test_every_request_carries_the_token_the_version_and_markdown_v2() -> None:
    _, rig = served()

    await rig.gateway.page_markdown(PRD_PAGE_ID)
    await rig.gateway.page(PRD_PAGE_ID)

    first, second = rig.requests
    assert first.url.path == f"/v1/pages/{PRD_PAGE_ID}/markdown"
    assert first.url.params["markdown_version"] == "v2"
    assert second.url.params.get("markdown_version") is None
    for request in rig.requests:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["notion-version"] == "2026-03-11"
    assert len(rig.tokens) == 2  # the token source is asked once per request


async def test_a_plain_id_reaches_notion_as_given() -> None:
    _, rig = served()

    await rig.gateway.page(PRD_PAGE_ID.replace("-", ""))
    assert rig.requests[-1].url.path == f"/v1/pages/{PRD_PAGE_ID.replace('-', '')}"


async def test_a_hostile_id_stays_one_path_segment() -> None:
    rig = make_rig(lambda _r: error(404, NOT_FOUND))

    assert await code_of(rig.gateway.page("../users/me")) is NotionCode.NOT_SHARED
    assert rig.requests[0].url.raw_path == b"/v1/pages/..%2Fusers%2Fme"


async def test_query_answers_one_page_and_the_caller_follows_the_cursor() -> None:
    notion, rig = served()
    fake = FakeNotionGateway()

    first = await rig.gateway.query_data_source(PLAN_TRIAL_DATA_SOURCE_ID, page_size=3)
    expected = await fake.query_data_source(PLAN_TRIAL_DATA_SOURCE_ID, page_size=3)
    assert first == expected
    assert first.next_cursor is not None

    second = await rig.gateway.query_data_source(PLAN_TRIAL_DATA_SOURCE_ID, start_cursor=first.next_cursor, page_size=3)
    assert second == await fake.query_data_source(
        PLAN_TRIAL_DATA_SOURCE_ID, start_cursor=first.next_cursor, page_size=3
    )
    assert second.next_cursor is None
    assert json.loads(rig.requests[1].content) == {"page_size": 3, "start_cursor": first.next_cursor}
    assert len(notion.rows[PLAN_TRIAL_DATA_SOURCE_ID]) == len(first.rows) + len(second.rows)


async def test_query_sends_the_filter_and_clamps_the_page_size() -> None:
    notion, rig = served()
    row = notion.pages[notion.rows[PLAN_TRIAL_DATA_SOURCE_ID][0]]
    name = next(k for k, v in row["properties"].items() if "rich_text" in v)
    where = {"property": name, "rich_text": {"equals": "no such value"}}

    rows = await rig.gateway.query_data_source(PLAN_TRIAL_DATA_SOURCE_ID, where=where, page_size=500)

    assert rows.rows == () and rows.next_cursor is None
    assert json.loads(rig.requests[0].content) == {"page_size": 100, "filter": where}


async def test_query_with_has_more_false_has_no_cursor_even_when_notion_sends_one() -> None:
    rig = make_rig(lambda _r: reply(200, {"results": [], "next_cursor": "abc", "has_more": False}))

    assert (await rig.gateway.query_data_source(REQUIREMENTS_DATA_SOURCE_ID)).next_cursor is None


# Markdown follow-up -------------------------------------------------------------------------------------------------


def markdown_server(pages: dict[str, dict[str, Any]]) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        block = request.url.path.split("/")[3]
        return reply(200, pages[block]) if block in pages else error(404, NOT_FOUND)

    return make_rig(handle)


async def test_unknown_blocks_are_read_and_appended_after_a_blank_line() -> None:
    rig = markdown_server(
        {
            "p": {"markdown": "top", "truncated": True, "unknown_block_ids": ["b1", "b2"]},
            "b1": {"markdown": "one", "truncated": True, "unknown_block_ids": ["b3"]},
            "b2": {"markdown": "", "truncated": False, "unknown_block_ids": []},
            "b3": {"markdown": "three", "truncated": False, "unknown_block_ids": []},
        }
    )

    result = await rig.gateway.page_markdown("p")

    assert (result.markdown, result.truncated, result.unknown_block_ids) == ("top\n\none\n\nthree", False, ())
    assert [r.url.path.split("/")[3] for r in rig.requests] == ["p", "b1", "b2", "b3"]
    assert all(r.url.params["markdown_version"] == "v2" for r in rig.requests)


async def test_a_block_is_read_once_even_when_notion_names_it_again() -> None:
    rig = markdown_server(
        {
            "p": {"markdown": "top", "truncated": True, "unknown_block_ids": ["b1", "b1"]},
            "b1": {"markdown": "one", "truncated": True, "unknown_block_ids": ["p", "b1"]},
        }
    )

    result = await rig.gateway.page_markdown("p")

    assert result.markdown == "top\n\none" and not result.truncated
    assert len(rig.requests) == 2


async def test_the_follow_up_stops_at_its_bound_and_names_what_is_unread() -> None:
    ids = [f"b{n}" for n in range(notion_http.MAX_FOLLOW_UPS + 3)]
    pages = {"p": {"markdown": "top", "truncated": True, "unknown_block_ids": ids}}
    pages.update({i: {"markdown": i, "truncated": False, "unknown_block_ids": []} for i in ids})
    rig = markdown_server(pages)

    result = await rig.gateway.page_markdown("p")

    assert result.truncated is True
    assert result.unknown_block_ids == tuple(ids[notion_http.MAX_FOLLOW_UPS :])
    assert len(rig.requests) == 1 + notion_http.MAX_FOLLOW_UPS


async def test_a_page_cut_by_size_stays_truncated_without_a_follow_up() -> None:
    rig = markdown_server({"p": {"markdown": "top", "truncated": True, "unknown_block_ids": []}})

    result = await rig.gateway.page_markdown("p")

    assert (result.markdown, result.truncated, result.unknown_block_ids) == ("top", True, ())
    assert len(rig.requests) == 1


async def test_a_follow_up_that_notion_refuses_fails_the_read_with_its_closed_code() -> None:
    rig = markdown_server({"p": {"markdown": "top", "truncated": True, "unknown_block_ids": ["gone"]}})

    assert await code_of(rig.gateway.page_markdown("p")) is NotionCode.NOT_SHARED


# Wrong-kind ids -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("read", ["page", "page_markdown"])
async def test_a_database_id_on_a_page_route_is_confirmed_with_one_database_call(read: str) -> None:
    _, rig = served()

    code = await code_of(getattr(rig.gateway, read)(REQUIREMENTS_DATABASE_ID))

    assert code is NotionCode.NOT_A_PAGE
    assert [r.url.path.split("/")[2] for r in rig.requests] == ["pages", "databases"]


async def test_a_page_id_on_the_database_route_is_confirmed_with_one_page_call() -> None:
    _, rig = served()

    assert await code_of(rig.gateway.database(PRD_PAGE_ID)) is NotionCode.LINK_INVALID
    assert [r.url.path.split("/")[2] for r in rig.requests] == ["databases", "pages"]


async def test_a_400_that_the_other_route_does_not_explain_is_item_invalid() -> None:
    answers = iter([error(400, "validation_error"), error(404, NOT_FOUND)])
    rig = make_rig(lambda _r: next(answers))
    assert await code_of(rig.gateway.page("x")) is NotionCode.ITEM_INVALID
    assert len(rig.requests) == 2

    answers = iter([error(400, "validation_error"), error(400, "validation_error")])
    rig = make_rig(lambda _r: next(answers))
    assert await code_of(rig.gateway.database("x")) is NotionCode.ITEM_INVALID


async def test_the_confirmation_call_passes_on_a_real_failure_of_its_own() -> None:
    answers = iter([error(400, "validation_error"), error(403, "restricted_resource")])
    rig = make_rig(lambda _r: next(answers))

    assert await code_of(rig.gateway.page("x")) is NotionCode.PERMISSIONS_MISSING


async def test_the_message_of_a_400_is_never_read() -> None:
    for message in ("Provided ID is a database, not a page.", "", "something else entirely"):
        rig = make_rig(lambda r, m=message: wrong_kind_answer(r, m))  # type: ignore[misc]

        assert await code_of(rig.gateway.page("d")) is NotionCode.NOT_A_PAGE


def wrong_kind_answer(request: httpx.Request, message: str) -> httpx.Response:
    if request.url.path.startswith("/v1/pages"):
        return reply(400, {"code": "validation_error", "message": message})
    return reply(200, {"id": "d", "object": "database"})


# Closed errors ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "unauthorized", NotionCode.AUTH_FAILED),
        (403, "restricted_resource", NotionCode.PERMISSIONS_MISSING),
        (404, NOT_FOUND, NotionCode.NOT_SHARED),
        (400, "invalid_request", NotionCode.ITEM_INVALID),
        (400, "invalid_json", NotionCode.UNREACHABLE),
        (409, "conflict_error", NotionCode.UNREACHABLE),
        (418, "teapot", NotionCode.UNREACHABLE),
    ],
)
async def test_a_refusal_is_one_closed_code_and_carries_no_provider_text(
    status: int, code: str, expected: NotionCode
) -> None:
    rig = make_rig(lambda _r: error(status, code))

    with pytest.raises(NotionError) as raised:
        await rig.gateway.data_source("x")

    assert raised.value.code is expected
    assert str(raised.value) == expected.value
    assert "PROVIDER-TEXT" not in repr(raised.value) and TOKEN not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert len(rig.requests) == 1


async def test_a_401_is_never_retried_on_any_method() -> None:
    rig = make_rig(lambda _r: error(401, "unauthorized"))

    assert await code_of(rig.gateway.page("x")) is NotionCode.AUTH_FAILED
    assert await code_of(rig.gateway.create_comment("x", "hi")) is NotionCode.AUTH_FAILED
    assert len(rig.requests) == 2 and rig.clock.sleeps == []


async def test_an_answer_that_is_not_a_notion_object_is_unreachable() -> None:
    for response in (httpx.Response(200, text="<html>"), reply(200, []), reply(200, {"no": "id"})):
        rig = make_rig(lambda _r, r=response: r)  # type: ignore[misc]
        assert await code_of(rig.gateway.page("x")) is NotionCode.UNREACHABLE
    rig = make_rig(lambda _r: reply(200, {"results": "no"}))
    assert await code_of(rig.gateway.query_data_source("x")) is NotionCode.UNREACHABLE


# Retries and the wait budget ----------------------------------------------------------------------------------------


def sequence(*answers: httpx.Response | Exception) -> Any:
    left = list(answers)

    def handle(_request: httpx.Request) -> httpx.Response:
        item = left.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return handle


PAGE = {"id": "p", "url": "u", "properties": {}}


async def test_a_429_honours_retry_after_through_the_limiter_and_succeeds() -> None:
    rig = make_rig(sequence(error(429, "rate_limited", **{"retry-after": "3"}), reply(200, PAGE)))

    assert (await rig.gateway.page("p")).id == "p"
    assert rig.clock.sleeps == [3.0]
    assert rig.budget.remaining == 57.0


async def test_a_429_holds_every_caller_of_the_same_bot() -> None:
    rig = make_rig(sequence(error(429, "rate_limited", **{"retry-after": "5"}), reply(200, PAGE)))
    other = HttpNotionGateway(
        _token,
        rig.limiter,
        base_url="https://notion.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: reply(200, PAGE))),
        sleep=rig.clock.sleep,  # noqa: E501
    )

    await rig.gateway.page("p")
    other_budget_before = other._budget.remaining  # noqa: SLF001
    await other.page("p")

    assert rig.clock.sleeps == [5.0] and other_budget_before == 60.0


async def _token() -> str:
    return TOKEN


async def test_a_529_is_retried_with_backoff_when_it_names_no_retry_after() -> None:
    rig = make_rig(sequence(error(529, "service_unavailable"), error(429, "rate_limited"), reply(200, PAGE)))

    await rig.gateway.page("p")

    assert rig.clock.sleeps == [1.0, 2.0]


async def test_writes_are_retried_on_429_too() -> None:
    rig = make_rig(sequence(error(429, "rate_limited", **{"retry-after": "1"}), reply(200, {})))

    await rig.gateway.create_comment("p", "hi")

    assert len(rig.requests) == 2


async def test_five_attempts_then_the_status_decides_the_code() -> None:
    rig = make_rig(lambda _r: error(429, "rate_limited", **{"retry-after": "1"}))
    assert await code_of(rig.gateway.page("p")) is NotionCode.RATE_LIMITED
    assert len(rig.requests) == notion_http.MAX_ATTEMPTS

    rig = make_rig(lambda _r: error(529, "service_unavailable"))
    assert await code_of(rig.gateway.page("p")) is NotionCode.UNREACHABLE
    assert len(rig.requests) == notion_http.MAX_ATTEMPTS
    assert rig.clock.sleeps == [1.0, 2.0, 4.0, 8.0]


async def test_a_retry_after_that_does_not_fit_the_budget_is_rate_limited_without_waiting() -> None:
    rig = make_rig(lambda _r: error(429, "rate_limited", **{"retry-after": "600"}), budget=30.0)

    assert await code_of(rig.gateway.page("p")) is NotionCode.RATE_LIMITED
    assert rig.clock.sleeps == [] and len(rig.requests) == 1


async def test_retry_after_is_capped_at_sixty_seconds() -> None:
    rig = make_rig(sequence(error(429, "rate_limited", **{"retry-after": "600"}), reply(200, PAGE)))

    await rig.gateway.page("p")

    assert rig.clock.sleeps == [60.0] and rig.budget.remaining == 0.0


async def test_the_budget_is_shared_by_every_call_of_the_gateway() -> None:
    rig = make_rig(
        sequence(
            error(429, "rate_limited", **{"retry-after": "40"}),
            reply(200, PAGE),
            error(429, "rate_limited", **{"retry-after": "40"}),
        )
    )

    await rig.gateway.page("p")
    assert await code_of(rig.gateway.page("p")) is NotionCode.RATE_LIMITED


async def test_a_5xx_is_retried_for_a_get_and_for_the_read_only_query() -> None:
    rig = make_rig(sequence(error(500, "internal_server_error"), error(502, "bad_gateway"), reply(200, PAGE)))
    assert (await rig.gateway.page("p")).id == "p"
    assert rig.clock.sleeps == [1.0, 2.0]

    rig = make_rig(sequence(error(503, "service_unavailable"), reply(200, {"results": [], "has_more": False})))
    assert (await rig.gateway.query_data_source("s")).rows == ()


async def test_a_5xx_is_not_retried_for_a_write() -> None:
    calls = [
        lambda g: g.create_page("s", properties={}),
        lambda g: g.update_page("p", properties={}),
        lambda g: g.replace_markdown("p", "x"),
        lambda g: g.create_comment("p", "x"),
        lambda g: g.create_database("p", "t", {}),
        lambda g: g.update_data_source("s", {}),
    ]
    for call in calls:
        rig = make_rig(lambda _r: error(500, "internal_server_error"))

        assert await code_of(call(rig.gateway)) is NotionCode.UNREACHABLE
        assert len(rig.requests) == 1 and rig.clock.sleeps == []


async def test_backoff_waits_count_against_the_budget() -> None:
    rig = make_rig(lambda _r: error(500, "internal_server_error"), budget=2.5)

    assert await code_of(rig.gateway.page("p")) is NotionCode.UNREACHABLE
    assert rig.clock.sleeps == [1.0] and len(rig.requests) == 2


async def test_a_transport_error_is_retried_for_a_read_and_for_an_unopened_connection() -> None:
    rig = make_rig(sequence(httpx.ReadError("x"), httpx.ConnectError("x"), reply(200, PAGE)))
    assert (await rig.gateway.page("p")).id == "p"

    rig = make_rig(sequence(httpx.ConnectTimeout("x"), reply(200, {})))
    await rig.gateway.create_comment("p", "hi")
    assert len(rig.requests) == 2


async def test_a_write_that_broke_in_flight_is_not_repeated() -> None:
    rig = make_rig(sequence(httpx.ReadError("secret detail")))

    with pytest.raises(NotionError) as raised:
        await rig.gateway.create_comment("p", "hi")

    assert raised.value.code is NotionCode.UNREACHABLE and raised.value.__cause__ is None
    assert len(rig.requests) == 1


async def test_a_dead_network_ends_in_unreachable_after_five_attempts() -> None:
    rig = make_rig(lambda _r: (_ for _ in ()).throw(httpx.ConnectError("x")))

    assert await code_of(rig.gateway.page("p")) is NotionCode.UNREACHABLE
    assert len(rig.requests) == notion_http.MAX_ATTEMPTS


# Writes -------------------------------------------------------------------------------------------------------------


async def test_write_payloads_follow_the_notion_api() -> None:
    notion, rig = served()
    schema = {"Name": {"title": {}}, "Points": {"number": {}}}

    database = await rig.gateway.create_database(ROOT_PAGE_ID, "Plan", schema)
    source_id = database.data_sources[0].id
    await rig.gateway.update_data_source(source_id, {"Parent": {"relation": {"data_source_id": source_id}}})
    page = await rig.gateway.create_page(
        source_id, properties={"Name": {"title": [{"text": {"content": "Story"}}]}}, markdown="# body"
    )
    bare = await rig.gateway.create_page(source_id, properties={"Name": {"title": [{"text": {"content": "B"}}]}})
    updated = await rig.gateway.update_page(page.id, properties={"Points": {"number": 3}})
    await rig.gateway.replace_markdown(page.id, "new body")
    await rig.gateway.create_comment(page.id, "**hi**")

    bodies = [(r.method, r.url.path, json.loads(r.content)) for r in rig.requests]
    assert bodies[0] == (
        "POST",
        "/v1/databases",
        {
            "parent": {"type": "page_id", "page_id": ROOT_PAGE_ID},
            "title": [{"type": "text", "text": {"content": "Plan"}}],
            "initial_data_source": {"properties": schema},
        },
    )
    assert bodies[1][:2] == ("PATCH", f"/v1/data_sources/{source_id}")
    assert bodies[2][2] == {
        "parent": {"type": "data_source_id", "data_source_id": source_id},
        "properties": {"Name": {"title": [{"text": {"content": "Story"}}]}},
        "markdown": "# body",
        "markdown_version": "v2",
    }
    assert "markdown" not in bodies[3][2]
    assert bodies[4] == ("PATCH", f"/v1/pages/{page.id}", {"properties": {"Points": {"number": 3}}})
    assert bodies[5] == (
        "PATCH",
        f"/v1/pages/{page.id}/markdown",
        {"markdown_version": "v2", "type": "replace_content", "replace_content": {"new_str": "new body"}},
    )
    assert bodies[6] == ("POST", "/v1/comments", {"parent": {"page_id": page.id}, "markdown": "**hi**"})
    assert (database.title, page.title, bare.title, updated.id) == ("Plan", "Story", "B", page.id)
    assert notion.markdown[page.id]["markdown"] == "new body" and notion.comments == [(page.id, "**hi**")]


# Client -------------------------------------------------------------------------------------------------------------


async def test_aclose_closes_a_client_it_made_and_leaves_one_it_was_given() -> None:
    _, rig = served()
    await rig.gateway.aclose()
    assert not rig.gateway._client.is_closed  # noqa: SLF001

    own = HttpNotionGateway(_token, RateLimiter(60))
    await own.aclose()  # nothing made yet
    client = own._http()  # noqa: SLF001
    assert client.trust_env is False
    await own.aclose()
    assert client.is_closed


# RateLimiter and WaitBudget -----------------------------------------------------------------------------------------


def limiter(per_minute: int) -> tuple[RateLimiter, Clock]:
    clock = Clock()
    return RateLimiter(per_minute, clock=clock.time, sleep=clock.sleep), clock


async def test_a_limiter_lets_per_minute_requests_through_then_waits_for_the_oldest_to_age_out() -> None:
    gate, clock = limiter(3)

    for _ in range(3):
        await gate.acquire()
        clock.now += 1.0
    assert clock.sleeps == []

    await gate.acquire()  # the first left 3 s ago: 57 s to wait
    assert clock.sleeps == [57.0]
    await gate.acquire()  # the second left 58 s ago
    assert clock.sleeps == [57.0, 1.0]


async def test_a_limiter_wait_that_does_not_fit_the_budget_raises_before_sleeping() -> None:
    gate, clock = limiter(1)
    await gate.acquire()
    budget = WaitBudget(10.0)

    with pytest.raises(NotionError) as raised:
        await gate.acquire(budget)

    assert raised.value.code is NotionCode.RATE_LIMITED
    assert clock.sleeps == [] and budget.remaining == 10.0


async def test_hold_delays_the_next_request_and_only_ever_extends() -> None:
    gate, clock = limiter(100)
    gate.hold(5.0)
    gate.hold(2.0)
    budget = WaitBudget()

    await gate.acquire(budget)

    assert clock.sleeps == [5.0] and budget.remaining == 55.0
    await gate.acquire(budget)
    assert clock.sleeps == [5.0]


async def test_wait_budget_spends_down_and_refuses_what_does_not_fit() -> None:
    budget = WaitBudget(5.0)
    budget.spend(5.0)
    assert budget.remaining == 0.0
    with pytest.raises(NotionError) as raised:
        budget.spend(0.1)
    assert raised.value.code is NotionCode.RATE_LIMITED
    assert WaitBudget().remaining == 60.0


def test_one_limiter_per_bot() -> None:
    limiters = RateLimiters(150)

    assert limiters.for_bot("a") is limiters.for_bot("a")
    assert limiters.for_bot("a") is not limiters.for_bot("b")
