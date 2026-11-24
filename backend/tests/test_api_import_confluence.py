"""The Confluence importer (``api.imports.confluence``) over a fake Confluence and Jira shaped like the real APIs
(``atlassian_import_support``) and a writer that records what it stores: a page, a page tree, a space, "follow links"
(one hop, same site, linked Jira issues), the page cap, the refresh after a 401, a page the member cannot see, and the
failure reasons of the source.

Sequence of the follow scenario::

    importer -> access (the member the project is bound to) -> Confluence: descendants of the root
    importer -> page 5 (writer.add) -> its links: page 42, "Refund policy" (title), PAY-7, PAY-8
    importer -> page 42, page of the title (writer.add; their own links are not followed)
    importer -> Jira: PAY-7 (+comments), PAY-8 (403: skipped)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest

from api.imports import ImportFailedError, ImportJob
from api.imports import confluence as confluence_module
from api.imports.confluence import ConfluenceImport, ConfluenceImporter, inspect
from api.schemas import ConfluenceLinkIn
from atlassian_import_support import (
    ACTING,
    CLOUD,
    SITE_HOST,
    FakeAtlassian,
    FakeConnections,
    FakeTokens,
    RecordingWriter,
    as_writer,
    install,
    jira_comment,
    jira_issue,
    no_sleep,
    storage_page,
)
from jira.confluence import ConfluenceClient
from jira.confluence_links import PageTarget, SpaceTarget
from jira.reader import AtlassianReader, confluence_url

PROJECT, SOURCE = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
BASE = f"https://{SITE_HOST}/wiki"
CANARY_TITLE = "Title-canary-8d21"
CANARY_TEXT = "text-canary-5b7e"


def job() -> ImportJob:
    return ImportJob(uuid.UUID(PROJECT), uuid.UUID(SOURCE), ACTING, "confluence")


def child(page_id: str, kind: str = "page") -> dict[str, str]:
    return {
        "id": page_id,
        "status": "current",
        "title": f"Child {page_id}",
        "type": kind,
        "parentId": "5",
        "depth": "1",
    }


async def run(source: ConfluenceImport, fake: FakeAtlassian, monkeypatch, connections=None) -> RecordingWriter:
    install(monkeypatch, fake)
    writer = RecordingWriter()
    await ConfluenceImporter(connections or FakeConnections(), source)(job(), as_writer(writer))
    return writer


def tree_fake() -> FakeAtlassian:
    return FakeAtlassian(
        pages={
            "5": storage_page("5", CANARY_TITLE, f"<h1>Root</h1><p>{CANARY_TEXT}</p>"),
            "6": storage_page("6", "Child 6", "<p>six</p>"),
            "7": storage_page("7", "Child 7", "<p>seven</p>"),
        },
        below={"5": [child("6"), child("8", "folder"), child("7")]},
    )


async def test_a_page_is_stored_with_its_title_as_the_heading_and_its_original_as_json(monkeypatch) -> None:
    fake = FakeAtlassian(pages={"5": storage_page("5", "Order limits", "<p>Above <b>500</b>.</p>")})

    writer = await run(ConfluenceImport(PageTarget("5"), follow=False), fake, monkeypatch)

    name, markdown, original = writer.added["page:5"]
    assert (name, markdown) == ("Order limits", "# Order limits\n\nAbove **500**.\n")
    assert json.loads(original)["body"]["storage"]["value"] == "<p>Above <b>500</b>.</p>"
    assert writer.declared == [] and writer.failed == []


async def test_a_page_tree_declares_the_descendants_by_title_and_stores_every_page_but_folders(monkeypatch) -> None:
    writer = await run(ConfluenceImport(PageTarget("5"), follow=False), tree_fake(), monkeypatch)

    assert writer.declared == [("page:6", "Child 6"), ("page:7", "Child 7")]
    assert list(writer.added) == ["page:5", "page:6", "page:7"]


async def test_a_space_stores_its_pages_over_several_result_pages(monkeypatch) -> None:
    fake = FakeAtlassian(
        spaces={"ORD": {"id": "10", "key": "ORD", "name": "Orders"}},
        space_pages={
            "10": [{"id": "1", "title": "Home"}, {"id": "2", "title": "Limits"}, {"id": "3", "title": "Refunds"}]
        },
        pages={n: storage_page(n, t, f"<p>{t}</p>") for n, t in (("1", "Home"), ("2", "Limits"), ("3", "Refunds"))},
        page_size=2,
    )

    writer = await run(ConfluenceImport(SpaceTarget("ORD"), follow=False), fake, monkeypatch)

    assert [name for name, _md, _original in writer.added.values()] == ["Home", "Limits", "Refunds"]
    assert [d[1] for d in writer.declared] == ["Home", "Limits", "Refunds"]


def following_fake() -> FakeAtlassian:
    body = (
        f'<p><a href="{BASE}/spaces/ORD/pages/42/Refunds">Refunds</a> <a href="https://elsewhere.example/wiki/spaces/A/pages/9/B">x</a>'
        '<ac:link><ri:page ri:content-title="Refund policy" ri:space-key="ORD" /></ac:link>'
        '<ac:link><ri:page ri:content-title="Nowhere" ri:space-key="ORD" /></ac:link>'
        '<ac:link><ri:page ri:content-title="Gone" ri:space-key="GONE" /></ac:link>'
        '<ac:link><ri:page ri:content-title="Same space" /></ac:link>'
        f'<a href="https://{SITE_HOST}/browse/PAY-7">PAY-7</a> '
        '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PAY-8</ac:parameter></ac:structured-macro></p>'
    )
    return FakeAtlassian(
        pages={
            "5": storage_page("5", "Root", body),
            "42": storage_page(
                "42", "Refunds", f'<p>two hops away <a href="{BASE}/spaces/ORD/pages/43/Deep">deep</a></p>'
            ),
            "43": storage_page("43", "Deep", "<p>never read</p>"),
            "44": storage_page("44", "Refund policy", "<p>policy</p>"),
            "45": storage_page("45", "Same space", "<p>same</p>"),
        },
        spaces={"ORD": {"id": "10", "key": "ORD", "name": "Orders"}},
        issues={
            "PAY-7": jira_issue("PAY-7", "Pay by card", "Card flow", [jira_comment("Ada", "Looks good")]),
            "PAY-8": jira_issue("PAY-8", "Hidden", "x"),
        },
        status={"/rest/api/3/issue/PAY-8": 403},
    )


async def test_follow_links_reads_same_site_pages_by_url_and_title_one_hop_and_the_linked_issues(monkeypatch) -> None:
    fake = following_fake()

    writer = await run(ConfluenceImport(PageTarget("5"), follow=True), fake, monkeypatch)

    assert list(writer.added) == ["page:5", "page:42", "page:44", "page:45", "issue:PAY-7"]
    assert "page:43" not in writer.added  # one hop: the links of a followed page are not followed
    assert writer.failed == [("issue:PAY-8", "no_access", "read")]  # not visible to the member: skipped and counted
    name, markdown, original = writer.added["issue:PAY-7"]
    assert name == "PAY-7: Pay by card"
    assert "# PAY-7: Pay by card" in markdown and "Card flow" in markdown and "**Ada** (2026-03-01):" in markdown
    assert json.loads(original)["comments"][0]["body"]["type"] == "doc"
    assert not any("elsewhere.example" in request.url.host for request in fake.requests)


async def test_without_follow_the_links_of_a_page_are_not_read(monkeypatch) -> None:
    fake = following_fake()

    writer = await run(ConfluenceImport(PageTarget("5"), follow=False), fake, monkeypatch)

    assert list(writer.added) == ["page:5"]
    assert fake.paths() == ["/wiki/api/v2/pages/5/descendants", "/wiki/api/v2/pages/5"]


async def test_a_page_read_twice_through_links_is_stored_once(monkeypatch) -> None:
    fake = following_fake()
    fake.pages["5"] = storage_page(
        "5", "Root", f'<a href="{BASE}/spaces/ORD/pages/5/Root">self</a><a href="{BASE}/spaces/ORD/pages/42/x">a</a>'
    )
    fake.below["5"] = [child("42")]
    fake.pages["42"] = storage_page("42", "Refunds", "<p>no links</p>")

    writer = await run(ConfluenceImport(PageTarget("5"), follow=True), fake, monkeypatch)

    assert list(writer.added) == ["page:5", "page:42"]
    assert fake.paths().count("/wiki/api/v2/pages/42") == 1


async def test_the_page_cap_stops_the_tree_and_is_logged_as_a_count(monkeypatch, caplog) -> None:
    monkeypatch.setattr(confluence_module, "MAX_PAGES", 3)
    fake = tree_fake()
    fake.below["5"] = [child(str(n)) for n in range(6, 12)]
    fake.pages.update({str(n): storage_page(str(n), f"Child {n}", "<p>x</p>") for n in range(6, 12)})

    with caplog.at_level(logging.INFO):
        writer = await run(ConfluenceImport(PageTarget("5"), follow=False), fake, monkeypatch)

    assert list(writer.added) == ["page:5", "page:6", "page:7"]
    assert "api.imports.page_cap_reached" in caplog.text
    assert CANARY_TITLE not in caplog.text and CANARY_TEXT not in caplog.text and "Child 6" not in caplog.text


async def test_the_page_cap_also_bounds_the_followed_pages_and_the_issue_cap_the_linked_issues(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(confluence_module, "MAX_PAGES", 2)
    monkeypatch.setattr(confluence_module, "MAX_LINKED_ISSUES", 1)
    fake = following_fake()
    fake.pages["5"] = storage_page(
        "5",
        "Root",
        f'<a href="{BASE}/spaces/ORD/pages/42/a">a</a><a href="{BASE}/spaces/ORD/pages/44/b">b</a>'
        '<ac:link><ri:page ri:content-title="Same space" /></ac:link>'
        f'<a href="https://{SITE_HOST}/browse/PAY-7">p</a><a href="https://{SITE_HOST}/browse/PAY-8">q</a>',
    )

    with caplog.at_level(logging.INFO):
        writer = await run(ConfluenceImport(PageTarget("5"), follow=True), fake, monkeypatch)

    assert list(writer.added) == ["page:5", "page:42", "issue:PAY-7"] and writer.failed == []
    assert "api.imports.page_cap_reached" in caplog.text and "api.imports.issue_cap_reached" in caplog.text


async def test_a_401_refreshes_the_members_token_once_and_the_import_goes_on(monkeypatch) -> None:
    fake = tree_fake()
    fake.expire_t1 = True
    connections = FakeConnections()

    writer = await run(ConfluenceImport(PageTarget("5"), follow=False), fake, monkeypatch, connections)

    assert list(writer.added) == ["page:5", "page:6", "page:7"]
    assert connections.tokens.refreshes == ["t1"]


async def test_a_page_the_member_cannot_see_is_skipped_and_counted_and_the_others_are_stored(monkeypatch) -> None:
    fake = tree_fake()
    fake.status["/wiki/api/v2/pages/6"] = 403
    fake.status["/wiki/api/v2/pages/7"] = 404

    writer = await run(ConfluenceImport(PageTarget("5"), follow=False), fake, monkeypatch)

    assert list(writer.added) == ["page:5"]
    assert writer.failed == [("page:6", "no_access", "read"), ("page:7", "no_access", "read")]


@pytest.mark.parametrize(
    ("status", "reason"),
    [(403, "no_access"), (404, "no_access"), (401, "no_access"), (503, "unknown_error"), (400, "unknown_error")],
)
async def test_a_refused_target_fails_the_source_with_a_closed_reason(monkeypatch, status, reason) -> None:
    fake = tree_fake()
    fake.status["/wiki/api/v2/pages/5/descendants"] = status
    fake.expire_t1 = status == 401

    with pytest.raises(ImportFailedError) as caught:
        await run(ConfluenceImport(PageTarget("5"), follow=False), fake, monkeypatch)

    assert caught.value.reason == reason


async def test_a_space_that_does_not_exist_fails_the_source_with_no_access(monkeypatch) -> None:
    with pytest.raises(ImportFailedError) as caught:
        await run(ConfluenceImport(SpaceTarget("NOPE"), follow=False), FakeAtlassian(), monkeypatch)

    assert caught.value.reason == "no_access"


@pytest.mark.parametrize("status", ["expired", "gone"])
async def test_a_member_without_an_active_connection_fails_the_source_with_no_access(monkeypatch, status) -> None:
    fake = tree_fake()
    connections = FakeConnections(status="expired")
    importer_job = job()
    if status == "gone":
        importer_job = ImportJob(importer_job.project_id, importer_job.source_id, importer_job.project_id, "confluence")
    install(monkeypatch, fake)

    with pytest.raises(ImportFailedError) as caught:
        await ConfluenceImporter(connections, ConfluenceImport(PageTarget("5"), follow=False))(
            importer_job, as_writer(RecordingWriter())
        )

    assert caught.value.reason == "no_access" and fake.requests == []


async def test_a_run_logs_no_title_and_no_text(monkeypatch, caplog) -> None:
    fake = following_fake()
    fake.pages["5"]["title"] = CANARY_TITLE

    with caplog.at_level(logging.DEBUG):
        await run(ConfluenceImport(PageTarget("5"), follow=True), fake, monkeypatch)

    assert CANARY_TITLE not in caplog.text and "Pay by card" not in caplog.text and "Card flow" not in caplog.text


async def client(fake: FakeAtlassian) -> tuple[ConfluenceClient, AtlassianReader]:
    reader = AtlassianReader(confluence_url(CLOUD), FakeTokens(), transport=fake.transport(), sleep=no_sleep)
    return ConfluenceClient(reader), reader


async def test_the_check_of_a_page_counts_its_tree_and_the_issues_its_first_page_links_to() -> None:
    fake = following_fake()
    fake.below["5"] = [child("42"), child("44")]
    confluence, reader = await client(fake)

    found = await inspect(confluence, ConfluenceImport(PageTarget("5"), follow=True), SITE_HOST)
    plain = await inspect(confluence, ConfluenceImport(PageTarget("5"), follow=False), SITE_HOST)
    await reader.aclose()

    assert (found.kind, found.title, found.pages, found.capped, found.linked_issues) == ("page", "Root", 3, False, 2)
    assert plain.linked_issues == 0


async def test_the_check_of_a_space_names_the_space_and_counts_its_pages_up_to_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(confluence_module, "MAX_PAGES", 2)
    fake = FakeAtlassian(
        spaces={"ORD": {"id": "10", "key": "ORD", "name": "Orders"}},
        space_pages={"10": [{"id": str(n), "title": "t"} for n in range(1, 6)]},
    )
    confluence, reader = await client(fake)

    found = await inspect(confluence, ConfluenceImport(SpaceTarget("ORD"), follow=True), SITE_HOST)
    await reader.aclose()

    assert (found.kind, found.title, found.pages, found.capped, found.linked_issues) == ("space", "Orders", 2, True, 0)


def test_the_follow_defaults_are_off_in_the_request_model() -> None:
    assert ConfluenceLinkIn(link="https://x").follow is False
    payload: dict[str, Any] = ConfluenceLinkIn(link="https://x", follow=True).model_dump()
    assert payload == {"link": "https://x", "follow": True}
