"""One scenario on ``FakeLinear`` and on ``HttpLinearGateway`` over a scripted Linear: the same observable results, so
the fake the streams are tested on keeps saying what the real gateway says."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable
from typing import Any

import httpx

from linear.errors import LinearError, LinearInvalidError, LinearUnavailableError
from linear.fake import FakeLinear
from linear.gateway import LinearGateway
from linear.http import HttpLinearGateway
from linear.models import NewIssue, Organization
from linear_http_support import (
    CANARY,
    ISSUE,
    OTHER,
    PROJECT,
    STATE,
    TEAM,
    comment_node,
    connection,
    data,
    failure,
    issue_node,
    org,
    project_node,
)

ORG = Organization(OTHER, "Graphico", "graphico-app")
SECOND = "88888888-8888-4888-8888-888888888888"
WHEN = "2026-01-01T00:00:00.000Z"
_OPERATION = re.compile(r"(?:query|mutation)\s+(\w+)")


def scripted_linear() -> dict[str, list[httpx.Response]]:
    """What Linear answers to ``scenario``, per operation and in call order."""

    plain = {"labels": {"nodes": []}, "project": {"id": PROJECT}}
    first = issue_node("GRA-1", estimate=3, priority=2, **plain)
    first["state"] = {"id": STATE, "name": "Backlog", "type": "backlog", "position": 0}
    second = issue_node("GRA-2", id=SECOND, estimate=None, priority=0, **plain)
    renamed = {**first, "title": "Renamed", "estimate": 5}
    return {
        "Viewer": [data({"viewer": {"id": OTHER, "name": "Fake Member", "organization": org()}})],
        "Teams": [data({"teams": connection([{"id": TEAM, "key": "GRA", "name": "GRA"}])})],
        "ProjectCreate": [data({"projectCreate": {"success": True, "project": project_node(description=None)}})],
        "IssueCreate": [
            data({"issueCreate": {"success": True, "issue": first}}),
            failure("INVALID_INPUT", userPresentableMessage="id must be unique"),
            failure("INVALID_INPUT", validationErrors=[{"property": "estimate"}]),
            data({"issueCreate": {"success": True, "issue": second}}),
        ],
        "IssueUpdate": [data({"issueUpdate": {"success": True, "issue": renamed}})],
        "Issue": [
            data({"issue": renamed}),
            failure("INPUT_ERROR", 400, userPresentableMessage="Could not find referenced Issue."),
        ],
        "CommentCreate": [data({"commentCreate": {"success": True, "comment": comment_node(SECOND, WHEN)}})],
        "Comments": [data({"issue": {"comments": connection([comment_node(SECOND, WHEN)])}})],
        "ProjectIssues": [
            data({"project": {"issues": connection([first], "cursor-1")}}),
            data({"project": {"issues": connection([second])}}),
        ],
    }


async def outcome(call: Awaitable[Any]) -> tuple[str, str | None]:
    """The closed code of a failing call and the field it names."""

    try:
        await call
    except LinearError as error:
        return str(error), error.field if isinstance(error, LinearInvalidError) else None
    return "no error", None


async def scenario(gateway: LinearGateway) -> list[Any]:
    """Every step records plain values: what a stream can observe."""

    seen: list[Any] = []
    viewer = await gateway.viewer()
    seen.append((viewer.name, viewer.organization.url_key))
    seen.append([(team.id, team.key) for team in await gateway.teams()])
    project = await gateway.create_project(PROJECT, TEAM, "Plan")
    seen.append((project.id, project.name, project.team_ids, project.description))
    created = await gateway.create_issue(NewIssue(ISSUE, TEAM, "A title", project_id=PROJECT, estimate=3, priority=2))
    seen.append((created.identifier, created.state.name, created.estimate, created.priority, created.project_id))
    seen.append(await outcome(gateway.create_issue(NewIssue(ISSUE, TEAM, "A title"))))
    seen.append(await outcome(gateway.create_issue(NewIssue(SECOND, TEAM, "Too big", estimate=99))))
    await gateway.create_issue(NewIssue(SECOND, TEAM, "Second", project_id=PROJECT))
    updated = await gateway.update_issue("GRA-1", title="Renamed", estimate=5)
    seen.append((updated.id, updated.title, updated.estimate))
    seen.append((await gateway.issue("GRA-1")).title)
    seen.append(await outcome(gateway.issue("GRA-404")))
    comment = await gateway.create_comment("GRA-1", "hello", comment_id=SECOND)
    seen.append((comment.id, comment.issue_id))
    seen.append([c.id for c in await gateway.comments("GRA-1")])
    first_page = await gateway.issues_of_project(PROJECT)
    last_page = await gateway.issues_of_project(PROJECT, first_page.next_cursor)
    seen.append(([i.identifier for i in first_page.items], first_page.next_cursor is not None))
    seen.append(([i.identifier for i in last_page.items], last_page.next_cursor))
    seen.append(await outcome(gateway.resolve_link("not a link")))
    return seen


def fake_linear() -> FakeLinear:
    fake = FakeLinear(organization=ORG, user_id=OTHER, page_size=1, estimate_allowed=lambda estimate: estimate <= 5)
    fake.add_team("GRA", team_id=TEAM)
    return fake


def http_gateway(transport: httpx.MockTransport) -> HttpLinearGateway:
    async def token() -> str:
        return CANARY

    return HttpLinearGateway(token, client=httpx.AsyncClient(transport=transport))


def scripted_gateway() -> HttpLinearGateway:
    routes = scripted_linear()

    def handler(request: httpx.Request) -> httpx.Response:
        operation = _OPERATION.match(json.loads(request.content)["query"].strip())
        assert operation, "every request is a named operation"
        queue = routes[operation.group(1)]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return http_gateway(httpx.MockTransport(handler))


async def test_the_scenario_has_the_same_observable_results_on_the_fake_and_the_real_gateway():
    fake, real = await scenario(fake_linear()), await scenario(scripted_gateway())
    assert fake == real
    for expected in [("linear_already_exists", None), ("linear_invalid", "estimate"), ("linear_not_found", None)]:
        assert expected in real
    assert ("linear_invalid", "link") in real


async def test_an_unreachable_linear_is_the_same_code_on_both():
    fake = fake_linear()
    fake.fail_next(LinearUnavailableError())
    real = http_gateway(httpx.MockTransport(lambda _: httpx.Response(503)))
    assert await outcome(fake.viewer()) == await outcome(real.viewer()) == ("linear_unreachable", None)
