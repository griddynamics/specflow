"""The Atlassian reader and the Confluence client (``jira.reader``, ``jira.confluence``) against the recorded-shape fake
of ``atlassian_import_support``: bearer token and one refresh on a ``401``, the closed reasons of a refusal, paging by
``_links.next``, folders left out of descendants, the page cap and the lookups by space key and title."""

from __future__ import annotations

import httpx
import pytest

from atlassian_import_support import CLOUD, FakeAtlassian, FakeTokens, no_sleep, storage_page
from jira.confluence import ConfluenceClient
from jira.gateway import JiraError
from jira.reader import AtlassianReader, confluence_url, jira_url
from jira.reasons import Reason


def client_for(fake: FakeAtlassian, tokens: FakeTokens | None = None) -> tuple[ConfluenceClient, AtlassianReader]:
    reader = AtlassianReader(confluence_url(CLOUD), tokens or FakeTokens(), transport=fake.transport(), sleep=no_sleep)
    return ConfluenceClient(reader), reader


def descendant(page_id: str, kind: str = "page") -> dict[str, str]:
    return {"id": page_id, "status": "current", "title": f"T{page_id}", "type": kind, "parentId": "1", "depth": "1"}


async def test_the_base_urls_go_through_the_atlassian_gateway() -> None:
    assert confluence_url("c1") == "https://api.atlassian.com/ex/confluence/c1"
    assert jira_url("c1") == "https://api.atlassian.com/ex/jira/c1"


async def test_a_page_is_read_with_its_storage_body_and_the_bearer_token() -> None:
    fake = FakeAtlassian(pages={"5": storage_page("5", "Order limits", "<p>x</p>")})
    client, reader = client_for(fake)

    page = await client.page("5")
    await reader.aclose()

    assert page["title"] == "Order limits"
    assert fake.requests[0].url.params["body-format"] == "storage" and fake.auths == ["Bearer t1"]
    assert fake.paths() == ["/wiki/api/v2/pages/5"]


async def test_a_401_refreshes_the_token_once_and_repeats_the_call() -> None:
    fake = FakeAtlassian(pages={"5": storage_page("5", "Order limits", "<p>x</p>")}, expire_t1=True)
    tokens = FakeTokens()
    client, reader = client_for(fake, tokens)

    page = await client.page("5")
    await reader.aclose()

    assert page["id"] == "5" and tokens.refreshes == ["t1"]
    assert fake.auths == ["Bearer t1", "Bearer t2"]


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (404, Reason.NOT_FOUND),
        (403, Reason.FORBIDDEN),
        (400, Reason.INVALID_FIELD),
        (410, Reason.UNEXPECTED),
        (401, Reason.AUTH_FAILED),
    ],
)
async def test_a_refusal_is_a_closed_reason_and_carries_no_text(status: int, reason: Reason) -> None:
    fake = FakeAtlassian(status={"/wiki/api/v2/pages/5": status}, expire_t1=status == 401)
    client, reader = client_for(fake, FakeTokens())
    fake.requests.clear()

    with pytest.raises(JiraError) as caught:
        await client.page("5")
    await reader.aclose()

    assert caught.value.reason is reason and "refused" not in str(caught.value)


async def test_a_5xx_is_retried_and_then_unreachable_and_a_body_that_is_not_an_object_is_unexpected() -> None:
    fake = FakeAtlassian(status={"/wiki/api/v2/pages/5": 503})
    client, reader = client_for(fake)
    with pytest.raises(JiraError) as down:
        await client.page("5")
    assert down.value.reason is Reason.UNREACHABLE and len(fake.requests) == 4  # the first call and three retries

    async def listing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    odd = AtlassianReader(confluence_url(CLOUD), FakeTokens(), transport=httpx.MockTransport(listing), sleep=no_sleep)
    with pytest.raises(JiraError) as unexpected:
        await ConfluenceClient(odd).page("5")
    assert unexpected.value.reason is Reason.UNEXPECTED
    assert CLOUD in repr(reader) and "Bearer" not in repr(reader)
    await reader.aclose()
    await odd.aclose()


async def test_descendants_follow_the_next_link_and_leave_folders_out() -> None:
    items = [
        descendant("11"),
        descendant("12", "folder"),
        descendant("13"),
        descendant("14", "whiteboard"),
        descendant("15"),
    ]
    fake = FakeAtlassian(below={"10": items}, page_size=2)
    client, reader = client_for(fake)

    found, capped = await client.descendants("10", 10)
    await reader.aclose()

    assert [page["id"] for page in found] == ["11", "13", "15"] and not capped
    assert fake.paths() == ["/wiki/api/v2/pages/10/descendants"] * 3
    assert [r.url.params.get("cursor") for r in fake.requests] == [None, "2", "4"]


async def test_the_limit_stops_the_listing_and_says_there_were_more() -> None:
    fake = FakeAtlassian(below={"10": [descendant(str(n)) for n in range(11, 18)]}, page_size=3)
    client, reader = client_for(fake)

    found, capped = await client.descendants("10", 4)
    exact, exact_capped = await client.descendants("10", 7)
    await reader.aclose()

    assert [page["id"] for page in found] == ["11", "12", "13", "14"] and capped
    assert len(exact) == 7 and not exact_capped


async def test_a_space_is_found_by_key_and_its_pages_are_listed() -> None:
    fake = FakeAtlassian(
        spaces={"ORD": {"id": "10", "key": "ORD", "name": "Orders"}},
        space_pages={
            "10": [{"id": "1", "title": "Home"}, {"id": "2", "title": "Limits"}, {"id": "3", "title": "Refunds"}]
        },
        page_size=2,
    )
    client, reader = client_for(fake)

    space = await client.space("ORD")
    pages, capped = await client.space_pages("10", 50)
    with pytest.raises(JiraError) as missing:
        await client.space("NOPE")
    await reader.aclose()

    assert space["name"] == "Orders" and [p["title"] for p in pages] == ["Home", "Limits", "Refunds"] and not capped
    assert missing.value.reason is Reason.NOT_FOUND


async def test_a_page_is_found_by_title_in_a_space() -> None:
    fake = FakeAtlassian(pages={"7": storage_page("7", "Refund policy", "", space_id="10")})
    client, reader = client_for(fake)

    assert await client.find_page("10", "Refund policy") == "7"
    assert await client.find_page("10", "Nothing") is None
    assert await client.find_page("11", "Refund policy") is None
    await reader.aclose()


@pytest.mark.parametrize(
    "next_link",
    [
        "https://evil.example/wiki/api/v2/pages/10/descendants?cursor=2",  # another host
        "http://evil.example/steal",
        "//evil.example/wiki/api/v2/pages/10/descendants",  # a network-path reference: another host too
        "/\\evil.example/x",
        "wiki/api/v2/pages/10/descendants?cursor=2",  # not a path of this site
        "",
        5,
    ],
)
async def test_a_next_link_that_is_not_a_path_of_the_site_ends_the_paging_and_the_token_goes_nowhere(
    next_link: object,
) -> None:
    hosts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        body = {"results": [descendant("11")], "_links": {"next": next_link}}
        return httpx.Response(200, json=body)

    reader = AtlassianReader(confluence_url(CLOUD), FakeTokens(), transport=httpx.MockTransport(handle), sleep=no_sleep)

    found, capped = await ConfluenceClient(reader).descendants("10", 10)
    await reader.aclose()

    assert [page["id"] for page in found] == ["11"] and not capped
    assert hosts == ["api.atlassian.com"]  # one request: nothing was sent to the host the link named


async def test_a_relative_next_link_is_followed() -> None:
    fake = FakeAtlassian(below={"10": [descendant(str(n)) for n in range(11, 14)]}, page_size=2)
    client, reader = client_for(fake)

    found, _ = await client.descendants("10", 10)
    await reader.aclose()

    assert [page["id"] for page in found] == ["11", "12", "13"] and len(fake.requests) == 2
