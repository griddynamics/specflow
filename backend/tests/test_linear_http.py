"""``HttpLinearGateway`` on ``httpx.MockTransport`` with recorded-style Linear answers: every gateway method, paging,
the error mapping and the leaks."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from linear.errors import (
    LinearAlreadyExistsError,
    LinearError,
    LinearForbiddenError,
    LinearInvalidError,
    LinearNotFoundError,
    LinearRateLimitedError,
    LinearUnauthorizedError,
    LinearUnavailableError,
)
from linear.http import GRAPHQL_URL, PAGE_SIZE, HttpLinearGateway
from linear.models import NewIssue
from linear_http_support import (
    CANARY,
    ISSUE,
    LABEL,
    LINEAR_WORDS,
    NEW_ID,
    OTHER,
    PROJECT,
    STATE,
    TEAM,
    comment_node,
    connection,
    data,
    failure,
    issue_node,
    label_node,
    org,
    project_node,
)

_OPERATION = re.compile(r"(?:query|mutation)\s+(\w+)")


class Linear:
    """Scripted Linear: ``routes`` maps an operation name (``Viewer``, ``IssueCreate``) to a response, an exception or a
    list of them (the last repeats). ``sent`` keeps every request as ``(operation, variables)``."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.tokens = iter(range(1, 100))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(self.operation(request), data({}))
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        return route

    @staticmethod
    def operation(request: httpx.Request) -> str:
        found = _OPERATION.match(json.loads(request.content)["query"].strip())
        return found.group(1) if found else ""

    async def token(self) -> str:
        return CANARY

    def gateway(self) -> HttpLinearGateway:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return HttpLinearGateway(self.token, client=client)

    def sent(self, index: int = -1) -> tuple[str, dict[str, Any]]:
        request = self.requests[index]
        return self.operation(request), json.loads(request.content)["variables"]

    def query(self, index: int = -1) -> str:
        return json.loads(self.requests[index].content)["query"]


@pytest.fixture
async def make():
    made = []

    def build(routes: dict[str, Any] | None = None) -> tuple[Linear, HttpLinearGateway]:
        linear = Linear(routes)
        gateway = linear.gateway()
        made.append(gateway)
        return linear, gateway

    yield build
    for gateway in made:
        await gateway.aclose()


# --- reads ------------------------------------------------------------------------------------------------------------


async def test_every_call_is_a_json_post_with_the_current_bearer_token():
    tokens = iter(["first", "second"])

    async def token() -> str:
        return next(tokens)

    linear = Linear({"Viewer": data({"viewer": {"id": ISSUE, "name": "Ada", "organization": org()}})})
    gateway = HttpLinearGateway(token, client=httpx.AsyncClient(transport=httpx.MockTransport(linear.handler)))
    await gateway.viewer()
    await gateway.viewer()
    first, second = linear.requests
    assert (first.method, str(first.url)) == ("POST", GRAPHQL_URL)
    assert [r.headers["Authorization"] for r in (first, second)] == ["Bearer first", "Bearer second"]
    assert first.headers["Content-Type"] == "application/json"
    assert set(json.loads(first.content)) == {"query", "variables"}


async def test_a_refused_token_is_refreshed_once_and_the_request_sent_again():
    used: list[str] = []

    async def token() -> str:
        return "stale"

    async def refresh(refused: str) -> str:
        used.append(refused)
        return "fresh"

    viewer = data({"viewer": {"id": ISSUE, "name": "Ada", "organization": org()}})
    linear = Linear({"Viewer": [failure("AUTHENTICATION_ERROR", 401), viewer]})
    client = httpx.AsyncClient(transport=httpx.MockTransport(linear.handler))
    gateway = HttpLinearGateway(token, client=client, refresh=refresh)

    assert (await gateway.viewer()).name == "Ada"
    assert used == ["stale"]
    assert [r.headers["Authorization"] for r in linear.requests] == ["Bearer stale", "Bearer fresh"]


async def test_a_token_refused_twice_or_without_a_refresher_is_unauthorized():
    async def token() -> str:
        return "stale"

    async def refresh(_: str) -> str:
        return "also-refused"

    for refresher, sent in ((None, 1), (refresh, 2)):
        linear = Linear({"Viewer": failure("AUTHENTICATION_ERROR", 401)})
        client = httpx.AsyncClient(transport=httpx.MockTransport(linear.handler))
        with pytest.raises(LinearUnauthorizedError):
            await HttpLinearGateway(token, client=client, refresh=refresher).viewer()
        assert len(linear.requests) == sent


async def test_viewer_names_the_member_and_the_workspace(make):
    _, gateway = make({"Viewer": data({"viewer": {"id": ISSUE, "name": "Ada", "organization": org()}})})
    viewer = await gateway.viewer()
    assert (viewer.id, viewer.name) == (ISSUE, "Ada")
    assert (viewer.organization.id, viewer.organization.url_key) == (OTHER, "graphico-app")


async def test_teams_follow_the_cursor_over_two_pages(make):
    page_one = data({"teams": connection([{"id": TEAM, "key": "GRA", "name": "Graphico"}], "cur-1")})
    page_two = data({"teams": connection([{"id": OTHER, "key": "ENG", "name": "Engineering"}])})
    linear, gateway = make({"Teams": [page_one, page_two]})
    teams = await gateway.teams()
    assert [team.key for team in teams] == ["GRA", "ENG"]
    assert [linear.sent(i)[1] for i in (0, 1)] == [{"after": None}, {"after": "cur-1"}]
    assert f"first: {PAGE_SIZE}" in linear.query(0)
    assert PAGE_SIZE <= 100


async def test_projects_and_project_read_the_team_ids(make):
    linear, gateway = make(
        {
            "Projects": data({"team": {"projects": connection([project_node()])}}),
            "Project": data({"project": project_node(description=None)}),
        }
    )
    (project,) = await gateway.projects(TEAM)
    assert (project.id, project.name, project.team_ids, project.description) == (PROJECT, "Plan", (TEAM,), "Why")
    assert linear.sent()[1] == {"team": TEAM, "after": None}
    assert (await gateway.project(PROJECT)).description is None
    assert linear.sent()[1] == {"id": PROJECT}


async def test_workflow_states_are_ordered_by_type_then_position(make):
    def state(name: str, kind: str, position: float) -> dict[str, Any]:
        return {"id": name, "name": name, "type": kind, "position": position}

    nodes = [state("Done", "completed", 0), state("Doing", "started", 9), state("Todo", "unstarted", 5)]
    nodes += [state("Backlog", "backlog", 7), state("Review", "started", 2)]
    _, gateway = make({"States": data({"team": {"states": connection(nodes)}})})
    states = await gateway.workflow_states(TEAM)
    assert [s.name for s in states] == ["Backlog", "Todo", "Review", "Doing", "Done"]
    assert {s.team_id for s in states} == {TEAM}


async def test_labels_cover_the_team_and_the_workspace_and_keep_the_group(make):
    group = label_node(OTHER, "area", isGroup=True, team=None)
    child = label_node(NEW_ID, "api", parent={"id": OTHER})
    linear, gateway = make({"Labels": data({"team": {"id": TEAM}, "issueLabels": connection([group, child])})})
    labels = await gateway.labels(TEAM)
    assert [(label.name, label.is_group, label.parent_id, label.team_id) for label in labels] == [
        ("area", True, None, None),
        ("api", False, OTHER, TEAM),
    ]
    assert "null: true" in linear.query()


async def test_issue_maps_every_field_and_asks_by_id_or_identifier(make):
    node = issue_node(parent={"id": OTHER}, projectMilestone={"id": NEW_ID})
    linear, gateway = make({"Issue": data({"issue": node})})
    issue = await gateway.issue("GRA-12")
    assert linear.sent()[1] == {"id": "GRA-12"}
    assert (issue.id, issue.identifier, issue.title, issue.description) == (ISSUE, "GRA-12", "A title", "Body")
    assert (issue.team_id, issue.parent_id, issue.project_id, issue.milestone_id) == (TEAM, OTHER, PROJECT, NEW_ID)
    assert (issue.state.name, issue.state.type, issue.state.position, issue.state.team_id) == (
        "Todo",
        "unstarted",
        1.5,
        TEAM,
    )
    assert (issue.estimate, issue.priority, issue.label_ids) == (3, 2, (LABEL,))
    assert issue.created_at == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert issue.updated_at == datetime(2026, 3, 2, 10, tzinfo=UTC)


async def test_issue_without_optional_fields(make):
    bare = issue_node(description=None, estimate=None, priority=0, project=None, labels={"nodes": []}, assignee=None)
    linear, gateway = make({"Issue": [data({"issue": bare}), data({"issue": issue_node()})]})
    issue = await gateway.issue(ISSUE)
    assert (issue.description, issue.estimate, issue.priority, issue.project_id, issue.labels) == (
        None,
        None,
        0,
        None,
        (),
    )
    assert issue.assignee_name is None
    assert (await gateway.issue(ISSUE)).assignee_name == "Ada Lovelace"
    assert "assignee { name }" in linear.query()


async def test_issues_of_project_is_one_page_with_its_next_cursor(make):
    second = issue_node("GRA-2", id=OTHER)
    pages = [
        data({"project": {"issues": connection([issue_node("GRA-1")], "cur-1")}}),
        data({"project": {"issues": connection([second])}}),
    ]
    linear, gateway = make({"ProjectIssues": pages})
    first = await gateway.issues_of_project(PROJECT)
    assert ([i.identifier for i in first.items], first.next_cursor) == (["GRA-1"], "cur-1")
    last = await gateway.issues_of_project(PROJECT, first.next_cursor)
    assert ([i.identifier for i in last.items], last.next_cursor) == (["GRA-2"], None)
    assert [linear.sent(i)[1] for i in (0, 1)] == [
        {"id": PROJECT, "after": None},
        {"id": PROJECT, "after": "cur-1"},
    ]


async def test_children_follow_every_page(make):
    pages = [
        data({"issue": {"children": connection([issue_node("GRA-2")], "c")}}),
        data({"issue": {"children": connection([issue_node("GRA-3")])}}),
    ]
    linear, gateway = make({"Children": pages})
    assert [i.identifier for i in await gateway.children("GRA-1")] == ["GRA-2", "GRA-3"]
    assert linear.sent(1)[1] == {"id": "GRA-1", "after": "c"}


async def test_comments_are_oldest_first_and_filtered_after_since(make):
    user = {"id": OTHER, "name": "Grace"}
    nodes = [comment_node("c2", "2026-03-02T00:00:00.000Z", user), comment_node("c1", "2026-03-01T00:00:00.000Z")]
    linear, gateway = make({"Comments": data({"issue": {"comments": connection(nodes)}})})
    since = datetime(2026, 2, 28, tzinfo=UTC)
    comments = await gateway.comments(ISSUE, since)
    assert [(c.id, c.user_id, c.user_name) for c in comments] == [("c1", None, None), ("c2", OTHER, "Grace")]
    assert comments[0].issue_id == ISSUE
    assert linear.sent()[1]["filter"] == {"createdAt": {"gt": since.isoformat()}}
    await gateway.comments(ISSUE)
    assert linear.sent()[1]["filter"] is None


async def test_relations_join_both_directions_without_duplicates(make):
    def relation(relation_id: str, first: str, second: str) -> dict[str, Any]:
        return {"id": relation_id, "type": "blocks", "issue": {"id": first}, "relatedIssue": {"id": second}}

    routes = {
        "Relations": [
            data({"issue": {"relations": connection([relation("r1", ISSUE, OTHER)])}}),
            data(
                {
                    "issue": {
                        "inverseRelations": connection([relation("r2", NEW_ID, ISSUE), relation("r1", ISSUE, OTHER)])
                    }
                }
            ),
        ]
    }
    linear, gateway = make(routes)
    found = await gateway.relations(ISSUE)
    assert [(r.id, r.type, r.issue_id, r.related_issue_id) for r in found] == [
        ("r1", "blocks", ISSUE, OTHER),
        ("r2", "blocks", NEW_ID, ISSUE),
    ]
    assert "inverseRelations(" in linear.query(1)


async def test_project_documents_carry_the_project_id(make):
    node = {
        "id": OTHER,
        "title": "Spec",
        "content": None,
        "url": "https://linear.app/d",
        "updatedAt": "2026-03-01T00:00:00Z",
    }
    _, gateway = make({"Documents": data({"project": {"id": PROJECT, "documents": connection([node])}})})
    (document,) = await gateway.project_documents(PROJECT)
    assert (document.project_id, document.title, document.content) == (PROJECT, "Spec", "")
    assert document.updated_at == datetime(2026, 3, 1, tzinfo=UTC)


# --- resolve_link -----------------------------------------------------------------------------------------------------


def workspace_routes() -> dict[str, Any]:
    return {
        "Viewer": data({"viewer": {"id": ISSUE, "name": "Ada", "organization": org()}}),
        "Issue": data({"issue": issue_node()}),
        "ProjectBySlug": data({"projects": {"nodes": [project_node()]}}),
    }


@pytest.mark.parametrize(
    "link",
    [
        "https://linear.app/graphico-app/issue/GRA-12/a-title",
        "https://linear.app/graphico-app/issue/GRA-12",
        "  GRA-12 ",
    ],
)
async def test_resolve_link_reads_an_issue_by_url_or_identifier(make, link):
    linear, gateway = make(workspace_routes())
    found = await gateway.resolve_link(link)
    assert found.identifier == "GRA-12"
    assert linear.sent() == ("Issue", {"id": "GRA-12"})


async def test_resolve_link_reads_a_project_by_the_slug_id_of_its_url(make):
    linear, gateway = make(workspace_routes())
    found = await gateway.resolve_link("https://linear.app/graphico-app/project/the-plan-abc123def456/overview")
    assert (found.id, found.name) == (PROJECT, "Plan")
    assert linear.sent() == ("ProjectBySlug", {"slug": "abc123def456"})


async def test_resolve_link_refuses_another_workspace_without_reading_it(make):
    linear, gateway = make(workspace_routes())
    with pytest.raises(LinearNotFoundError):
        await gateway.resolve_link("https://linear.app/other-org/issue/GRA-12/x")
    assert [linear.operation(r) for r in linear.requests] == ["Viewer"]


async def test_resolve_link_not_found_and_invalid(make):
    routes = workspace_routes() | {"ProjectBySlug": data({"projects": {"nodes": []}})}
    _, gateway = make(routes)
    with pytest.raises(LinearNotFoundError):
        await gateway.resolve_link("https://linear.app/graphico-app/project/gone-aaaaaaaaaaaa")
    for text in ("not a link", "https://example.com/issue/GRA-1", ""):
        with pytest.raises(LinearInvalidError) as caught:
            await gateway.resolve_link(text)
        assert caught.value.field == "link"


# --- writes -----------------------------------------------------------------------------------------------------------


async def test_create_project_sends_the_client_id_and_only_the_given_fields(make):
    routes = {"ProjectCreate": data({"projectCreate": {"success": True, "project": project_node()}})}
    linear, gateway = make(routes)
    project = await gateway.create_project(PROJECT, TEAM, "Plan")
    assert project.id == PROJECT
    assert linear.sent()[1] == {"input": {"id": PROJECT, "name": "Plan", "teamIds": [TEAM]}}
    await gateway.create_project(PROJECT, TEAM, "Plan", description="Why")
    assert linear.sent()[1]["input"]["description"] == "Why"


async def test_create_milestone(make):
    node = {"id": NEW_ID, "name": "M1", "description": None, "sortOrder": 2.5, "project": {"id": PROJECT}}
    linear, gateway = make(
        {"MilestoneCreate": data({"projectMilestoneCreate": {"success": True, "projectMilestone": node}})}
    )
    milestone = await gateway.create_milestone(PROJECT, NEW_ID, "M1", description="D", sort_order=2.5)
    assert (milestone.id, milestone.project_id, milestone.name, milestone.sort_order) == (NEW_ID, PROJECT, "M1", 2.5)
    expected = {"id": NEW_ID, "projectId": PROJECT, "name": "M1", "description": "D", "sortOrder": 2.5}
    assert linear.sent()[1] == {"input": expected}


async def test_create_label_and_label_group(make):
    node = label_node(NEW_ID, "area", isGroup=True)
    linear, gateway = make({"LabelCreate": data({"issueLabelCreate": {"success": True, "issueLabel": node}})})
    label = await gateway.create_label(TEAM, NEW_ID, "area", color="#ff0000", is_group=True)
    assert (label.id, label.is_group, label.team_id) == (NEW_ID, True, TEAM)
    sent = linear.sent()[1]["input"]
    assert sent == {"id": NEW_ID, "teamId": TEAM, "name": "area", "color": "#ff0000", "isGroup": True}
    await gateway.create_label(TEAM, NEW_ID, "api", parent_id=OTHER)
    assert linear.sent()[1]["input"] == {"id": NEW_ID, "teamId": TEAM, "name": "api", "parentId": OTHER}


async def test_create_issue_maps_every_input_field(make):
    routes = {"IssueCreate": data({"issueCreate": {"success": True, "issue": issue_node()}})}
    linear, gateway = make(routes)
    new = NewIssue(
        ISSUE,
        TEAM,
        "A title",
        "Body",
        OTHER,
        PROJECT,
        NEW_ID,
        label_ids=(LABEL,),
        estimate=0,
        priority=2,
        state_id=STATE,
    )
    assert (await gateway.create_issue(new)).identifier == "GRA-12"
    assert linear.sent()[1] == {
        "input": {
            "id": ISSUE,
            "teamId": TEAM,
            "title": "A title",
            "description": "Body",
            "parentId": OTHER,
            "projectId": PROJECT,
            "projectMilestoneId": NEW_ID,
            "labelIds": [LABEL],
            "estimate": 0,
            "priority": 2,
            "stateId": STATE,
        }
    }


async def test_create_issue_leaves_out_what_was_not_given(make):
    routes = {"IssueCreate": data({"issueCreate": {"success": True, "issue": issue_node()}})}
    linear, gateway = make(routes)
    await gateway.create_issue(NewIssue(ISSUE, TEAM, "T"))
    assert linear.sent()[1] == {"input": {"id": ISSUE, "teamId": TEAM, "title": "T"}}


async def test_update_issue_renames_fields_and_can_clear_one(make):
    linear, gateway = make({"IssueUpdate": data({"issueUpdate": {"success": True, "issue": issue_node()}})})
    await gateway.update_issue(
        "GRA-12",
        title="T",
        state_id=STATE,
        label_ids=(LABEL,),
        milestone_id=None,
        project_id=PROJECT,
        parent_id=OTHER,
        priority=1,
        estimate=5,
        description="D",
    )
    assert linear.sent()[1] == {
        "id": "GRA-12",
        "input": {
            "title": "T",
            "stateId": STATE,
            "labelIds": [LABEL],
            "projectMilestoneId": None,
            "projectId": PROJECT,
            "parentId": OTHER,
            "priority": 1,
            "estimate": 5,
            "description": "D",
        },
    }


async def test_update_issue_with_an_unknown_field_is_invalid_and_sends_nothing(make):
    linear, gateway = make()
    with pytest.raises(LinearInvalidError) as caught:
        await gateway.update_issue(ISSUE, colour="red")
    assert caught.value.field == "colour"
    assert linear.requests == []


async def test_create_comment_with_and_without_a_client_id(make):
    node = comment_node(NEW_ID, "2026-03-01T00:00:00Z", {"id": OTHER, "name": "Ada"})
    linear, gateway = make({"CommentCreate": data({"commentCreate": {"success": True, "comment": node}})})
    comment = await gateway.create_comment("GRA-12", "hi", comment_id=NEW_ID)
    assert (comment.id, comment.issue_id, comment.user_name) == (NEW_ID, ISSUE, "Ada")
    assert linear.sent()[1] == {"input": {"id": NEW_ID, "issueId": "GRA-12", "body": "hi"}}
    await gateway.create_comment("GRA-12", "hi")
    assert linear.sent()[1] == {"input": {"issueId": "GRA-12", "body": "hi"}}


async def test_create_relation(make):
    node = {"id": NEW_ID, "type": "blocks", "issue": {"id": ISSUE}, "relatedIssue": {"id": OTHER}}
    linear, gateway = make({"RelationCreate": data({"issueRelationCreate": {"success": True, "issueRelation": node}})})
    relation = await gateway.create_relation(NEW_ID, ISSUE, OTHER, "blocks")
    assert (relation.id, relation.type, relation.issue_id, relation.related_issue_id) == (
        NEW_ID,
        "blocks",
        ISSUE,
        OTHER,
    )
    expected = {"id": NEW_ID, "issueId": ISSUE, "relatedIssueId": OTHER, "type": "blocks"}
    assert linear.sent()[1] == {"input": expected}


async def test_attach_link(make):
    node = {"id": NEW_ID, "url": "https://example.com/p", "title": "Plan", "issue": {"id": ISSUE}}
    linear, gateway = make({"AttachLink": data({"attachmentLinkURL": {"success": True, "attachment": node}})})
    attachment = await gateway.attach_link("GRA-12", "https://example.com/p", "Plan")
    assert (attachment.id, attachment.issue_id, attachment.url, attachment.title) == (
        NEW_ID,
        ISSUE,
        "https://example.com/p",
        "Plan",
    )
    assert linear.sent()[1] == {"issueId": "GRA-12", "url": "https://example.com/p", "title": "Plan"}


async def test_a_mutation_that_reports_no_success_is_invalid(make):
    _, gateway = make({"IssueCreate": data({"issueCreate": {"success": False, "issue": None}})})
    with pytest.raises(LinearInvalidError):
        await gateway.create_issue(NewIssue(ISSUE, TEAM, "T"))


# --- error mapping ----------------------------------------------------------------------------------------------------

AUTH = failure("AUTHENTICATION_ERROR", type="authentication error")
INPUT_ERROR = {"type": "invalid input", "userPresentableMessage": "Could not find referenced Issue."}
ESTIMATE = {"validationErrors": [{"property": "estimate"}]}
TOO_COMPLEX = {"message": "Query too complex", "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (AUTH, LinearUnauthorizedError),
        (httpx.Response(401, json={"errors": [{"message": LINEAR_WORDS}]}), LinearUnauthorizedError),
        (httpx.Response(401, text="nope"), LinearUnauthorizedError),
        (failure("FORBIDDEN", type="forbidden"), LinearForbiddenError),
        (httpx.Response(403, text="nope"), LinearForbiddenError),
        (failure("RATELIMITED", type="ratelimited"), LinearRateLimitedError),
        (httpx.Response(200, json={"errors": [TOO_COMPLEX]}), LinearRateLimitedError),
        (failure("INPUT_ERROR", 400, **INPUT_ERROR), LinearNotFoundError),
        (failure("NOT_FOUND"), LinearNotFoundError),
        (failure("INVALID_INPUT", userPresentableMessage="id must be unique"), LinearAlreadyExistsError),
        (failure("INPUT_ERROR", 400, userPresentableMessage="Entity already exists"), LinearAlreadyExistsError),
        (failure("INVALID_INPUT", **ESTIMATE), LinearInvalidError),
        (httpx.Response(400, text="bad"), LinearInvalidError),
        (httpx.Response(500, text=LINEAR_WORDS), LinearUnavailableError),
        (httpx.Response(503, json={"errors": [{"message": LINEAR_WORDS}]}), LinearUnavailableError),
        (failure("INTERNAL_SERVER_ERROR"), LinearUnavailableError),
        (httpx.Response(200, text="<html>"), LinearUnavailableError),
        (httpx.Response(200, json={}), LinearUnavailableError),
        (httpx.Response(200, json=["x"]), LinearUnavailableError),
    ],
)
async def test_every_failure_becomes_its_closed_error(make, response, expected):
    _, gateway = make({"Viewer": response})
    with pytest.raises(LinearError) as caught:
        await gateway.viewer()
    assert type(caught.value) is expected
    assert LINEAR_WORDS not in str(caught.value) + repr(caught.value)


async def test_an_invalid_input_names_the_field_from_the_validation_details(make):
    _, gateway = make({"IssueCreate": failure("INVALID_INPUT", **ESTIMATE)})
    with pytest.raises(LinearInvalidError) as caught:
        await gateway.create_issue(NewIssue(ISSUE, TEAM, "T", estimate=99))
    assert caught.value.field == "estimate"


async def test_an_invalid_input_names_the_field_from_its_text_only_inside_the_error(make):
    response = failure("INVALID_INPUT", userPresentableMessage="Estimate is not allowed by the team")
    _, gateway = make({"IssueCreate": response})
    with pytest.raises(LinearInvalidError) as caught:
        await gateway.create_issue(NewIssue(ISSUE, TEAM, "T", estimate=99))
    assert (caught.value.field, "Estimate" in str(caught.value)) == ("estimate", False)


async def test_the_milestone_validation_property_is_named_like_the_gateway_field(make):
    _, gateway = make({"IssueUpdate": failure("INVALID_INPUT", validationErrors=[{"property": "projectMilestoneId"}])})
    with pytest.raises(LinearInvalidError) as caught:
        await gateway.update_issue(ISSUE, milestone_id=OTHER)
    assert caught.value.field == "milestone_id"


async def test_a_429_carries_retry_after_and_the_status(make):
    _, gateway = make({"Viewer": httpx.Response(429, headers={"Retry-After": "17"})})
    with pytest.raises(LinearRateLimitedError) as caught:
        await gateway.viewer()
    assert (caught.value.retry_after, caught.value.http_status) == (17.0, 429)


async def test_a_graphql_rate_limit_waits_for_the_exhausted_bucket_reset(make):
    headers = {
        "X-RateLimit-Requests-Remaining": "1200",
        "X-RateLimit-Complexity-Remaining": "0",
        "X-RateLimit-Complexity-Reset": str(int((time.time() + 30) * 1000)),  # read at the call, not at import
    }
    response = httpx.Response(200, headers=headers, json={"errors": [{"extensions": {"code": "RATELIMITED"}}]})
    _, gateway = make({"Viewer": response})
    with pytest.raises(LinearRateLimitedError) as caught:
        await gateway.viewer()
    assert caught.value.retry_after is not None
    assert 20 < caught.value.retry_after <= 30


async def test_a_rate_limit_without_any_header_has_no_retry_after(make):
    _, gateway = make({"Viewer": failure("RATELIMITED")})
    with pytest.raises(LinearRateLimitedError) as caught:
        await gateway.viewer()
    assert caught.value.retry_after is None


@pytest.mark.parametrize(
    "fault", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("x")]
)
async def test_a_network_failure_is_unavailable(make, fault):
    _, gateway = make({"Viewer": fault})
    with pytest.raises(LinearUnavailableError) as caught:
        await gateway.viewer()
    assert caught.value.http_status is None


async def test_a_missing_object_on_a_read_is_not_found(make):
    _, gateway = make({"Issue": data({"issue": None}), "Children": data({"issue": None})})
    with pytest.raises(LinearNotFoundError):
        await gateway.issue("GRA-404")
    with pytest.raises(LinearNotFoundError):
        await gateway.children("GRA-404")


async def test_an_answer_of_the_wrong_shape_is_unavailable(make):
    _, gateway = make({"Issue": data({"issue": {"id": ISSUE}})})
    with pytest.raises(LinearUnavailableError):
        await gateway.issue(ISSUE)


# --- leaks ------------------------------------------------------------------------------------------------------------


async def test_token_and_linear_text_never_reach_logs_errors_or_repr(make, caplog):
    caplog.set_level(logging.DEBUG)
    headers = {"x-request-id": "req-123"}
    linear, gateway = make(
        {
            "Viewer": [
                httpx.Response(401, headers=headers, json={"errors": [{"message": f"{LINEAR_WORDS} {CANARY}"}]}),
                httpx.ConnectError(f"boom {CANARY}"),
                httpx.Response(500, text=CANARY),
                failure("RATELIMITED", userPresentableMessage=CANARY),
            ]
        }
    )
    errors = []
    for _ in range(4):
        with pytest.raises(LinearError) as caught:
            await gateway.viewer()
        errors.append(caught.value)
    texts = [text for error in errors for text in (str(error), repr(error))] + [repr(gateway), str(gateway)]
    records = [record.getMessage() for record in caplog.records] + [repr(vars(record)) for record in caplog.records]
    assert records
    assert any("req-123" in text for text in records)
    assert not [text for text in texts + records if CANARY in text or LINEAR_WORDS in text]
    assert CANARY not in repr(gateway._client)
    assert linear.requests[0].headers["Authorization"] == f"Bearer {CANARY}"


async def test_the_gateway_closes_only_a_client_it_made():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: data({})))
    borrowed = HttpLinearGateway(Linear().token, client=client)
    await borrowed.aclose()
    assert not client.is_closed
    await client.aclose()
    owned = HttpLinearGateway(Linear().token)
    await owned.aclose()
    assert owned._client.is_closed
