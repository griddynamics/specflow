"""``POST /task-progress`` and ``POST /story-completion`` (``report_task``, ``complete_story``; FR-MCP-WRITE-05..10,
AC9-AC14, tools.md "report_task", "complete_story", "Repeats"): progress that never changes a status, the one path to
``completed``, the commit and URL checks, the claim checks and what a refused call leaves behind, over the real
``PlanService`` (``people_support``).

Sequence of a story's end::

    agent -> route: POST /task-progress (task, token, done | skipped + note | todo)   status stays in_progress
    agent -> route: POST /story-completion (story, token, merge_commit, pr_url, done?)
    route -> PlanService: complete_story: tasks in ``done`` -> completed (commit, completed_by), the rest -> pending
    route -> agent: completed, released, now_available, spec_changed; a repeat answers the same, released []
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from agent_contracts import AGENT_ROUTES, StoryCompletion, TaskReport
from people_support import API, UI, PeopleRig, serve_people
from specflow_api_agent_support import ALPHA, agent_env, events

CLAIM, PROGRESS, COMPLETE = (AGENT_ROUTES[name] for name in ("claim_story", "report_task", "complete_story"))
RELEASE = AGENT_ROUTES["release_story"]
CONTRACT, LOGIC = "TASK::A::API_CONTRACT", "TASK::A::API_LOGIC"
COMMIT: Final = "a" * 40
PR: Final = "https://git.example.test/org/repo/pull/7"
GUESS: Final = "nope"  # a claim token nobody holds


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


def report(task_id: str, token: str, progress: str = "done", **more: Any) -> dict[str, Any]:
    return {"task_id": task_id, "claim_token": token, "progress": progress, **more}


def complete(token: str, story_id: str = API, **more: Any) -> dict[str, Any]:
    return {"story_id": story_id, "claim_token": token, "merge_commit": COMMIT, "pr_url": PR, **more}


async def claim(people: PeopleRig, story_id: str = API) -> str:
    return (await people.post(CLAIM, {"story_id": story_id}, project_id=ALPHA)).json()["claim_token"]


def snapshot(people: PeopleRig) -> tuple[Any, ...]:
    return (
        {key: dict(row) for key, row in people.memory.stories.items()},
        {key: dict(row) for key, row in people.memory.tasks.items()},
    )


# --- report_task ------------------------------------------------------------------------------------------------


async def test_report_task_records_progress_and_the_note_and_leaves_the_status_in_progress(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        statuses = []
        answers = []
        for progress, note in (("done", "Built; tests green"), ("todo", None), ("skipped", "Not needed here")):
            answers.append(await people.post(PROGRESS, report(CONTRACT, token, progress, note=note), project_id=ALPHA))
            statuses.append(people.memory.tasks[CONTRACT]["status"])
        stored = people.memory.tasks[CONTRACT]
        story = people.memory.stories[API]
    assert [a.status_code for a in answers] == [200, 200, 200] and set(statuses) == {"in_progress"}
    assert [a.json()["task"]["progress"] for a in answers] == ["done", "todo", "skipped"]
    assert (stored["progress"], stored["note"], story["claimed_by"]) == (
        "skipped",
        "Not needed here",
        str(people.alice),
    )
    task = answers[0].json()["task"]
    assert TaskReport.model_validate(answers[0].json())
    assert (answers[0].json()["project_id"], task["task_id"], task["status"], task["story"]["story_id"]) == (
        ALPHA,
        CONTRACT,
        "in_progress",
        API,
    )
    assert set(task) == {
        "task_id",
        "title",
        "phase",
        "status",
        "complexity",
        "area",
        "story",
        "ready",
        "waits_for",
        "planned_by",
        "progress",
    }
    assert "Built; tests" not in caplog.text and token not in caplog.text and "Not needed" not in caplog.text
    assert {(e.route, e.outcome) for e in events(caplog, "api.agent.written") if e.route == "task-progress"} == {
        ("task-progress", "ok")
    }


async def test_the_same_progress_again_answers_the_same_and_writes_nothing(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        first = await people.post(PROGRESS, report(CONTRACT, token, note="ok"), project_id=ALPHA)
        statements = len(people.memory.statements)
        again = await people.post(PROGRESS, report(CONTRACT, token, note="ok"), project_id=ALPHA)
        assert not any(s.startswith("plan_set") for s in people.memory.statements[statements:])
    assert again.status_code == 200 and again.json() == first.json()
    assert [e.repeat for e in events(caplog, "api.agent.written") if e.route == "task-progress"] == [False, True]


async def test_skipped_needs_a_note(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        before = snapshot(people)
        refused = [
            await people.post(PROGRESS, report(CONTRACT, token, "skipped", **extra), project_id=ALPHA)
            for extra in ({}, {"note": None}, {"note": ""}, {"note": "  \n"})
        ]
        assert snapshot(people) == before
        allowed = await people.post(PROGRESS, report(CONTRACT, token, "skipped", note="Not needed"), project_id=ALPHA)
    assert {r.status_code for r in refused} == {400}
    assert all(
        r.json()["error"]
        == {
            "code": "invalid_arguments",
            "message": "The arguments do not fit together.",
            "details": {"argument": "note"},
        }
        for r in refused
    )
    assert allowed.status_code == 200


async def test_report_task_refuses_a_wrong_token_a_free_task_another_members_claim_and_a_completed_task(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        mine = await claim(people, "STORY::A::X")
        await people.hold(API, people.ben)
        bens_token = people.memory.stories[API]["claim_token"]
        before = snapshot(people)
        wrong = await people.post(PROGRESS, report("TASK::A::X_CONTRACT", GUESS), project_id=ALPHA)
        other_story = await people.post(PROGRESS, report(CONTRACT, mine), project_id=ALPHA)  # my token, Ben's task
        foreign = await people.post(PROGRESS, report(CONTRACT, bens_token), project_id=ALPHA)  # Ben's valid token
        free = await people.post(PROGRESS, report("TASK::A::UI_SCREEN", mine), project_id=ALPHA)
        completed = await people.post(PROGRESS, report("TASK::P::TC", mine), project_id=ALPHA)
        assert snapshot(people) == before
    for refused in (wrong, other_story, foreign, free, completed):
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "claim_mismatch" and refused.json()["error"]["details"] == {}


async def test_report_task_of_an_unknown_task_without_a_plan_or_during_a_plan_persist(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        unknown = await people.post(PROGRESS, report("TASK::NOPE", token), project_id=ALPHA)
        people.memory.plan = {**people.memory.plan, "applying": "run-1"}
        before = snapshot(people)
        applying = await people.post(PROGRESS, report(CONTRACT, token), project_id=ALPHA)
        assert snapshot(people) == before
        people.memory.plan = None
        no_plan = await people.post(PROGRESS, report(CONTRACT, token), project_id=ALPHA)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "task_not_found")
    assert (applying.status_code, applying.json()["error"]["code"]) == (409, "plan_applying")  # A71
    assert (no_plan.status_code, no_plan.json()["error"]["code"]) == (404, "no_plan")


async def test_a_report_body_out_of_schema_is_422_and_writes_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        before = snapshot(people)
        bodies = [
            {},
            report(CONTRACT, token, "finished"),
            report(CONTRACT, token, note="n" * 501),
            report("", token),
            {"task_id": CONTRACT, "progress": "done"},
        ]
        responses = [await people.post(PROGRESS, body, project_id=ALPHA) for body in bodies]
        assert snapshot(people) == before
    assert {r.status_code for r in responses} == {422}


# --- complete_story ---------------------------------------------------------------------------------------------


async def test_complete_story_completes_the_tasks_with_the_commit_ends_the_claim_and_lists_what_became_ready(
    monkeypatch, env, caplog
):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        await people.post(PROGRESS, report(CONTRACT, token), project_id=ALPHA)
        assert people.memory.tasks[CONTRACT]["status"] == "in_progress"  # report_task never completes
        response = await people.post(COMPLETE, complete(token, merge_commit=COMMIT.upper()), project_id=ALPHA)
        tasks, story = people.memory.tasks, people.memory.stories[API]
        done = await people.get(AGENT_ROUTES["list_stories"], project_id=ALPHA, view="done")
    assert response.status_code == 200 and StoryCompletion.model_validate(response.json())
    assert response.json() == {
        "project_id": ALPHA,
        "story_id": API,
        "completed": [CONTRACT, LOGIC],
        "released": [],
        "now_available": [UI],
        "spec_changed": [],
    }
    assert [(tasks[t]["status"], tasks[t]["commit"], tasks[t]["completed_by"]) for t in (CONTRACT, LOGIC)] == [
        ("completed", COMMIT, str(people.alice))
    ] * 2
    assert (story["claimed_by"], story["claim_token"], story["pr_url"]) == (None, token, PR)
    row = next(item for item in done.json()["items"] if item["story_id"] == API)
    assert (row["pr_url"], row["merge_commit"], row["completed_by"]["you"]) == (PR, COMMIT, True)
    written = [e for e in events(caplog, "api.agent.written") if e.route == "story-completion"]
    assert [(e.outcome, e.repeat, e.story_id) for e in written] == [("ok", False, API)]
    assert token not in caplog.text and PR not in caplog.text and COMMIT not in caplog.text


async def test_spec_changed_lists_the_implemented_nodes_a_build_wrote_after_the_plan_consumed_its_builds(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        people.specs.builds["FEAT::API"] = "b2"  # the plan consumed b1 only
        token = await claim(people)
        response = await people.post(COMPLETE, complete(token), project_id=ALPHA)
    assert response.json()["spec_changed"] == ["FEAT::API"]


async def test_a_64_character_commit_and_a_pull_request_left_out_are_accepted(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        response = await people.post(
            COMPLETE, {"story_id": API, "claim_token": token, "merge_commit": "b" * 64}, project_id=ALPHA
        )
        story = people.memory.stories[API]
    assert response.status_code == 200 and story.get("pr_url") is None
    assert people.memory.tasks[CONTRACT]["commit"] == "b" * 64


async def test_done_completes_those_tasks_and_the_rest_of_the_claim_goes_back_to_pending(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        response = await people.post(COMPLETE, complete(token, done=[CONTRACT]), project_id=ALPHA)
        tasks = people.memory.tasks
    assert response.json()["completed"] == [CONTRACT] and response.json()["released"] == [LOGIC]
    assert (tasks[CONTRACT]["status"], tasks[LOGIC]["status"]) == ("completed", "pending")


async def test_a_skipped_task_completes_with_its_note(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        await people.post(
            PROGRESS, report(LOGIC, token, "skipped", note="Covered by the contract task"), project_id=ALPHA
        )
        response = await people.post(COMPLETE, complete(token), project_id=ALPHA)
        skipped = people.memory.tasks[LOGIC]
    assert response.status_code == 200 and LOGIC in response.json()["completed"]
    assert (skipped["status"], skipped["note"]) == ("completed", "Covered by the contract task")


async def test_complete_story_again_with_the_same_commit_answers_the_completed_tasks_and_writes_nothing(
    monkeypatch, env, caplog
):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        first = await people.post(COMPLETE, complete(token), project_id=ALPHA)
        before, statements = snapshot(people), len(people.memory.statements)
        again = await people.post(COMPLETE, complete(token), project_id=ALPHA)
        other_commit = await people.post(COMPLETE, complete(token, merge_commit="c" * 40), project_id=ALPHA)
        assert snapshot(people) == before
        assert not any(s.startswith("plan_set") for s in people.memory.statements[statements:])
    assert again.status_code == 200
    assert again.json() == {**first.json(), "released": [], "spec_changed": []}
    assert (other_commit.status_code, other_commit.json()["error"]["code"]) == (409, "claim_mismatch")
    assert [e.repeat for e in events(caplog, "api.agent.written") if e.route == "story-completion"] == [
        False,
        True,
        False,
    ]


@pytest.mark.parametrize("commit", ["", "abc", "a" * 39, "a" * 41, "g" * 40, "a" * 63, "a" * 65, " " + "a" * 39])
async def test_a_merge_commit_that_is_not_40_or_64_hex_is_invalid_commit_and_writes_nothing(monkeypatch, env, commit):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        before = snapshot(people)
        response = await people.post(COMPLETE, complete(token, merge_commit=commit), project_id=ALPHA)
        assert snapshot(people) == before
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_commit"
    assert response.json()["error"]["details"] == {"argument": "merge_commit"}


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/p/1",
        "https://user:pw@example.com/p/1",
        "https:///p/1",
        "not a url",
        "https://x.test/" + "a" * 2000,
        "http://[abc/x",  # urlsplit raises: still the argument's error, not internal_error
        "http://[::1",
    ],
)
async def test_a_pr_url_that_breaks_its_form_is_invalid_arguments_naming_pr_url(monkeypatch, env, url):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        before = snapshot(people)
        response = await people.post(COMPLETE, complete(token, pr_url=url), project_id=ALPHA)
        assert snapshot(people) == before
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_arguments"
    assert response.json()["error"]["details"] == {"argument": "pr_url"}
    assert "pw@" not in response.text


async def test_a_done_id_outside_the_claim_is_invalid_arguments_naming_done(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        before = snapshot(people)
        response = await people.post(COMPLETE, complete(token, done=[CONTRACT, "TASK::A::UI_SCREEN"]), project_id=ALPHA)
        assert snapshot(people) == before
    assert response.status_code == 400 and response.json()["error"]["details"] == {"argument": "done"}


async def test_complete_story_refuses_a_wrong_token_a_free_story_and_another_members_claim(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.ben)
        bens_token = people.memory.stories[API]["claim_token"]
        mine = await claim(people, "STORY::A::X")
        before = snapshot(people)
        wrong = await people.post(COMPLETE, complete(GUESS), project_id=ALPHA)
        foreign = await people.post(COMPLETE, complete(bens_token), project_id=ALPHA)  # Ben's token, Ben's story
        mismatched = await people.post(COMPLETE, complete(mine), project_id=ALPHA)  # my token, Ben's story
        free = await people.post(COMPLETE, complete(mine, UI), project_id=ALPHA)
        assert snapshot(people) == before
    for refused in (wrong, foreign, mismatched, free):
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "claim_mismatch" and refused.json()["error"]["details"] == {}


async def test_complete_story_of_an_unknown_story_without_a_plan_or_during_a_plan_persist(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        unknown = await people.post(COMPLETE, complete(token, "STORY::NOPE"), project_id=ALPHA)
        people.memory.plan = {**people.memory.plan, "applying": "run-1"}
        before = snapshot(people)
        applying = await people.post(COMPLETE, complete(token), project_id=ALPHA)
        assert snapshot(people) == before
        people.memory.plan = None
        no_plan = await people.post(COMPLETE, complete(token), project_id=ALPHA)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "story_not_found")
    assert (applying.status_code, applying.json()["error"]["code"]) == (409, "plan_applying")  # A71
    assert (no_plan.status_code, no_plan.json()["error"]["code"]) == (404, "no_plan")


async def test_a_completion_body_out_of_schema_is_422_and_writes_nothing(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        before = snapshot(people)
        bodies = [
            {},
            {"story_id": API, "claim_token": token},
            complete(token, done=[f"TASK::{n}" for n in range(201)]),
            complete(""),
        ]
        responses = [await people.post(COMPLETE, body, project_id=ALPHA) for body in bodies]
        assert snapshot(people) == before
    assert {r.status_code for r in responses} == {422}


# --- the holder, not only the token (A107: checked by the library under the story lock) ---------------------------


async def test_a_completion_repeated_by_another_member_with_the_last_token_is_claim_mismatch(monkeypatch, env):
    # Ben holds and completes API; Alice then presents Ben's last token and the same commit: not her completion.
    async with serve_people(monkeypatch, env) as people:
        token = await people.hold(API, people.ben)
        await people.plan.complete_story(
            ALPHA, API, claim_token=token, completed_by=str(people.ben), merge_commit=COMMIT
        )
        before, statements = snapshot(people), len(people.memory.statements)
        response = await people.post(COMPLETE, complete(token), project_id=ALPHA)
        assert snapshot(people) == before
        assert not any(s.startswith("plan_set") for s in people.memory.statements[statements:])
    assert (response.status_code, response.json()["error"]["code"]) == (409, "claim_mismatch")


async def test_a_completion_repeated_with_another_done_answers_the_first_completion(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        token = await claim(people)
        first = await people.post(COMPLETE, complete(token, done=[CONTRACT]), project_id=ALPHA)
        before = snapshot(people)
        again = await people.post(COMPLETE, complete(token, done=[CONTRACT, LOGIC]), project_id=ALPHA)
        assert snapshot(people) == before
    assert again.status_code == 200 and again.json()["completed"] == first.json()["completed"] == [CONTRACT]
    assert again.json()["released"] == []


@pytest.mark.parametrize("reclaimer", ["ben", "alice"])
async def test_a_stale_token_after_a_release_and_a_new_claim_is_claim_mismatch_on_every_write(
    monkeypatch, env, reclaimer
):
    # Alice claims and releases API; Ben (or Alice herself) claims it anew; Alice's old token matches no write.
    async with serve_people(monkeypatch, env) as people:
        old = await claim(people)
        await people.post(RELEASE, {"story_id": API, "claim_token": old}, project_id=ALPHA)
        if reclaimer == "ben":
            await people.hold(API, people.ben)
        else:
            assert await claim(people) != old
        before = snapshot(people)
        responses = [
            await people.post(RELEASE, {"story_id": API, "claim_token": old}, project_id=ALPHA),
            await people.post(COMPLETE, complete(old), project_id=ALPHA),
            await people.post(PROGRESS, report(CONTRACT, old), project_id=ALPHA),
        ]
        assert snapshot(people) == before
    for response in responses:
        assert (response.status_code, response.json()["error"]["code"]) == (409, "claim_mismatch")
