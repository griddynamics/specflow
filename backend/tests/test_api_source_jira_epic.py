"""``POST /projects/{pid}/sources/jira-epic[/check]`` over the REST app: any member (D6), the acting account is the
member the project's Atlassian is bound to (R1), the closed codes of a key, a link and a refused read, and the import
handed to ``runtime.imports`` with the bound member as the reader and the caller as the one who added it.

Standings as in test_api_jira_routes: alice owns ``Alpha``, ben is its collaborator, carol is no member.

Sequence::

    web -> project_member -> bound Atlassian member -> key or link -> Jira (check: key, summary, issue count)
    add: -> runtime.imports.add_source(connected_by = caller) -> start(job as the bound member) -> 201 source card
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from api.imports.jira_epic import JiraEpicImporter
from atlassian_import_support import (
    ACTING,
    SITE,
    SOURCE_ID,
    FakeAtlassian,
    RecordingWriter,
    as_writer,
    wire_import_rig,
)
from db import source_files
from specflow_api_agent_support import ALPHA, agent_env, log_haystack, serve_agent
from test_api_atlassian_routes import call, error_of
from test_api_import_jira_epic import child, epic_issue

ALICE, BEN, CAROL = "alice@example.com", "ben@example.com", "carol@example.com"
CANARY = "epic-summary-canary-2b8f"


@pytest_asyncio.fixture
async def rig(monkeypatch, valid_env):
    async with serve_agent(monkeypatch, agent_env(valid_env)) as served:
        fake = FakeAtlassian(
            issues={
                "PAY-1": epic_issue("PAY-1", CANARY),
                "PAY-2": child("PAY-2", "Card flow"),
                "PAY-3": child("PAY-3", "Wallet flow"),
            },
        )
        yield await wire_import_rig(served, monkeypatch, fake, alpha=uuid.UUID(ALPHA), kind="jira_epic")


def path(rig: Any, tail: str = "") -> str:
    return f"projects/{rig.alpha}/sources/jira-epic{tail}"


async def test_the_check_answers_the_key_summary_and_issue_count_as_any_member_for_a_key_or_a_link(rig) -> None:
    for who, value in ((ALICE, "PAY-1"), (BEN, f"{SITE}/browse/pay-1"), (ALICE, f"  {SITE}/browse/PAY-1  ")):
        response = await call(rig, "POST", path(rig, "/check"), who, {"key_or_link": value})

        assert response.status_code == 200
        assert response.json() == {"key": "PAY-1", "summary": CANARY, "issues": 2}


async def test_a_non_member_is_a_stranger_to_the_project(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), CAROL, {"key_or_link": "PAY-1"})

    assert error_of(response) == (404, "project_not_found") and rig.fake.requests == []


@pytest.mark.parametrize(
    ("value", "status", "code"),
    [
        ("not a key", 422, "jira_epic_link_invalid"),
        (f"{SITE}/jira/boards/4", 422, "jira_epic_link_invalid"),
        ("https://other.atlassian.net/browse/PAY-1", 422, "atlassian_site_mismatch"),
    ],
)
async def test_a_key_or_link_that_cannot_be_used_is_refused_before_any_read(rig, value, status, code) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"key_or_link": value})

        assert error_of(response) == (status, code)
    assert rig.fake.requests == [] and rig.imports.added == []


async def test_an_issue_that_is_not_an_epic_is_refused(rig) -> None:
    for tail in ("/check", ""):
        response = await call(rig, "POST", path(rig, tail), ALICE, {"key_or_link": "PAY-2"})

        assert error_of(response) == (422, "jira_epic_not_an_epic")
    assert rig.imports.added == []


@pytest.mark.parametrize(
    ("forced", "status", "code"),
    [
        (404, 409, "jira_epic_not_found"),
        (403, 409, "jira_epic_forbidden"),
        (503, 502, "atlassian_unreachable"),
        (400, 502, "atlassian_unreachable"),
    ],
)
async def test_a_refused_read_is_a_closed_code(rig, forced, status, code) -> None:
    rig.fake.status["/rest/api/3/issue/PAY-1"] = forced

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"key_or_link": "PAY-1"})

    assert error_of(response) == (status, code) and CANARY not in response.text


async def test_a_key_that_does_not_exist_is_not_found(rig) -> None:
    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"key_or_link": "PAY-99"})

    assert error_of(response) == (409, "jira_epic_not_found")


async def test_a_refused_token_asks_the_member_to_connect_again(rig) -> None:
    rig.fake.status["/rest/api/3/issue/PAY-1"] = 401
    rig.fake.expire_t1 = True

    response = await call(rig, "POST", path(rig, "/check"), ALICE, {"key_or_link": "PAY-1"})

    assert error_of(response) == (409, "atlassian_reconnect_required")


async def test_without_a_bound_member_or_with_an_expired_connection_or_without_oauth_nothing_is_read(rig) -> None:
    body = {"key_or_link": "PAY-1"}
    rig.connections.status = "expired"
    assert error_of(await call(rig, "POST", path(rig, "/check"), ALICE, body)) == (409, "atlassian_reconnect_required")
    rig.connections.status = "active"
    rig.integrations.release(rig.alpha, ACTING)
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (409, "atlassian_not_connected")
    rig.runtime.jira = SimpleNamespace(connections=None)
    assert error_of(await call(rig, "POST", path(rig), ALICE, body)) == (503, "atlassian_oauth_unavailable")
    assert rig.fake.requests == [] and rig.imports.added == []


async def test_adding_an_epic_creates_the_source_for_the_caller_and_reads_as_the_bound_member(rig) -> None:
    response = await call(rig, "POST", path(rig), BEN, {"key_or_link": "pay-1"})

    assert response.status_code == 201, response.text
    source = response.json()["source"]
    assert (source["id"], source["kind"], source["name"], source["link"]) == (
        str(SOURCE_ID),
        "jira_epic",
        f"PAY-1: {CANARY}",
        f"{SITE}/browse/PAY-1",
    )
    (added,) = rig.imports.added
    assert (added["project_id"], added["user_id"], added["kind"]) == (rig.alpha, rig.ben.id, "jira_epic")
    (job, importer) = rig.imports.started[0]
    assert (job.project_id, job.source_id, job.user_id, job.kind) == (rig.alpha, SOURCE_ID, ACTING, "jira_epic")
    assert isinstance(importer, JiraEpicImporter)
    writer = RecordingWriter()
    await importer(job, as_writer(writer))
    assert list(writer.added) == ["issue:PAY-1", "issue:PAY-2", "issue:PAY-3"]


async def test_a_source_removed_before_it_is_read_back_is_not_found(rig, monkeypatch) -> None:
    async def gone(*_args: Any) -> None:
        return None

    monkeypatch.setattr(source_files, "source_summary", gone)

    response = await call(rig, "POST", path(rig), ALICE, {"key_or_link": "PAY-1"})

    assert error_of(response) == (404, "source_not_found")


async def test_the_request_body_is_closed_and_the_summary_never_reaches_a_log(rig, caplog) -> None:
    assert (await call(rig, "POST", path(rig, "/check"), ALICE, {"key_or_link": "PAY-1", "x": 1})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {})).status_code == 422
    assert (await call(rig, "POST", path(rig), ALICE, {"key_or_link": "x" * 3000})).status_code == 422

    await call(rig, "POST", path(rig), ALICE, {"key_or_link": "PAY-1"})

    assert CANARY not in log_haystack(caplog)
