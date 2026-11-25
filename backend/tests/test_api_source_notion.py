"""``POST /projects/{pid}/sources/notion[/check]`` over the REST app: any member (D6), the acting account is the member
the project's Notion is bound to (R1), the closed ``notion_*`` codes of a link, a missing or expired binding and a
refused read, and the import handed to ``runtime.imports`` with the bound member as the reader and the caller as the
one who added it. The converter of stream B is replaced by the stand-in of ``test_api_import_notion``.

Standings as in test_api_confluence routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence::

    web -> project_member -> bound Notion member -> link -> Notion (check: kind, title, pages)
    add: -> runtime.imports.add_source(connected_by = caller) -> start(job as the bound member) -> 201 source card
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.imports.notion import NotionImporter
from db import source_files
from notion import convert
from notion.fake import PRD_PAGE_ID, REQUIREMENTS_DATABASE_ID, ROOT_PAGE_ID, WIKI_PAGE_ID
from notion.reasons import NotionCode
from notion_support import ACTING, SOURCE_ID, RecordingWriter, as_writer, wire_notion_rig
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import call, error_of
from test_api_import_notion import render_row, scrub_original, to_markdown

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"


def link_of(object_id: str, view: str | None = None) -> str:
    return f"https://www.notion.so/Some-Title-{object_id.replace('-', '')}" + (f"?v={view}" if view else "")


WIKI_LINK, PRD_LINK = link_of(WIKI_PAGE_ID), link_of(PRD_PAGE_ID)


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    monkeypatch.setattr(convert, "to_markdown", to_markdown)
    monkeypatch.setattr(convert, "render_row", render_row)
    monkeypatch.setattr(convert, "scrub_original", scrub_original)
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        made = await wire_notion_rig(served, monkeypatch, alpha=uuid.UUID(ALPHA))
        ben = await served.repo.upsert_user(
            None, SimpleNamespace(subject="user:ben@example.com", email="ben@example.com", name=None)
        )
        await served.repo.upsert_user(
            None, SimpleNamespace(subject="user:carol@example.com", email="carol@example.com", name=None)
        )
        served.repo.members.add((made.alpha, ben.id))
        served.repo.roles[(made.alpha, ben.id)] = "collaborator"
        made.ben = ben
        yield made


def path(rig: Any, tail: str = "") -> str:
    return f"projects/{rig.alpha}/sources/notion{tail}"


async def test_the_check_of_a_page_answers_its_title_and_the_pages_of_its_tree_as_any_member(rig) -> None:
    for who in (ALICE, BEN):
        response = await call(rig, "POST", path(rig, "/check"), who, {"link": WIKI_LINK})

        assert response.status_code == 200
        assert response.json() == {"kind": "page", "title": "Engineering Wiki", "pages": 3, "capped": False}
    assert set(rig.connections.gateways_asked) == {ACTING}


async def test_the_check_of_a_database_link_answers_the_database_with_its_rows(rig) -> None:
    for link in (link_of(REQUIREMENTS_DATABASE_ID, "v1"), link_of(REQUIREMENTS_DATABASE_ID)):
        response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": link})

        assert response.json() == {"kind": "database", "title": "Requirements DB", "pages": 3, "capped": False}


async def test_the_check_is_a_probe_and_the_add_only_opens_the_link(rig) -> None:
    await call(rig, "POST", path(rig, "/check"), ALICE, {"link": WIKI_LINK})
    checked = [c.method for c in rig.fake.calls]
    rig.fake.calls.clear()
    await call(rig, "POST", path(rig), ALICE, {"link": WIKI_LINK})
    added = [c.method for c in rig.fake.calls]
    rig.fake.calls.clear()
    await call(rig, "POST", path(rig), ALICE, {"link": link_of(REQUIREMENTS_DATABASE_ID, "v1")})

    assert checked == ["page", "page_markdown"] and added == ["page"]
    assert [c.method for c in rig.fake.calls] == ["page", "database"]


async def test_a_non_member_is_a_stranger_to_the_project(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), CAROL, {"link": PRD_LINK})

    assert error_of(response) == (404, "project_not_found") and rig.fake.calls == []


@pytest.mark.parametrize(
    "link",
    [
        "nonsense",
        "https://example.com/Title-423ed336064581f3b5eaff40471c93e5",
        "http://www.notion.so/Title-423ed336064581f3b5eaff40471c93e5",
        "https://www.notion.so/no-id-here",
        "https://www.notion.so/",
    ],
)
async def test_a_link_that_is_not_a_notion_page_or_database_is_refused_before_any_read(rig, link) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"link": link})

        assert error_of(response) == (422, "notion_link_invalid")
    assert rig.fake.calls == [] and rig.imports.added == []


async def test_a_page_or_database_nobody_shared_is_not_shared(rig) -> None:
    rig.fake.unshare(PRD_PAGE_ID, REQUIREMENTS_DATABASE_ID)
    unknown = link_of("fa4e1000-0000-4000-8000-000000000404")

    for link in (PRD_LINK, link_of(REQUIREMENTS_DATABASE_ID, "v1"), unknown):
        for tail in ("/check", ""):
            assert error_of(await call(rig, "POST", path(rig, tail), ALICE, {"link": link})) == (
                409,
                "notion_not_shared",
            )
    assert rig.imports.added == []


@pytest.mark.parametrize(
    ("code", "status", "api_code"),
    [
        (NotionCode.UNREACHABLE, 502, "notion_unreachable"),
        (NotionCode.RATE_LIMITED, 503, "notion_rate_limited"),
        (NotionCode.AUTH_FAILED, 502, "notion_auth_failed"),
        (NotionCode.RECONNECT_REQUIRED, 409, "notion_reconnect_required"),
        (NotionCode.PERMISSIONS_MISSING, 409, "notion_permissions_missing"),
    ],
)
async def test_a_refused_read_is_a_closed_code(rig, code, status, api_code) -> None:
    rig.fake.fail("page", code)

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": PRD_LINK})

    assert error_of(response) == (status, api_code)


async def test_a_failure_in_the_middle_of_the_check_is_a_closed_code(rig) -> None:
    rig.fake.fail("page_markdown", NotionCode.UNREACHABLE)

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"link": WIKI_LINK})

    assert error_of(response) == (502, "notion_unreachable")


async def test_without_a_binding_an_expired_connection_or_the_integration_nothing_is_read(rig) -> None:
    body = {"link": PRD_LINK}
    rig.connections.expire(ACTING)
    for tail in ("/check", ""):
        assert error_of(await call(rig, "POST", path(rig, tail), ALICE, body)) == (409, "notion_reconnect_required")
    rig.connections.users[ACTING] = "active"
    rig.integrations.release(rig.alpha, ACTING)
    for tail in ("/check", ""):
        assert error_of(await call(rig, "POST", path(rig, tail), ALICE, body)) == (409, "notion_not_connected")
    rig.runtime.notion = None
    for tail in ("/check", ""):
        assert error_of(await call(rig, "POST", path(rig, tail), ALICE, body)) == (503, "notion_unavailable")
    assert rig.fake.calls == [] and rig.imports.added == []


async def test_adding_a_link_creates_the_source_for_the_caller_and_reads_as_the_bound_member(rig) -> None:
    response = await call(rig, "POST", path(rig), BEN, {"link": f"  {WIKI_LINK}  "})

    assert response.status_code == 201
    source = response.json()["source"]
    assert (source["id"], source["kind"], source["name"], source["link"]) == (
        str(SOURCE_ID),
        "notion",
        "Engineering Wiki",
        WIKI_LINK,
    )
    (added,) = rig.imports.added
    assert (added["project_id"], added["user_id"], added["kind"], added["name"]) == (
        rig.alpha,
        rig.ben.id,
        "notion",
        "Engineering Wiki",
    )
    (job, importer) = rig.imports.started[0]
    assert (job.project_id, job.source_id, job.user_id, job.kind) == (rig.alpha, SOURCE_ID, ACTING, "notion")
    assert isinstance(importer, NotionImporter)
    writer = RecordingWriter()
    await importer(job, as_writer(writer))
    assert len(writer.added) == 4 and writer.failed == []


async def test_adding_a_database_names_the_source_by_the_database_title(rig) -> None:
    response = await call(rig, "POST", path(rig), ALICE, {"link": link_of(REQUIREMENTS_DATABASE_ID, "v1")})

    assert response.status_code == 201 and rig.imports.added[0]["name"] == "Requirements DB"


async def test_adding_the_same_link_twice_makes_two_sources_and_the_writer_links_duplicate_content(rig) -> None:
    for _ in range(2):
        assert (await call(rig, "POST", path(rig), ALICE, {"link": PRD_LINK})).status_code == 201

    assert [a["link"] for a in rig.imports.added] == [PRD_LINK, PRD_LINK] and len(rig.imports.started) == 2


async def test_a_source_removed_before_it_is_read_back_is_not_found(rig, monkeypatch) -> None:
    async def gone(*_args: Any) -> None:
        return None

    monkeypatch.setattr(source_files, "source_summary", gone)

    response = await call(rig, "POST", path(rig), ALICE, {"link": PRD_LINK})

    assert error_of(response) == (404, "source_not_found")


async def test_the_request_body_is_closed_and_no_title_or_link_reaches_a_log(rig, caplog) -> None:
    assert (await call(rig, "POST", path(rig, "/check"), ALICE, {"link": PRD_LINK, "extra": 1})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {})).status_code == 422

    await call(rig, "POST", path(rig), ALICE, {"link": PRD_LINK})

    haystack = log_haystack(caplog)
    assert "PRD - Checkout Loyalty Points" not in haystack and PRD_PAGE_ID.replace("-", "") not in haystack
    assert ROOT_PAGE_ID not in haystack
