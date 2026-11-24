"""``jira.issues.JiraIssueReader`` and ``jira.epic_links`` against the recorded-shape fake of
``atlassian_import_support``: one issue, the issues of an epic by JQL over several result pages (and the limit), the
count, the comments beyond the ones an issue came with, and the keys and links an epic may be named by."""

from __future__ import annotations

import json

import pytest

from atlassian_import_support import CLOUD, FakeAtlassian, FakeTokens, jira_comment, jira_issue, no_sleep
from jira.epic_links import EpicLinkError, parse_epic
from jira.gateway import JiraError
from jira.issues import JiraIssueReader
from jira.reader import AtlassianReader, jira_url
from jira.reasons import Reason

HOST = "example.atlassian.net"


def child(key: str, parent: str = "PAY-1") -> dict:
    return {**jira_issue(key, f"Summary {key}", "text"), "parent": parent}


def reader_for(fake: FakeAtlassian) -> tuple[JiraIssueReader, AtlassianReader]:
    reader = AtlassianReader(jira_url(CLOUD), FakeTokens(), transport=fake.transport(), sleep=no_sleep)
    return JiraIssueReader(reader), reader


async def test_an_issue_is_read_with_the_fields_the_document_needs() -> None:
    fake = FakeAtlassian(issues={"PAY-7": jira_issue("PAY-7", "Pay", "x")})
    jira, reader = reader_for(fake)

    issue = await jira.issue("PAY-7")
    with pytest.raises(JiraError) as missing:
        await jira.issue("PAY-404")
    await reader.aclose()

    assert issue["key"] == "PAY-7" and missing.value.reason is Reason.NOT_FOUND
    assert fake.requests[0].url.params["fields"] == "summary,description,status,issuetype,labels,comment"


async def test_the_issues_of_an_epic_come_by_jql_over_every_result_page() -> None:
    fake = FakeAtlassian(issues={f"PAY-{n}": child(f"PAY-{n}") for n in range(2, 7)}, page_size=2)
    jira, reader = reader_for(fake)

    issues, capped = await jira.epic_issues("PAY-1", 10)
    count = await jira.count_epic_issues("PAY-1")
    await reader.aclose()

    assert [issue["key"] for issue in issues] == [f"PAY-{n}" for n in range(2, 7)] and not capped and count == 5
    searches = [json.loads(r.content) for r in fake.requests if r.url.path.endswith("/search/jql")]
    assert [body.get("nextPageToken") for body in searches] == [None, "2", "4"]
    assert searches[0]["jql"] == 'parent = "PAY-1" ORDER BY key' and "comment" in searches[0]["fields"]


async def test_the_limit_stops_the_listing_and_says_there_were_more() -> None:
    fake = FakeAtlassian(issues={f"PAY-{n}": child(f"PAY-{n}") for n in range(2, 7)}, page_size=2)
    jira, reader = reader_for(fake)

    issues, capped = await jira.epic_issues("PAY-1", 3)
    exact, exact_capped = await jira.epic_issues("PAY-1", 5)
    await reader.aclose()

    assert [issue["key"] for issue in issues] == ["PAY-2", "PAY-3", "PAY-4"] and capped
    assert len(exact) == 5 and not exact_capped


async def test_an_epic_without_issues_and_a_key_that_needs_quoting_are_safe() -> None:
    fake = FakeAtlassian()
    jira, reader = reader_for(fake)

    assert await jira.epic_issues('X" OR project is not EMPTY', 5) == ([], False)
    await reader.aclose()

    assert json.loads(fake.requests[0].content)["jql"] == 'parent = "X\\" OR project is not EMPTY" ORDER BY key'


async def test_the_comments_beyond_the_ones_an_issue_came_with_are_read_page_by_page() -> None:
    everything = [jira_comment("Ada", "first"), jira_comment("Ben", "second"), jira_comment("Cy", "third")]
    issue = jira_issue("PAY-7", "Pay", "x", everything[:1])
    issue["fields"]["comment"]["total"] = 3
    fake = FakeAtlassian(issues={"PAY-7": issue}, all_comments={"PAY-7": everything})
    jira, reader = reader_for(fake)

    comments = await jira.comments(issue)
    await reader.aclose()

    assert [c["author"]["displayName"] for c in comments] == [
        "Ada",
        "Ben",
        "Cy",
    ]  # the fake serves one comment per call
    assert [r.url.params["startAt"] for r in fake.requests] == ["1", "2"]
    assert fake.paths() == ["/rest/api/3/issue/PAY-7/comment"] * 2


async def test_comments_without_a_total_or_with_an_empty_page_end_the_read() -> None:
    fake = FakeAtlassian()
    jira, reader = reader_for(fake)

    assert await jira.comments({"key": "PAY-7", "fields": {}}) == []
    stalled = {"key": "PAY-7", "fields": {"comment": {"comments": [], "total": 4}}}
    assert await jira.comments(stalled) == []
    await reader.aclose()


@pytest.mark.parametrize(
    ("value", "key"),
    [
        ("PAY-12", "PAY-12"),
        ("  pay-12 ", "PAY-12"),
        (f"https://{HOST}/browse/PAY-12", "PAY-12"),
        (f"https://{HOST}/browse/PAY-12/", "PAY-12"),
        (f"https://{HOST.upper()}/browse/pay-12?focusedId=3", "PAY-12"),
        (f"https://{HOST}/jira/software/projects/PAY/boards/4/backlog?selectedIssue=PAY-12", "PAY-12"),
    ],
)
def test_an_epic_is_named_by_its_key_or_a_link_of_the_site(value: str, key: str) -> None:
    assert parse_epic(value, HOST) == key


@pytest.mark.parametrize(
    ("value", "foreign"),
    [
        ("https://other.atlassian.net/browse/PAY-12", "foreign"),
        ("", "malformed"),
        ("PAY", "malformed"),
        ("12", "malformed"),
        ("not a key", "malformed"),
        (f"http://{HOST}/browse/PAY-12", "malformed"),
        (f"https://{HOST}/browse/notakey", "malformed"),
        (f"https://{HOST}/jira/boards/4", "malformed"),
        (f"https://{HOST}/jira/boards/4?selectedIssue=PAY-1&selectedIssue=PAY-2", "malformed"),
    ],
)
def test_anything_else_is_refused_and_a_foreign_site_is_told_apart(value: str, foreign: str) -> None:
    with pytest.raises(EpicLinkError) as caught:
        parse_epic(value, HOST)

    assert caught.value.site_mismatch is (foreign == "foreign")
