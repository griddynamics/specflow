"""The GitHub Projects calls (``github.projects``) on a scripted ``api.github.com``: the parsing of answers and the
mapping of every refusal to one closed code. The token never reaches an error."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from github.client import GitHubClient
from github.codes import GitHubCode, GitHubError
from github.projects import REMOVED_FROM_PROJECT, IssueContent, IssueState, ProjectsApi

TOKEN = SecretStr("ghu-token-canary-77")
PROJECT = "PVT_1"
CONTENT = IssueContent("Title", "Body", ["specflow"])


SLEPT: list[float] = []


async def fake_sleep(seconds: float) -> None:
    SLEPT.append(seconds)


def api_over(handler: Callable[[httpx.Request], httpx.Response]) -> tuple[ProjectsApi, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    SLEPT.clear()

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = GitHubClient(
        "id", SecretStr("secret"), "http://localhost/cb", transport=httpx.MockTransport(record), sleep=fake_sleep
    )
    return ProjectsApi(client), seen


def graphql(data: Any = None, errors: list[dict[str, str]] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(200, json={"data": data, "errors": errors} if errors else {"data": data})


def status(code: int, body: dict[str, Any] | None = None, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(code, json=body or {}, headers=headers)


async def code_of(call: Any) -> GitHubCode:
    with pytest.raises(GitHubError) as raised:
        await call
    assert TOKEN.get_secret_value() not in str(raised.value) and TOKEN.get_secret_value() not in repr(raised.value)
    return raised.value.code


async def test_a_repository_of_an_organization_the_user_may_push_to() -> None:
    owner = {"login": "acme", "type": "Organization"}
    api, seen = api_over(status(200, {"owner": owner, "permissions": {"push": True}}))

    found = await api.repository(TOKEN, "acme/shop")

    assert (found.organization, found.can_write) == ("acme", True)
    assert seen[0].url.path == "/repos/acme/shop" and seen[0].headers["authorization"] == "Bearer ghu-token-canary-77"


async def test_a_repository_of_a_user_has_no_organization_and_a_missing_one_is_not_accessible() -> None:
    api, _ = api_over(status(200, {"owner": {"login": "me", "type": "User"}, "permissions": {"pull": True}}))
    found = await api.repository(TOKEN, "me/x")
    assert (found.organization, found.can_write) == (None, False)

    gone, _ = api_over(status(404))
    assert await code_of(gone.repository(TOKEN, "me/x")) is GitHubCode.REPO_NOT_ACCESSIBLE


async def test_the_open_projects_of_an_organization_are_listed_and_closed_ones_left_out() -> None:
    nodes = [
        {"id": "P1", "number": 1, "title": "One", "url": "https://github.com/orgs/acme/projects/1", "closed": False},
        {"id": "P2", "number": 2, "title": "Old", "url": "https://github.com/orgs/acme/projects/2", "closed": True},
        {"id": 5},
    ]
    api, seen = api_over(graphql({"organization": {"id": "O1", "projectsV2": {"nodes": nodes}}}))

    projects = await api.organization_projects(TOKEN, "acme")

    assert [(p.node_id, p.title) for p in projects] == [("P1", "One")]
    assert json.loads(seen[0].content)["variables"] == {"org": "acme"} and seen[0].url.path == "/graphql"


@pytest.mark.parametrize(
    ("errors", "expected"),
    [
        ([{"type": "FORBIDDEN", "message": "Resource not accessible by integration"}], GitHubCode.PERMISSIONS_MISSING),
        ([{"type": "INSUFFICIENT_SCOPES", "message": "x"}], GitHubCode.PERMISSIONS_MISSING),
        ([{"type": "NOT_FOUND", "message": "x"}], GitHubCode.NOT_AN_ORGANIZATION),
        ([{"type": "RATE_LIMITED", "message": "x"}], GitHubCode.UNREACHABLE),
        ([{"type": "SOMETHING", "message": "x"}], GitHubCode.REFUSED),
    ],
)
async def test_a_graphql_error_is_one_closed_code(errors: list[dict[str, str]], expected: GitHubCode) -> None:
    api, _ = api_over(graphql(None, errors))

    assert await code_of(api.organization_projects(TOKEN, "acme")) is expected


async def test_a_project_of_another_kind_or_a_missing_one_is_not_found_and_an_owner_is_read() -> None:
    node = {"id": PROJECT, "number": 3, "title": "T", "url": "https://x/3", "closed": False, "owner": {"login": "acme"}}
    api, _ = api_over(graphql({"node": node}))
    project, owner = await api.project(TOKEN, PROJECT)
    assert (project.number, owner) == (3, "acme")

    missing, _ = api_over(graphql({"node": None}))
    assert await code_of(missing.project(TOKEN, PROJECT)) is GitHubCode.PROJECT_NOT_FOUND


async def test_rest_refusals_are_closed_codes() -> None:
    forbidden, _ = api_over(status(403, {"message": "Resource not accessible by integration"}))
    assert await code_of(forbidden.create_issue(TOKEN, "acme/shop", CONTENT)) is GitHubCode.PERMISSIONS_MISSING
    invalid, _ = api_over(status(422))
    assert await code_of(invalid.create_issue(TOKEN, "acme/shop", CONTENT)) is GitHubCode.ITEM_INVALID
    no_repo, _ = api_over(status(404))
    assert await code_of(no_repo.create_issue(TOKEN, "acme/shop", CONTENT)) is GitHubCode.REPO_NOT_ACCESSIBLE
    gone, _ = api_over(status(410))
    assert await code_of(gone.edit_issue(TOKEN, "acme/shop", 3, CONTENT)) is GitHubCode.ISSUE_GONE
    missing, _ = api_over(status(404))
    assert await code_of(missing.comment(TOKEN, "acme/shop", 3, "x")) is GitHubCode.ISSUE_GONE


async def test_a_rate_limit_or_a_server_error_is_unreachable_and_a_401_asks_for_a_refresh() -> None:
    limited, _ = api_over(status(403, {}, **{"x-ratelimit-remaining": "0"}))
    assert await code_of(limited.create_issue(TOKEN, "acme/shop", CONTENT)) is GitHubCode.UNREACHABLE
    secondary, _ = api_over(status(429))
    assert await code_of(secondary.comment(TOKEN, "acme/shop", 3, "x")) is GitHubCode.UNREACHABLE
    down, _ = api_over(status(502))
    assert await code_of(down.comment(TOKEN, "acme/shop", 3, "x")) is GitHubCode.UNREACHABLE
    expired, _ = api_over(status(401))
    assert await code_of(expired.comment(TOKEN, "acme/shop", 3, "x")) is GitHubCode.AUTH_FAILED


async def test_a_secondary_limit_with_retry_after_is_waited_for_once_then_answers() -> None:
    answers = iter(
        [
            httpx.Response(403, headers={"retry-after": "7"}),
            httpx.Response(201, json={"number": 1, "id": 10, "node_id": "I_1"}),
        ]
    )
    api, seen = api_over(lambda _request: next(answers))

    ref = await api.create_issue(TOKEN, "acme/shop", CONTENT)

    assert ref.number == 1 and SLEPT == [7.0] and len(seen) == 2


async def test_a_second_limit_a_long_wait_and_a_silent_limit_end_as_one_closed_code() -> None:
    twice, seen = api_over(status(429, {}, **{"retry-after": "500"}))
    assert await code_of(twice.comment(TOKEN, "acme/shop", 1, "x")) is GitHubCode.UNREACHABLE
    assert SLEPT == [60.0] and len(seen) == 2  # the wait is capped, and asked for once

    silent, seen = api_over(
        status(403, {"message": "You have exceeded a secondary rate limit"}, **{"retry-after": "abc"})
    )
    assert await code_of(silent.comment(TOKEN, "acme/shop", 1, "x")) is GitHubCode.UNREACHABLE
    assert SLEPT == [] and len(seen) == 1

    plain, _ = api_over(status(403, {"message": "Resource not accessible by integration"}))
    assert await code_of(plain.comment(TOKEN, "acme/shop", 1, "x")) is GitHubCode.PERMISSIONS_MISSING


async def test_a_graphql_call_waits_for_retry_after_the_same_way() -> None:
    answers = iter(
        [
            httpx.Response(403, headers={"retry-after": "3"}),
            httpx.Response(200, json={"data": {"organization": {"id": "O1", "projectsV2": {"nodes": []}}}}),
        ]
    )
    api, _ = api_over(lambda _request: next(answers))

    assert await api.organization_projects(TOKEN, "acme") == [] and SLEPT == [3.0]


async def test_an_issue_is_created_with_its_three_ids_and_the_content_is_sent_as_given() -> None:
    api, seen = api_over(status(201, {"number": 7, "id": 70, "node_id": "I_7"}))

    ref = await api.create_issue(TOKEN, "acme/shop", CONTENT)

    assert (ref.number, ref.issue_id, ref.node_id) == (7, 70, "I_7")
    assert json.loads(seen[0].content) == {"title": "Title", "body": "Body", "labels": ["specflow"]}
    assert seen[0].url.path == "/repos/acme/shop/issues" and seen[0].method == "POST"
    bad, _ = api_over(status(201, {"number": "x"}))
    assert await code_of(bad.create_issue(TOKEN, "acme/shop", CONTENT)) is GitHubCode.REFUSED


async def test_an_edit_sends_title_and_body_only_so_labels_stay() -> None:
    api, seen = api_over(status(200, {}))

    await api.edit_issue(TOKEN, "acme/shop", 4, CONTENT)

    assert seen[0].method == "PATCH" and json.loads(seen[0].content) == {"title": "Title", "body": "Body"}


async def test_a_sub_issue_that_is_already_one_and_a_label_that_is_already_gone_are_fine() -> None:
    again, seen = api_over(status(422, {"message": "already a sub-issue"}))
    await again.add_sub_issue(TOKEN, "acme/shop", 1, 20)
    assert json.loads(seen[0].content) == {"sub_issue_id": 20} and seen[0].url.path.endswith("/issues/1/sub_issues")
    gone, _ = api_over(status(404))
    await gone.remove_label(TOKEN, "acme/shop", 1, "graphico-removed")
    broken, _ = api_over(status(500))
    assert await code_of(broken.add_sub_issue(TOKEN, "acme/shop", 1, 20)) is GitHubCode.UNREACHABLE


async def test_a_422_of_the_sub_issue_call_is_fine_only_when_it_says_the_issue_is_already_one() -> None:
    duplicate, _ = api_over(status(422, {"message": "Validation Failed", "errors": ["duplicate sub-issues"]}))
    await duplicate.add_sub_issue(TOKEN, "acme/shop", 1, 20)
    already, _ = api_over(status(422, {"message": "Issue is already a sub-issue"}))
    await already.add_sub_issue(TOKEN, "acme/shop", 1, 20)
    other, _ = api_over(status(422, {"message": "Validation Failed", "errors": ["cannot nest 9 levels"]}))
    assert await code_of(other.add_sub_issue(TOKEN, "acme/shop", 1, 20)) is GitHubCode.ITEM_INVALID
    for message in ("Issue already has a parent", "Sub-issue has a parent", "already has a parent issue"):
        elsewhere, _ = api_over(status(422, {"message": message}))
        assert await code_of(elsewhere.add_sub_issue(TOKEN, "acme/shop", 1, 20)) is GitHubCode.ITEM_INVALID


async def test_issue_states_read_the_status_of_the_issues_own_item_by_its_id() -> None:
    def item(item_id: str, name: str | None) -> dict[str, Any]:
        return {"id": item_id, "fieldValueByName": None if name is None else {"name": name}}

    issues = [
        {"id": "I_1", "state": "OPEN"},
        {"id": "I_2", "state": "OPEN"},
        {"id": "I_3", "state": "CLOSED"},
        {"id": "I_4", "state": "OPEN"},
        {"id": "I_6", "state": "OPEN"},
        None,
    ]
    items = [item("PVTI_1", "Todo"), item("PVTI_2", "In progress"), item("PVTI_4", None), None]
    api, seen = api_over(graphql({"issues": issues, "items": items}))

    refs = [("I_1", "PVTI_1"), ("I_2", "PVTI_2"), ("I_3", None), ("I_4", "PVTI_4"), ("I_5", None), ("I_6", "PVTI_6")]
    states = await api.issue_states(TOKEN, refs)

    assert json.loads(seen[0].content)["variables"] == {
        "ids": ["I_1", "I_2", "I_3", "I_4", "I_5", "I_6"],
        "items": ["PVTI_1", "PVTI_2", "PVTI_4", "PVTI_6"],
    }
    assert states["I_1"] == IssueState(open=True, status="Todo") and states["I_1"].not_started
    assert not states["I_2"].not_started and not states["I_3"].not_started
    assert states["I_4"].not_started and "I_5" not in states  # an item with no Status is not started
    assert states["I_6"].status == REMOVED_FROM_PROJECT and not states["I_6"].not_started  # taken off the project


async def test_a_project_is_created_in_the_organization_and_an_item_is_added_and_moved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if "createProjectV2" in query:
            node = {"id": "P9", "number": 9, "title": "Shop", "url": "https://x/9", "closed": False}
            return httpx.Response(200, json={"data": {"createProjectV2": {"projectV2": node}}})
        if "addProjectV2ItemById" in query:
            return httpx.Response(200, json={"data": {"addProjectV2ItemById": {"item": {"id": "ITEM"}}}})
        if "updateProjectV2ItemPosition" in query:
            return httpx.Response(200, json={"data": {"updateProjectV2ItemPosition": {"clientMutationId": None}}})
        return httpx.Response(200, json={"data": {"organization": {"id": "O1"}}})

    api, seen = api_over(handler)

    created = await api.create_project(TOKEN, "acme", "Shop")
    item = await api.add_item(TOKEN, "P9", "I_1")
    await api.move_item(TOKEN, "P9", item, None)

    assert (created.node_id, item) == ("P9", "ITEM")
    assert json.loads(seen[-1].content)["variables"] == {"project": "P9", "item": "ITEM", "after": None}
