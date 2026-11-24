"""The Jira epic importer (``api.imports.jira_epic``) over a fake Jira shaped like the REST v3 API
(``atlassian_import_support``) and a writer that records what it stores: the epic and its issues over several search
pages, with comments (also the ones beyond those an issue came with), an issue the member cannot read, the cap, the
refresh after a 401, and the failure reasons of the source.

Sequence::

    importer -> access (the member the project is bound to) -> Jira: the epic (GET), its issues (JQL, paged)
    importer -> writer.declare(epic + issues) -> per issue: comments (more pages when needed) -> writer.add
    an issue whose comments are refused (403/404) -> writer.fail(no_access, read), the import goes on
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest

from api.imports import ImportFailedError, ImportJob
from api.imports import jira_epic as epic_module
from api.imports.jira_epic import JiraEpicImport, JiraEpicImporter, inspect_epic, is_epic
from atlassian_import_support import (
    ACTING,
    CLOUD,
    FakeAtlassian,
    FakeConnections,
    FakeTokens,
    RecordingWriter,
    as_writer,
    install,
    jira_comment,
    jira_issue,
    no_sleep,
)
from jira.gateway import JiraError
from jira.issues import JiraIssueReader
from jira.reader import AtlassianReader, jira_url
from jira.reasons import Reason

CANARY = "summary-canary-6d40"


def job() -> ImportJob:
    return ImportJob(uuid.UUID(int=1), uuid.UUID(int=2), ACTING, "jira_epic")


def epic_issue(key: str = "PAY-1", summary: str = "Checkout") -> dict[str, Any]:
    issue = jira_issue(key, summary, "The epic.")
    issue["fields"]["issuetype"] = {"name": "Epic", "hierarchyLevel": 1}
    return issue


def child(key: str, summary: str, comments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {**jira_issue(key, summary, f"Text of {key}", comments), "parent": "PAY-1"}


def epic_fake() -> FakeAtlassian:
    return FakeAtlassian(
        issues={
            "PAY-1": epic_issue(),
            "PAY-2": child("PAY-2", "Card flow", [jira_comment("Ada", "Looks good")]),
            "PAY-3": child("PAY-3", "Wallet flow"),
            "PAY-4": child("PAY-4", "Refunds"),
        },
        page_size=2,
    )


async def run(source: JiraEpicImport, fake: FakeAtlassian, monkeypatch, connections=None) -> RecordingWriter:
    install(monkeypatch, fake)
    writer = RecordingWriter()
    await JiraEpicImporter(connections or FakeConnections(), source)(job(), as_writer(writer))
    return writer


async def test_the_epic_and_every_issue_under_it_are_stored_with_their_comments(monkeypatch) -> None:
    fake = epic_fake()

    writer = await run(JiraEpicImport("PAY-1"), fake, monkeypatch)

    assert [d[0] for d in writer.declared] == ["issue:PAY-1", "issue:PAY-2", "issue:PAY-3", "issue:PAY-4"]
    assert list(writer.added) == ["issue:PAY-1", "issue:PAY-2", "issue:PAY-3", "issue:PAY-4"] and writer.failed == []
    name, markdown, original = writer.added["issue:PAY-2"]
    assert name == "PAY-2: Card flow"
    assert markdown.startswith("# PAY-2: Card flow\n\nType: Story | Status: In Progress | Labels: checkout\n\n")
    assert "Text of PAY-2" in markdown and "## Comments\n\n**Ada** (2026-03-01):\n\nLooks good" in markdown
    stored = json.loads(original)
    assert stored["issue"]["key"] == "PAY-2" and stored["comments"][0]["author"]["displayName"] == "Ada"
    searches = [r for r in fake.requests if r.url.path.endswith("/search/jql")]
    assert len(searches) == 2  # three issues over pages of two


async def test_the_comments_beyond_the_ones_a_search_result_carries_are_read(monkeypatch) -> None:
    fake = epic_fake()
    everything = [jira_comment("Ada", "one"), jira_comment("Ben", "two"), jira_comment("Cy", "three")]
    fake.issues["PAY-3"]["fields"]["comment"] = {"comments": everything[:1], "total": 3}
    fake.all_comments["PAY-3"] = everything

    writer = await run(JiraEpicImport("PAY-1"), fake, monkeypatch)

    markdown = writer.added["issue:PAY-3"][1]
    assert [name in markdown for name in ("**Ada**", "**Ben**", "**Cy**")] == [True, True, True]


async def test_an_issue_whose_comments_the_member_cannot_read_is_skipped_and_counted(monkeypatch) -> None:
    fake = epic_fake()
    fake.issues["PAY-3"]["fields"]["comment"]["total"] = 2
    fake.status["/rest/api/3/issue/PAY-3/comment"] = 403

    writer = await run(JiraEpicImport("PAY-1"), fake, monkeypatch)

    assert list(writer.added) == ["issue:PAY-1", "issue:PAY-2", "issue:PAY-4"]
    assert writer.failed == [("issue:PAY-3", "no_access", "read")]


async def test_an_epic_without_issues_is_one_document(monkeypatch) -> None:
    fake = FakeAtlassian(issues={"PAY-1": epic_issue()})

    writer = await run(JiraEpicImport("PAY-1"), fake, monkeypatch)

    assert list(writer.added) == ["issue:PAY-1"]


async def test_the_cap_stops_the_listing_and_is_logged_as_a_count(monkeypatch, caplog) -> None:
    monkeypatch.setattr(epic_module, "MAX_EPIC_ISSUES", 2)

    with caplog.at_level(logging.INFO):
        writer = await run(JiraEpicImport("PAY-1"), epic_fake(), monkeypatch)

    assert list(writer.added) == ["issue:PAY-1", "issue:PAY-2", "issue:PAY-3"]
    assert "api.imports.epic_cap_reached" in caplog.text and "Card flow" not in caplog.text


async def test_a_401_refreshes_the_members_token_once_and_the_import_goes_on(monkeypatch) -> None:
    fake = epic_fake()
    fake.expire_t1 = True
    connections = FakeConnections()

    writer = await run(JiraEpicImport("PAY-1"), fake, monkeypatch, connections)

    assert len(writer.added) == 4 and connections.tokens.refreshes == ["t1"]


@pytest.mark.parametrize(
    ("status", "reason"), [(404, "no_access"), (403, "no_access"), (401, "no_access"), (503, "unknown_error")]
)
async def test_an_epic_that_cannot_be_read_fails_the_source_with_a_closed_reason(monkeypatch, status, reason) -> None:
    fake = epic_fake()
    fake.status["/rest/api/3/issue/PAY-1"] = status
    fake.expire_t1 = status == 401

    with pytest.raises(ImportFailedError) as caught:
        await run(JiraEpicImport("PAY-1"), fake, monkeypatch)

    assert caught.value.reason == reason


async def test_a_member_without_an_active_connection_fails_the_source_with_no_access(monkeypatch) -> None:
    fake = epic_fake()
    install(monkeypatch, fake)

    with pytest.raises(ImportFailedError) as caught:
        await JiraEpicImporter(FakeConnections(status="expired"), JiraEpicImport("PAY-1"))(
            job(), as_writer(RecordingWriter())
        )

    assert caught.value.reason == "no_access" and fake.requests == []


async def test_a_run_logs_no_summary_and_no_text(monkeypatch, caplog) -> None:
    fake = epic_fake()
    fake.issues["PAY-2"]["fields"]["summary"] = CANARY

    with caplog.at_level(logging.DEBUG):
        await run(JiraEpicImport("PAY-1"), fake, monkeypatch)

    assert CANARY not in caplog.text and "Text of PAY-2" not in caplog.text and "Looks good" not in caplog.text


def reader(fake: FakeAtlassian) -> tuple[JiraIssueReader, AtlassianReader]:
    made = AtlassianReader(jira_url(CLOUD), FakeTokens(), transport=fake.transport(), sleep=no_sleep)
    return JiraIssueReader(made), made


async def test_the_check_reads_the_epic_and_counts_its_issues() -> None:
    jira, made = reader(epic_fake())

    found = await inspect_epic(jira, "PAY-1")
    await made.aclose()

    assert (found.key, found.summary, found.issues, found.is_epic) == ("PAY-1", "Checkout", 3, True)


async def test_the_check_of_a_story_says_it_is_not_an_epic_without_counting() -> None:
    fake = epic_fake()
    jira, made = reader(fake)

    found = await inspect_epic(jira, "PAY-2")
    await made.aclose()

    assert (found.is_epic, found.issues) == (False, 0) and not any("approximate-count" in p for p in fake.paths())


async def test_the_check_of_a_missing_issue_is_not_found() -> None:
    jira, made = reader(FakeAtlassian())

    with pytest.raises(JiraError) as caught:
        await inspect_epic(jira, "PAY-9")
    await made.aclose()

    assert caught.value.reason is Reason.NOT_FOUND


def test_an_epic_is_an_issue_of_hierarchy_level_one_or_named_epic() -> None:
    assert is_epic({"fields": {"issuetype": {"hierarchyLevel": 1}}})
    assert is_epic({"fields": {"issuetype": {"name": "Epic"}}})
    assert not is_epic({"fields": {"issuetype": {"name": "Story", "hierarchyLevel": 0}}})
    assert not is_epic({"fields": {}})
