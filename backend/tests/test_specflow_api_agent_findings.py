"""``POST /findings`` (``add_finding``; FR-MCP-WRITE-01..04, AC9-AC14, tools.md "add_finding", "Repeats"): the finding
and its repeat, the blocking guard, the typed errors and what a refused call leaves behind, over the real
``FindingFlow``, ``Findings`` and ``PlanService`` (``people_support``).

Sequence of a blocking finding (A19, A60, A64)::

    agent -> route: POST /findings (kind, about, story_id, claim_token, blocking)
    route -> FindingFlow: add_finding
    FindingFlow -> Findings: match (a repeat answers here, before the guard)
    FindingFlow -> PlanService: check_block (holder, token, a node of the story)
    FindingFlow -> Findings: add -> PlanService: block_story (a failure withdraws the finding)
    route -> agent: finding, created, blocked
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from agent_contracts import AGENT_ROUTES, FindingResult
from people_support import API, UI, PeopleRig, serve_people
from specflow_api_agent_support import ALPHA, agent_env, bearer_header, events, mcp_token

FINDINGS, CLAIM = AGENT_ROUTES["add_finding"], AGENT_ROUTES["claim_story"]
API_NODE, UI_NODE = "FEAT::API", "FEAT::UI"
GUESS = "nope"  # a claim token nobody holds
API_TASKS = ["TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"]


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


def finding_body(**more: Any) -> dict[str, Any]:
    return {"kind": "question", "text": "Does the order limit count cancelled orders?", "about": [API_NODE], **more}


def mine(people: PeopleRig) -> dict[str, dict[str, Any]]:
    """The findings the caller wrote through the route: the seeded two are Alice's blocking and Ben's note."""

    return {key: row for key, row in people.gap.findings().items() if row["text"] not in SEEDED}


SEEDED = {"Which currencies does the API accept?", "The API is slow | very.\nReally."}


async def claim(people: PeopleRig, story_id: str = API) -> str:
    return (await people.post(CLAIM, {"story_id": story_id}, project_id=ALPHA)).json()["claim_token"]


def snapshot(people: PeopleRig) -> tuple[Any, ...]:
    """Everything a write could change: the plan's rows and the findings store with its calls."""

    return (
        {key: dict(row) for key, row in people.memory.stories.items()},
        {key: dict(row) for key, row in people.memory.tasks.items()},
        {key: dict(row) for key, row in people.gap.findings().items()},
        len(people.gap.refs()),
    )


# --- the finding ------------------------------------------------------------------------------------------------


async def test_a_finding_is_written_with_its_about_and_refers_to_edges_and_answers_in_the_tools_md_shape(
    monkeypatch, env, caplog
):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        body = finding_body(refers_to=[{"type": "commit", "value": "E44F0C1"}, {"type": "file", "value": "src/a.py"}])
        response = await people.post(FINDINGS, body, project_id=ALPHA)
        written = mine(people)
    assert response.status_code == 200 and response.headers["X-SpecFlow-User-Id"] == str(people.alice)
    answer = response.json()
    assert FindingResult.model_validate(answer)
    finding = answer["finding"]
    assert (answer["project_id"], answer["created"], answer["blocked"]) == (ALPHA, True, [])
    assert finding["finding_id"].startswith("FND::") and finding["finding_id"] != "FND::1"
    assert {key: finding[key] for key in ("kind", "text", "status", "blocking", "about", "story_id", "orphaned")} == {
        "kind": "question",
        "text": "Does the order limit count cancelled orders?",
        "status": "open",
        "blocking": False,
        "about": [API_NODE],
        "story_id": None,
        "orphaned": False,
    }
    assert finding["refers_to"] == [{"type": "commit", "value": "e44f0c1"}, {"type": "file", "value": "src/a.py"}]
    assert finding["author"] == {"name": "Alice", "you": True, "agent_label": None}
    assert len(written) == 1 and next(iter(written.values()))["created_by"] == str(people.alice)
    assert next(iter(written.values()))["origin"] == "agent"
    logged = events(caplog, "api.agent.written")
    assert [(e.route, e.outcome, e.repeat, e.finding_id) for e in logged] == [
        ("findings", "ok", False, finding["finding_id"])
    ]
    assert "cancelled orders" not in caplog.text and "src/a.py" not in caplog.text and "e44f0c1" not in caplog.text


async def test_the_same_finding_again_returns_the_existing_one_with_created_false_and_writes_nothing(
    monkeypatch, env, caplog
):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        first = await people.post(FINDINGS, finding_body(), project_id=ALPHA)
        before = snapshot(people)
        again = await people.post(
            FINDINGS,
            finding_body(text="  does the ORDER limit   count cancelled orders? ", about=[API_NODE]),
            project_id=ALPHA,
        )
        assert snapshot(people) == before
    assert again.status_code == 200
    assert again.json()["created"] is False and again.json()["blocked"] == []
    assert again.json()["finding"] == first.json()["finding"]  # the same answer shape and content
    assert [e.repeat for e in events(caplog, "api.agent.written")] == [False, True]


async def test_a_repeat_is_answered_before_the_blocking_rules_so_its_flags_do_not_matter(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        note = await people.post(FINDINGS, finding_body(kind="note", text="Fine."), project_id=ALPHA)
        question = await people.post(FINDINGS, finding_body(text="Which one?"), project_id=ALPHA)
        before = snapshot(people)
        as_blocking_note = await people.post(
            FINDINGS, finding_body(kind="note", text="Fine.", blocking=True), project_id=ALPHA
        )
        without_claim = await people.post(FINDINGS, finding_body(text="Which one?", blocking=True), project_id=ALPHA)
        wrong_token = await people.post(
            FINDINGS, finding_body(text="Which one?", blocking=True, story_id=API, claim_token=GUESS), project_id=ALPHA
        )
        assert snapshot(people) == before
    for repeat, first in ((as_blocking_note, note), (without_claim, question), (wrong_token, question)):
        assert repeat.status_code == 200 and repeat.json()["created"] is False
        assert repeat.json()["finding"]["finding_id"] == first.json()["finding"]["finding_id"]


async def test_another_members_identical_finding_is_a_finding_of_its_own(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        theirs = await people.rig.client.post(
            FINDINGS, params={"project_id": ALPHA}, json=finding_body(), headers=ben_header()
        )
        mine_ = await people.post(FINDINGS, finding_body(), project_id=ALPHA)
    assert theirs.json()["created"] is True and mine_.json()["created"] is True
    assert theirs.json()["finding"]["finding_id"] != mine_.json()["finding"]["finding_id"]
    assert theirs.json()["finding"]["author"]["you"] is True  # you = the caller, whoever that is


def ben_header() -> dict[str, str]:
    return bearer_header(mcp_token("ben@example.com"))


# --- the rules in the order of tools.md -------------------------------------------------------------------------


async def test_an_about_id_that_resolves_to_no_node_is_node_not_found_naming_the_ids_and_writes_nothing(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        before = snapshot(people)
        response = await people.post(
            FINDINGS, finding_body(about=[API_NODE, "REQ::NOPE", "REQ::NONE"]), project_id=ALPHA
        )
        assert snapshot(people) == before
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "node_not_found"
    assert response.json()["error"]["details"] == {"spec_ids": ["REQ::NONE", "REQ::NOPE"]} or response.json()["error"][
        "details"
    ] == {"spec_ids": ["REQ::NOPE", "REQ::NONE"]}


async def test_a_story_that_is_not_in_the_plan_is_story_not_found_and_without_a_plan_no_plan(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        unknown = await people.post(FINDINGS, finding_body(story_id="STORY::NOPE"), project_id=ALPHA)
        before = snapshot(people)
        people.memory.plan = None
        no_plan = await people.post(FINDINGS, finding_body(story_id=API), project_id=ALPHA)
        assert snapshot(people)[2:] == before[2:]
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "story_not_found")
    assert (no_plan.status_code, no_plan.json()["error"]["code"]) == (404, "no_plan")


@pytest.mark.parametrize(
    "refers_to",
    [
        [{"type": "commit", "value": "xyz"}],
        [{"type": "commit", "value": "abc"}],
        [{"type": "file", "value": "/etc/passwd"}],
        [{"type": "file", "value": "a/../b"}],
        [{"type": "file", "value": "a\\b"}],
        [{"type": "file", "value": "x" * 501}],
        [{"type": "url", "value": "ftp://example.com/x"}],
        [{"type": "url", "value": "https://user:pw@example.com/x"}],
        [{"type": "url", "value": "https:///nohost"}],
        [{"type": "url", "value": "https://example.com/" + "a" * 2000}],
        [{"type": "pr", "value": "not a pr"}],
        [{"type": "pr", "value": "javascript:alert(1)"}],
        [{"type": "url", "value": "https://example.com/ with space"}],
    ],
)
async def test_a_refers_to_that_breaks_its_form_is_invalid_arguments_naming_refers_to_and_writes_nothing(
    monkeypatch, env, refers_to
):
    async with serve_people(monkeypatch, env) as people:
        before = snapshot(people)
        response = await people.post(FINDINGS, finding_body(refers_to=refers_to), project_id=ALPHA)
        assert snapshot(people) == before
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_arguments"
    assert response.json()["error"]["details"] == {"argument": "refers_to"}
    assert "xyz" not in response.text and "passwd" not in response.text


async def test_refers_to_values_are_stored_and_never_fetched(monkeypatch, env):
    """A URL, a pull request URL or number, a commit and a file are stored as text: no socket is opened and no HTTP
    client sends anything while the route runs (A25, SPECS section 10)."""

    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("a refers_to value was fetched")

    async with serve_people(monkeypatch, env) as people:
        await people.get(AGENT_ROUTES["list_areas"], project_id=ALPHA)  # the token check fetches its keys once
        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)  # the rig's client is ASGI
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
        refs = [
            {"type": "url", "value": "http://169.254.169.254/latest/meta-data"},
            {"type": "pr", "value": "https://git.example.test/org/repo/pull/7"},
            {"type": "pr", "value": "7"},
            {"type": "commit", "value": "e44f0c1"},
            {"type": "file", "value": "src/api/app.py"},
        ]
        response = await people.post(FINDINGS, finding_body(refers_to=refs), project_id=ALPHA)
    assert response.status_code == 200
    assert sorted(response.json()["finding"]["refers_to"], key=str) == sorted(refs, key=str)


@pytest.mark.parametrize("text", ["   ", "\n\t"])
async def test_a_text_of_white_space_only_is_invalid_arguments_naming_text(monkeypatch, env, text):
    async with serve_people(monkeypatch, env) as people:
        before = snapshot(people)
        response = await people.post(FINDINGS, finding_body(text=text), project_id=ALPHA)
        assert snapshot(people) == before
    assert response.status_code == 400 and response.json()["error"]["details"] == {"argument": "text"}


async def test_the_fifty_first_open_finding_on_a_story_is_finding_limit_and_writes_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        for number in range(50):
            added = await people.post(FINDINGS, finding_body(text=f"Question {number}", story_id=API), project_id=ALPHA)
            assert added.status_code == 200, added.text
        before = snapshot(people)
        over = await people.post(FINDINGS, finding_body(text="One too many", story_id=API), project_id=ALPHA)
        other = await people.post(FINDINGS, finding_body(text="One too many", story_id=UI), project_id=ALPHA)
        assert snapshot(people)[:2] == before[:2]
    assert over.status_code == 409 and over.json()["error"]["code"] == "finding_limit"
    assert other.status_code == 200  # the limit counts per story


# --- blocking ---------------------------------------------------------------------------------------------------


async def test_a_blocking_finding_blocks_the_claimed_tasks_ends_the_claim_and_shows_in_the_story_list(
    monkeypatch, env, caplog
):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        response = await people.post(
            FINDINGS,
            finding_body(kind="gap", text="The limit is not defined.", story_id=API, claim_token=token, blocking=True),
            project_id=ALPHA,
        )
        finding_id = response.json()["finding"]["finding_id"]
        tasks, story = people.memory.tasks, people.memory.stories[API]
        listed = await people.get(AGENT_ROUTES["list_stories"], project_id=ALPHA, view="mine")
        again = await people.post(
            FINDINGS,
            finding_body(kind="gap", text="The limit is not defined.", story_id=API, claim_token=token, blocking=True),
            project_id=ALPHA,
        )
    assert response.status_code == 200
    assert (response.json()["created"], response.json()["finding"]["blocking"]) == (True, True)
    assert response.json()["blocked"] == API_TASKS
    assert [(tasks[t]["status"], tasks[t]["block_reason"], tasks[t]["blocked_by"]) for t in API_TASKS] == [
        ("blocked", "spec_missing", finding_id)
    ] * 2
    assert (story["claimed_by"], story["claim_token"]) == (None, token)  # the claim ends, the story keeps its token
    row = next(item for item in listed.json()["items"] if item["story_id"] == API)
    assert row["state"] == "blocked"
    # The oldest blocking finding raised on the story; the seeded older one about the same node was raised on STUCK.
    assert row["question"] == "The limit is not defined."
    assert row["findings"] == 3  # the seeded blocking question, Ben's note and this finding
    assert again.json()["created"] is False and again.json()["blocked"] == []
    assert token not in caplog.text and "not defined" not in caplog.text


@pytest.mark.parametrize(("kind", "reason"), [("question", "spec_ambiguous"), ("conflict", "spec_contradiction")])
async def test_the_block_reason_follows_the_kind_of_the_finding(monkeypatch, env, kind, reason):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        response = await people.post(
            FINDINGS, finding_body(kind=kind, story_id=API, claim_token=token, blocking=True), project_id=ALPHA
        )
        assert people.memory.tasks[API_TASKS[0]]["block_reason"] == reason
    assert response.status_code == 200


async def test_blocking_without_a_claim_with_a_wrong_token_or_with_another_members_token_is_claim_mismatch(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        free = await people.post(
            FINDINGS, finding_body(text="Free", story_id=API, claim_token=GUESS, blocking=True), project_id=ALPHA
        )  # nobody holds API: no claim
        await people.hold(API, people.ben)
        bens_token = people.memory.stories[API]["claim_token"]
        before = snapshot(people)
        wrong = await people.post(
            FINDINGS, finding_body(text="Wrong", story_id=API, claim_token=GUESS, blocking=True), project_id=ALPHA
        )
        foreign = await people.post(  # a valid token, but Ben's: the caller is Alice
            FINDINGS,
            finding_body(text="Foreign", story_id=API, claim_token=bens_token, blocking=True),
            project_id=ALPHA,
        )
        assert snapshot(people) == before
    for refused in (free, wrong, foreign):
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "claim_mismatch" and refused.json()["error"]["details"] == {}


async def test_blocking_about_a_node_of_another_story_or_with_a_note_or_without_a_story_writes_nothing(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        people.gap.spec(UI_NODE, "Feature", [])
        people.memory.set_task("TASK::A::UI_SCREEN", implements=[UI_NODE])
        token = await claim(people)
        before = snapshot(people)
        other_story = await people.post(
            FINDINGS, finding_body(about=[UI_NODE], story_id=API, claim_token=token, blocking=True), project_id=ALPHA
        )
        note = await people.post(
            FINDINGS, finding_body(kind="note", story_id=API, claim_token=token, blocking=True), project_id=ALPHA
        )
        no_story = await people.post(FINDINGS, finding_body(claim_token=token, blocking=True), project_id=ALPHA)
        no_token = await people.post(FINDINGS, finding_body(story_id=API, blocking=True), project_id=ALPHA)
        assert snapshot(people) == before
        assert people.memory.stories[API]["claimed_by"] == str(people.alice)  # the claim stands
    assert (other_story.status_code, other_story.json()["error"]["code"]) == (409, "claim_mismatch")
    for refused, argument in ((note, "kind"), (no_story, "story_id"), (no_token, "claim_token")):
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_arguments"
        assert refused.json()["error"]["details"] == {"argument": argument}


async def test_a_plan_persist_refuses_the_blocking_finding_with_plan_applying_and_withdraws_it(monkeypatch, env):
    """A71: the block cannot run while a plan writes the plan, so the finding just written is withdrawn."""

    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        people.memory.plan = {**people.memory.plan, "applying": "run-1"}
        before = snapshot(people)
        response = await people.post(
            FINDINGS, finding_body(story_id=API, claim_token=token, blocking=True), project_id=ALPHA
        )
        assert snapshot(people) == before  # no finding left, no task blocked
    assert response.status_code == 409 and response.json()["error"]["code"] == "plan_applying"


async def test_a_non_blocking_finding_is_written_while_a_plan_persists(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.memory.plan = {**people.memory.plan, "applying": "run-1"}
        response = await people.post(FINDINGS, finding_body(story_id=API), project_id=ALPHA)
    assert response.status_code == 200


async def test_a_blocking_finding_of_the_wrong_kind_never_reaches_the_library_check_as_an_internal_error(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        response = await people.post(FINDINGS, finding_body(kind="rumour"), project_id=ALPHA)
    assert response.status_code == 422  # the enum of the schema


async def test_a_body_out_of_schema_is_422_and_writes_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        before = snapshot(people)
        bodies = [
            {},
            finding_body(about=[]),
            finding_body(about=[f"N::{n}" for n in range(21)]),
            finding_body(text=""),
            finding_body(text="x" * 2001),
            finding_body(refers_to=[{"type": "mail", "value": "a"}]),
            finding_body(story_id=""),
            finding_body(refers_to=[{"type": "commit", "value": "abcdef1"}] * 11),
        ]
        responses = [await people.post(FINDINGS, body, project_id=ALPHA) for body in bodies]
        assert snapshot(people) == before
    assert {r.status_code for r in responses} == {422}


# --- the rule order, the dedupe key and the limit's scope (A103) -----------------------------------------------


@pytest.mark.parametrize("kind", ["note", "question"])
async def test_a_bad_node_on_a_blocking_finding_is_node_not_found_before_the_blocking_rules(monkeypatch, env, kind):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        before = snapshot(people)
        response = await people.post(
            FINDINGS,
            finding_body(kind=kind, about=["REQ::NOPE"], story_id=API, claim_token=token, blocking=True),
            project_id=ALPHA,
        )
        assert snapshot(people) == before
    assert (response.status_code, response.json()["error"]["code"]) == (404, "node_not_found")
    assert response.json()["error"]["details"] == {"spec_ids": ["REQ::NOPE"]}


async def test_the_blocking_guard_answers_claim_mismatch_before_the_finding_limit(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        for number in range(50):
            added = await people.post(FINDINGS, finding_body(text=f"Question {number}", story_id=API), project_id=ALPHA)
            assert added.status_code == 200, added.text
        before = snapshot(people)
        response = await people.post(
            FINDINGS, finding_body(text="Blocked?", story_id=API, claim_token=GUESS, blocking=True), project_id=ALPHA
        )
        assert snapshot(people) == before
    assert (response.status_code, response.json()["error"]["code"]) == (409, "claim_mismatch")


@pytest.mark.parametrize(
    ("first", "again"),
    [
        ({"about": [API_NODE, UI_NODE]}, {"about": [UI_NODE, API_NODE]}),  # about order
        ({"about": [API_NODE]}, {"about": ["FEAT::OLD_API"]}),  # an alias resolves to the same node
        (
            {"text": "Is ABC 12 in cents?"},
            {"text": "Is \uff21\uff22\uff23 \uff11\uff12 in cents?"},
        ),  # NFKC: full-width letters and digits
    ],
)
async def test_the_dedupe_key_ignores_about_order_aliases_and_full_width_forms(monkeypatch, env, first, again):
    async with serve_people(monkeypatch, env) as people:
        people.gap.spec(UI_NODE, "Feature", [])
        people.gap.spec(API_NODE, "Feature", ["FEAT::OLD_API"])
        added = await people.post(FINDINGS, finding_body(**first), project_id=ALPHA)
        before = snapshot(people)
        repeat = await people.post(FINDINGS, finding_body(**{**first, **again}), project_id=ALPHA)
        assert snapshot(people) == before
    assert added.json()["created"] is True and repeat.json()["created"] is False
    assert repeat.json()["finding"]["finding_id"] == added.json()["finding"]["finding_id"]


async def test_the_finding_limit_counts_findings_without_a_story_and_per_caller(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        for number in range(50):
            added = await people.post(FINDINGS, finding_body(text=f"Loose {number}"), project_id=ALPHA)
            assert added.status_code == 200, added.text
        over = await people.post(FINDINGS, finding_body(text="One too many"), project_id=ALPHA)
        on_a_story = await people.post(FINDINGS, finding_body(text="One too many", story_id=API), project_id=ALPHA)
        bens = await people.rig.client.post(
            FINDINGS, params={"project_id": ALPHA}, json=finding_body(text="One too many"), headers=ben_header()
        )
    assert (over.status_code, over.json()["error"]["code"]) == (409, "finding_limit")
    assert on_a_story.status_code == 200 and bens.status_code == 200  # the story's and Ben's own counts


async def test_refers_to_is_never_fetched_on_a_refused_call_either(monkeypatch, env):
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("a refers_to value was fetched")

    async with serve_people(monkeypatch, env) as people:
        await people.get(AGENT_ROUTES["list_areas"], project_id=ALPHA)  # the token check fetches its keys once
        for target, name in (
            (socket.socket, "connect"),
            (socket, "getaddrinfo"),
            (socket, "create_connection"),
            (httpx.AsyncHTTPTransport, "handle_async_request"),
            (httpx.HTTPTransport, "handle_request"),
        ):
            monkeypatch.setattr(target, name, refuse)
        refs = [{"type": "url", "value": "http://169.254.169.254/latest/meta-data"}, {"type": "pr", "value": "7"}]
        unknown_node = await people.post(FINDINGS, finding_body(about=["REQ::NOPE"], refers_to=refs), project_id=ALPHA)
        mismatch = await people.post(
            FINDINGS,
            finding_body(refers_to=refs, story_id=API, claim_token=GUESS, blocking=True),
            project_id=ALPHA,
        )
    assert unknown_node.json()["error"]["code"] == "node_not_found"
    assert mismatch.json()["error"]["code"] == "claim_mismatch"
