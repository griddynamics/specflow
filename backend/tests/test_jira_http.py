"""``HttpJiraGateway`` on ``httpx.MockTransport``: every call of sync.md#jira-calls, retries, leaks."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from pydantic import SecretStr

from jira.gateway import JiraError
from jira.http import PERMISSIONS, HttpJiraGateway
from jira.reasons import Reason

CANARY = "canary-token-9f3a7c1e5b"
EMAIL = "svc.user@example.com"
SITE = "https://example.atlassian.net"


class Jira:
    """Scripted Jira: ``routes`` maps ``METHOD /path`` to a response or a list of responses (the last repeats)."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request):
        self.requests.append(request)
        route = self.routes.get(f"{request.method} {request.url.path}", httpx.Response(200, json={}))
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, Exception):
            raise route
        return route(request) if callable(route) else route

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    def gateway(self):
        return HttpJiraGateway(
            SITE, EMAIL, SecretStr(CANARY), transport=httpx.MockTransport(self.handler), sleep=self.sleep
        )

    def sent(self, index=-1):
        request = self.requests[index]
        return request.method, request.url.path, json.loads(request.content) if request.content else None


@pytest.fixture
async def make():
    made = []

    def build(routes=None):
        jira = Jira(routes)
        jira.gw = jira.gateway()
        made.append(jira.gw)
        return jira

    yield build
    for gateway in made:
        await gateway.aclose()


def status_page(*issues, page=None):
    body = {"issues": list(issues)}
    if page:
        body["nextPageToken"] = page
    return httpx.Response(200, json=body)


def issue(key, category="new", name="To Do", links=()):
    return {
        "key": key,
        "fields": {"status": {"name": name, "statusCategory": {"key": category}}, "issuelinks": list(links)},
    }


async def test_basic_auth_and_json_headers(make):
    jira = make({"GET /rest/api/3/myself": httpx.Response(200, json={"accountId": "acc-1"})})
    assert await jira.gw.myself() == "acc-1"
    request = jira.requests[0]
    assert str(request.url) == f"{SITE}/rest/api/3/myself"
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(f"{EMAIL}:{CANARY}".encode()).decode()
    assert request.headers["Accept"] == "application/json"


async def test_project_reads_issue_type_ids(make):
    body = {
        "name": "Platform",
        "issueTypes": [
            {"id": "1", "name": "Task", "subtask": False},
            {"id": "10000", "name": "Epic", "subtask": False},
            {"id": "10001", "name": "Story", "subtask": False},
            {"id": "10003", "name": "Sub-task", "subtask": True},
        ],
    }
    jira = make({"GET /rest/api/3/project/PRJ": httpx.Response(200, json=body)})
    project = await jira.gw.project("PRJ")
    assert (project.name, project.epic_type_id, project.story_type_id, project.subtask_type_id) == (
        "Platform",
        "10000",
        "10001",
        "10003",
    )


async def test_project_with_missing_types_gives_none(make):
    body = {"name": "P", "issueTypes": [{"id": "1", "name": "Task", "subtask": False}]}
    project = await make({"GET /rest/api/3/project/PRJ": httpx.Response(200, json=body)}).gw.project("PRJ")
    assert (project.epic_type_id, project.story_type_id, project.subtask_type_id) == (None, None, None)


async def test_missing_permissions(make):
    permissions = {name: {"havePermission": name not in {"LINK_ISSUES", "SCHEDULE_ISSUES"}} for name in PERMISSIONS}
    jira = make({"GET /rest/api/3/mypermissions": httpx.Response(200, json={"permissions": permissions})})
    assert await jira.gw.missing_permissions("PRJ") == {"LINK_ISSUES", "SCHEDULE_ISSUES"}
    query = jira.requests[0].url.params
    assert query["projectKey"] == "PRJ"
    assert query["permissions"] == "BROWSE_PROJECTS,CREATE_ISSUES,EDIT_ISSUES,ADD_COMMENTS,LINK_ISSUES,SCHEDULE_ISSUES"


async def test_absent_permission_counts_as_missing(make):
    jira = make({"GET /rest/api/3/mypermissions": httpx.Response(200, json={"permissions": {}})})
    assert len(await jira.gw.missing_permissions("PRJ")) == 6


async def test_issue_summary(make):
    body = {"fields": {"summary": "Epic one", "issuetype": {"name": "Epic"}, "project": {"key": "PRJ"}}}
    jira = make({"GET /rest/api/3/issue/PRJ-1": httpx.Response(200, json=body)})
    summary = await jira.gw.issue_summary("PRJ-1")
    assert (summary.project_key, summary.type_name, summary.summary) == ("PRJ", "Epic", "Epic one")
    assert jira.requests[0].url.params["fields"] == "summary,issuetype,project"


async def test_story_points_field_is_looked_up_once(make):
    fields = [{"id": "customfield_1", "name": "Other"}, {"id": "customfield_90001", "name": "Story Points"}]
    jira = make({"GET /rest/api/3/field": httpx.Response(200, json=fields)})
    assert await jira.gw.story_points_field() == "customfield_90001"
    assert await jira.gw.story_points_field() == "customfield_90001"
    assert len(jira.requests) == 1


async def test_story_points_field_absent_is_cached_as_none(make):
    jira = make({"GET /rest/api/3/field": httpx.Response(200, json=[{"id": "x", "name": "Story points (old)"}])})
    assert await jira.gw.story_points_field() is None
    assert await jira.gw.story_points_field() is None
    assert len(jira.requests) == 1


async def test_create_sends_fields_and_property_in_one_request(make):
    jira = make({"POST /rest/api/3/issue": httpx.Response(201, json={"id": "9", "key": "PRJ-7"})})
    fields = {
        "project": {"key": "PRJ"},
        "issuetype": {"id": "10001"},
        "summary": "S",
        "parent": {"key": "PRJ-1"},
        "labels": ["specflow"],
    }
    assert await jira.gw.create_issue(fields, project_id="p-1", node_id="STORY::a::b") == "PRJ-7"
    method, path, body = jira.sent()
    assert (method, path) == ("POST", "/rest/api/3/issue")
    assert body == {
        "fields": fields,
        "properties": [{"key": "specflow", "value": {"project_id": "p-1", "node_id": "STORY::a::b"}}],
    }
    assert "customfield" not in json.dumps(body)
    assert "Epic Name" not in json.dumps(body)


async def test_edit_sends_fields_and_label_operations(make):
    jira = make({"PUT /rest/api/3/issue/PRJ-7": httpx.Response(204)})
    await jira.gw.edit_issue("PRJ-7", fields={"summary": "T"}, add_labels=["a", "b"], remove_labels=["c"])
    assert jira.sent() == (
        "PUT",
        "/rest/api/3/issue/PRJ-7",
        {"fields": {"summary": "T"}, "update": {"labels": [{"add": "a"}, {"add": "b"}, {"remove": "c"}]}},
    )


async def test_edit_with_nothing_to_change_makes_no_call(make):
    jira = make()
    await jira.gw.edit_issue("PRJ-7", fields={})
    assert jira.requests == []


async def test_issue_states_one_search_with_status_and_blocked_by(make):
    link = {"id": "555", "type": {"name": "Blocks"}, "inwardIssue": {"key": "PRJ-1"}}
    outward = {"id": "556", "type": {"name": "Blocks"}, "outwardIssue": {"key": "PRJ-9"}}
    relates = {"id": "557", "type": {"name": "Relates"}, "inwardIssue": {"key": "PRJ-3"}}
    page = status_page(issue("PRJ-2", "indeterminate", "In Progress", [link, outward, relates]), issue("PRJ-1"))
    jira = make({"POST /rest/api/3/search/jql": page})
    states = await jira.gw.issue_states(["PRJ-1", "PRJ-2"])
    assert states["PRJ-1"].category == "new"
    assert states["PRJ-1"].blocked_by == {}
    assert (states["PRJ-2"].category, states["PRJ-2"].status) == ("indeterminate", "In Progress")
    assert states["PRJ-2"].blocked_by == {"PRJ-1": "555"}
    _, _, body = jira.sent()
    assert body["jql"] == 'key in ("PRJ-1","PRJ-2")'
    assert body["fields"] == ["status", "issuelinks"]


async def test_issue_states_pages_by_100_keys_and_follows_the_token(make):
    keys = [f"PRJ-{n}" for n in range(1, 151)]

    def answer(request):
        body = json.loads(request.content)
        wanted = [k.strip('"') for k in body["jql"][len("key in (") : -1].split(",")]
        if body.get("nextPageToken") is None and len(wanted) == 100:
            return status_page(*(issue(k) for k in wanted[:60]), page="t2")
        if body.get("nextPageToken") == "t2":
            return status_page(*(issue(k) for k in wanted if k not in {f"PRJ-{n}" for n in range(1, 61)}))
        return status_page(*(issue(k) for k in wanted))

    jira = make({"POST /rest/api/3/search/jql": answer})
    states = await jira.gw.issue_states(keys)
    assert set(states) == set(keys)
    assert all(state is not None for state in states.values())
    assert len(jira.requests) == 3
    assert json.loads(jira.requests[1].content)["nextPageToken"] == "t2"


async def test_refused_status_page_is_read_one_by_one_and_404_is_none(make):
    jira = make(
        {
            "POST /rest/api/3/search/jql": httpx.Response(400, json={"errorMessages": ["secret text"]}),
            "GET /rest/api/3/issue/PRJ-1": httpx.Response(200, json=issue("PRJ-1", "done", "Done")),
            "GET /rest/api/3/issue/PRJ-2": httpx.Response(404),
        }
    )
    states = await jira.gw.issue_states(["PRJ-1", "PRJ-2"])
    assert states["PRJ-1"].category == "done"
    assert states["PRJ-2"] is None
    reads = [r for r in jira.requests if r.method == "GET"]
    assert [r.url.params["fields"] for r in reads] == ["status,issuelinks"] * 2


async def test_key_missing_from_a_good_page_is_read_one_by_one(make):
    jira = make(
        {
            "POST /rest/api/3/search/jql": status_page(issue("PRJ-1")),
            "GET /rest/api/3/issue/PRJ-2": httpx.Response(404),
        }
    )
    states = await jira.gw.issue_states(["PRJ-1", "PRJ-2"])
    assert states["PRJ-1"] is not None
    assert states["PRJ-2"] is None
    assert [r.url.path for r in jira.requests if r.method == "GET"] == ["/rest/api/3/issue/PRJ-2"]


async def test_one_by_one_read_other_errors_stop_the_call(make):
    jira = make(
        {
            "POST /rest/api/3/search/jql": httpx.Response(400),
            "GET /rest/api/3/issue/PRJ-1": httpx.Response(403),
        }
    )
    with pytest.raises(JiraError) as caught:
        await jira.gw.issue_states(["PRJ-1"])
    assert caught.value.reason is Reason.FORBIDDEN


async def test_find_created_keeps_only_this_projects_property(make):
    def found(key, project_id, node_id):
        return {"key": key, "properties": {"specflow": {"project_id": project_id, "node_id": node_id}}}

    page = status_page(found("PRJ-5", "p-1", "STORY::a::b"), found("PRJ-6", "p-2", "STORY::c::d"), {"key": "PRJ-7"})
    jira = make({"POST /rest/api/3/search/jql": page})
    assert await jira.gw.find_created("PRJ", project_id="p-1", minutes=20) == {"STORY::a::b": "PRJ-5"}
    _, _, body = jira.sent()
    assert body["jql"] == 'project = "PRJ" AND labels = specflow AND created >= -20m'
    assert body["properties"] == ["specflow"]


async def test_comment_link_and_remote_link_bodies(make):
    jira = make({"POST /rest/api/3/issueLink": httpx.Response(201)})
    doc = {"type": "doc", "version": 1, "content": []}
    await jira.gw.add_comment("PRJ-2", doc)
    assert jira.sent() == ("POST", "/rest/api/3/issue/PRJ-2/comment", {"body": doc})
    await jira.gw.upsert_remote_link("PRJ-2", global_id="specflow:p:n", url="https://x/plan", title="Specflow plan")
    assert jira.sent() == (
        "POST",
        "/rest/api/3/issue/PRJ-2/remotelink",
        {"globalId": "specflow:p:n", "object": {"url": "https://x/plan", "title": "Specflow plan"}},
    )


async def test_blocks_link_direction_prerequisite_is_inward(make):
    jira = make()
    await jira.gw.add_blocks_link(prerequisite="PRJ-1", waiter="PRJ-2")
    assert jira.sent() == (
        "POST",
        "/rest/api/3/issueLink",
        {"type": {"name": "Blocks"}, "inwardIssue": {"key": "PRJ-1"}, "outwardIssue": {"key": "PRJ-2"}},
    )


async def test_rank_before_and_after(make):
    jira = make()
    await jira.gw.rank(["PRJ-3", "PRJ-4"], before="PRJ-2")
    assert jira.sent() == (
        "PUT",
        "/rest/agile/1.0/issue/rank",
        {"issues": ["PRJ-3", "PRJ-4"], "rankBeforeIssue": "PRJ-2"},
    )
    await jira.gw.rank(["PRJ-3"], after="PRJ-9")
    assert jira.sent()[2] == {"issues": ["PRJ-3"], "rankAfterIssue": "PRJ-9"}


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"before": "A-1", "after": "A-2"}, {"before": "A-1", "keys": [f"A-{n}" for n in range(51)]}],
)
async def test_rank_refuses_bad_arguments(make, kwargs):
    jira = make()
    keys = kwargs.pop("keys", ["PRJ-3"])
    with pytest.raises(ValueError, match="at most 50 keys"):
        await jira.gw.rank(keys, **kwargs)
    assert jira.requests == []


async def test_delete_link(make):
    jira = make({"DELETE /rest/api/3/issueLink/555": httpx.Response(204)})
    await jira.gw.delete_link("555")
    assert jira.sent() == ("DELETE", "/rest/api/3/issueLink/555", None)


async def test_a_full_run_never_deletes_an_issue_or_transitions(make):
    jira = make({"POST /rest/api/3/issue": httpx.Response(201, json={"key": "PRJ-7"})})
    gw = jira.gw
    await gw.create_issue({"summary": "S"}, project_id="p", node_id="n")
    await gw.edit_issue("PRJ-7", fields={"summary": "T"}, add_labels=["x"], remove_labels=["y"])
    await gw.add_comment("PRJ-7", {})
    await gw.add_blocks_link(prerequisite="PRJ-1", waiter="PRJ-7")
    await gw.delete_link("1")
    await gw.rank(["PRJ-7"], before="PRJ-1")
    await gw.upsert_remote_link("PRJ-7", global_id="g", url="u", title="t")
    await gw.issue_states(["PRJ-7"])
    await gw.find_created("PRJ", project_id="p", minutes=5)
    for request in jira.requests:
        assert "/transitions" not in request.url.path
        if request.method == "DELETE":
            assert request.url.path.startswith("/rest/api/3/issueLink/")


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, Reason.INVALID_FIELD),
        (401, Reason.AUTH_FAILED),
        (403, Reason.FORBIDDEN),
        (404, Reason.NOT_FOUND),
        (409, Reason.UNEXPECTED),
        (302, Reason.UNEXPECTED),
    ],
)
async def test_status_maps_to_a_reason_without_retry(make, status, reason):
    jira = make({"GET /rest/api/3/myself": httpx.Response(status, json={"message": "Jira words"})})
    with pytest.raises(JiraError) as caught:
        await jira.gw.myself()
    assert caught.value.reason is reason
    assert caught.value.http_status == status
    assert str(caught.value) == reason.value
    assert len(jira.requests) == 1
    assert jira.sleeps == []


async def test_non_json_success_is_unexpected(make):
    jira = make({"GET /rest/api/3/myself": httpx.Response(200, text="<html>")})
    with pytest.raises(JiraError) as caught:
        await jira.gw.myself()
    assert caught.value.reason is Reason.UNEXPECTED


async def test_429_waits_retry_after_then_succeeds(make):
    limited = httpx.Response(429, headers={"Retry-After": "7"})
    jira = make({"GET /rest/api/3/myself": [limited, limited, httpx.Response(200, json={"accountId": "a"})]})
    assert await jira.gw.myself() == "a"
    assert jira.sleeps == [7, 7]


async def test_429_without_retry_after_waits_one_second(make):
    jira = make({"GET /rest/api/3/myself": [httpx.Response(429), httpx.Response(200, json={"accountId": "a"})]})
    await jira.gw.myself()
    assert jira.sleeps == [1]


async def test_sixth_429_ends_with_unreachable(make):
    jira = make({"GET /rest/api/3/myself": httpx.Response(429, headers={"Retry-After": "2"})})
    with pytest.raises(JiraError) as caught:
        await jira.gw.myself()
    assert caught.value.reason is Reason.UNREACHABLE
    assert len(jira.requests) == 6
    assert jira.sleeps == [2] * 5


async def test_retry_after_over_60_seconds_stops_without_sleeping(make):
    jira = make({"GET /rest/api/3/myself": httpx.Response(429, headers={"Retry-After": "120"})})
    with pytest.raises(JiraError) as caught:
        await jira.gw.myself()
    assert caught.value.reason is Reason.UNREACHABLE
    assert jira.sleeps == []
    assert len(jira.requests) == 1


async def test_retry_after_of_exactly_60_seconds_is_waited(make):
    jira = make(
        {"GET /rest/api/3/myself": [httpx.Response(429, headers={"Retry-After": "60"}), httpx.Response(200, json={})]}
    )
    await jira.gw.myself()
    assert jira.sleeps == [60]


async def test_5xx_backs_off_1_2_4_then_unreachable(make):
    jira = make({"GET /rest/api/3/myself": httpx.Response(503, text="down")})
    with pytest.raises(JiraError) as caught:
        await jira.gw.myself()
    assert caught.value.reason is Reason.UNREACHABLE
    assert caught.value.http_status == 503
    assert jira.sleeps == [1, 2, 4]
    assert len(jira.requests) == 4


async def test_transport_error_and_timeout_are_retried(make):
    ok = httpx.Response(200, json={"accountId": "a"})
    jira = make({"GET /rest/api/3/myself": [httpx.ConnectError("no route"), httpx.ReadTimeout("slow"), ok]})
    assert await jira.gw.myself() == "a"
    assert jira.sleeps == [1, 2]


async def test_persistent_timeout_is_unreachable_without_status(make):
    jira = make({"GET /rest/api/3/myself": httpx.ReadTimeout("slow")})
    with pytest.raises(JiraError) as caught:
        await jira.gw.myself()
    assert (caught.value.reason, caught.value.http_status) == (Reason.UNREACHABLE, None)


async def test_the_client_timeout_is_30_seconds(make):
    assert make().gw._client.timeout == httpx.Timeout(30)


async def test_token_never_reaches_logs_errors_or_repr(make, caplog):
    caplog.set_level(logging.DEBUG)
    jira = make(
        {
            "GET /rest/api/3/myself": [
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.ConnectError(f"boom {CANARY}"),
                httpx.Response(500, text=CANARY),
                httpx.Response(401, json={"message": CANARY}),
            ]
        }
    )
    with pytest.raises(JiraError) as caught:
        await jira.gw.myself()
    texts = [str(caught.value), repr(caught.value), repr(jira.gw), str(jira.gw)]
    texts += [record.getMessage() for record in caplog.records] + [repr(vars(record)) for record in caplog.records]
    assert caplog.records
    assert not [text for text in texts if CANARY in text or EMAIL in text]
    assert CANARY not in repr(jira.gw._client)
    assert "Jira words" not in repr(caught.value)


# --- failure handling and request shape

CREATE = "POST /rest/api/3/issue"
CREATED = httpx.Response(201, json={"id": "9", "key": "PRJ-7"})


async def create(gateway):
    return await gateway.create_issue({"summary": "S"}, project_id="p-1", node_id="STORY::a::b")


async def comment(gateway):
    await gateway.add_comment("PRJ-7", {"type": "doc"})


async def blocks_link(gateway):
    await gateway.add_blocks_link(prerequisite="PRJ-1", waiter="PRJ-2")


@pytest.mark.parametrize(
    ("route", "call"),
    [(CREATE, create), ("POST /rest/api/3/issue/PRJ-7/comment", comment), ("POST /rest/api/3/issueLink", blocks_link)],
)
@pytest.mark.parametrize("failure", [httpx.Response(502), httpx.ReadTimeout("slow"), httpx.ConnectError("reset")])
async def test_a_create_a_comment_and_a_link_are_not_sent_again_after_a_timeout_or_a_5xx(make, route, call, failure):
    # Jira may have done the call; a second POST could duplicate it, so the next pass recovers it instead.
    jira = make({route: [failure, httpx.Response(201, json={"key": "PRJ-8"})]})
    with pytest.raises(JiraError) as caught:
        await call(jira.gw)
    assert caught.value.reason is Reason.UNREACHABLE
    assert len(jira.requests) == 1 and jira.sleeps == []


async def test_a_create_is_still_retried_after_a_429_because_jira_did_not_process_it(make):
    jira = make({CREATE: [httpx.Response(429, headers={"Retry-After": "3"}), CREATED]})
    assert await create(jira.gw) == "PRJ-7"
    assert jira.sleeps == [3] and len(jira.requests) == 2


async def test_a_create_answered_without_a_key_is_unexpected(make):
    # Jira answered 2xx but gave no key
    jira = make({CREATE: httpx.Response(201, json={"id": "9"})})
    with pytest.raises(JiraError) as caught:
        await create(jira.gw)
    assert caught.value.reason is Reason.UNEXPECTED


async def test_a_rank_answered_207_is_a_refused_move(make):
    # Jira ranked some issues and refused others
    jira = make({"PUT /rest/agile/1.0/issue/rank": httpx.Response(207, json={"entries": []})})
    with pytest.raises(JiraError) as caught:
        await jira.gw.rank(["PRJ-3"], before="PRJ-2")
    assert caught.value.reason is Reason.INVALID_FIELD


async def test_a_rank_answered_204_is_done(make):
    jira = make({"PUT /rest/agile/1.0/issue/rank": httpx.Response(204)})
    await jira.gw.rank(["PRJ-3"], before="PRJ-2")


@pytest.mark.parametrize("site", ["http://example.atlassian.net", "example.atlassian.net", "ftp://example.net"])
def test_the_gateway_refuses_a_site_that_is_not_https(site):
    # The token would travel in clear
    with pytest.raises(ValueError, match="https"):
        HttpJiraGateway(site, EMAIL, SecretStr(CANARY))


async def test_a_key_is_quoted_in_the_path(make):
    # A key with a space and a slash cannot escape the path
    jira = make()
    await jira.gw.add_comment("A B/C", {"type": "doc"})
    assert jira.requests[0].url.raw_path == b"/rest/api/3/issue/A%20B%2FC/comment"


async def test_retry_after_given_as_an_http_date_is_waited(make):
    # Retry-After as an HTTP date
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    jira = make(
        {"GET /rest/api/3/myself": [httpx.Response(429, headers={"Retry-After": when}), httpx.Response(200, json={})]}
    )
    await jira.gw.myself()
    assert 25 < jira.sleeps[0] <= 30


async def test_retry_after_that_is_neither_a_number_nor_a_date_waits_one_second(make):
    jira = make(
        {"GET /rest/api/3/myself": [httpx.Response(429, headers={"Retry-After": "soon"}), httpx.Response(200, json={})]}
    )
    await jira.gw.myself()
    assert jira.sleeps == [1]
