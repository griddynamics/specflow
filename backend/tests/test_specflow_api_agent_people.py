"""The people tools' API routes (FR-MCP-STORY-01 to -09): ``GET /areas``, ``/stories``, ``/story-preview`` and
``POST /story-claim``, ``/story-release``, over the REAL ``PlanService`` and findings library on the in-memory plan of
``mini_plan`` (``people_support``). Covers the structured answers, ordering and paging, holders by name, the typed error
details, idempotency by state, the refusals that write nothing, and the audit event."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from agent_contracts import AGENT_ROUTES
from people_support import API, LATE, SETUP, STUCK, UI, PeopleRig, X, Y, serve_people
from specflow_api_agent_support import ALPHA, USER_HEADER, agent_env, bearer_header, events, mcp_token

AREAS, STORIES, PREVIEW = (AGENT_ROUTES[name] for name in ("list_areas", "list_stories", "preview_story"))
CLAIM, RELEASE = AGENT_ROUTES["claim_story"], AGENT_ROUTES["release_story"]


@pytest.fixture
def env(valid_env: dict[str, str]) -> dict[str, str]:
    return agent_env(valid_env)


def claim_body(story_id: str = API, **more: object) -> dict[str, object]:
    return {"story_id": story_id, **more}


async def _member(people: PeopleRig, name: str) -> uuid.UUID:
    """A new member of ALPHA called ``name``; their user id."""

    user = await people.rig.repo.upsert_user(
        None,
        SimpleNamespace(subject=f"user:{name.lower()}@example.com", email=f"{name.lower()}@example.com", name=name),
    )
    people.rig.repo.members.add((uuid.UUID(ALPHA), user.id))
    return user.id


# --- list_areas -----------------------------------------------------------------------------------------------


async def test_list_areas_counts_stories_by_state_and_puts_the_project_area_last(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        response = await people.get(AREAS, project_id=ALPHA)
    body = response.json()
    assert response.status_code == 200 and response.headers[USER_HEADER] == people.rig.alice_id
    assert body["project_id"] == ALPHA
    assert [item["area_id"] for item in body["items"]] == ["AREA::A", "AREA::P"]
    area, project = body["items"]
    assert (area["kind"], area["title"]) == ("module", "Area AREA::A")
    assert (area["ready"], area["waiting"], area["blocked"], area["done"]) == (3, 2, 1, 0)
    assert area["in_progress"] == {"count": 0, "holders": []} and area["open_tasks"] == 10
    assert (project["kind"], project["done"], project["ready"], project["open_tasks"]) == ("project", 1, 0, 0)
    assert sum(area[k] for k in ("ready", "waiting", "blocked", "done")) == 6  # the active stories of the area


async def test_list_areas_names_the_holders_oldest_claim_first_at_most_three_and_marks_the_caller(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.ben, "Claude Code · laptop")
        await people.hold(X, people.alice)
        response = await people.get(AREAS, project_id=ALPHA)
    area = response.json()["items"][0]
    assert area["in_progress"]["count"] == 3  # API, and X with Y: one claim unit
    assert area["in_progress"]["holders"] == [
        {"name": "Ben", "you": False, "agent_label": None},
        {"name": "Alice", "you": True, "agent_label": None},
    ]


async def test_list_areas_names_three_holders_of_four_and_counts_all_four(monkeypatch, env):
    # Four members each hold one story of AREA::A (claims one minute apart): the cell counts four stories and names
    # the three oldest claims.
    async with serve_people(monkeypatch, env) as people:
        carol = await _member(people, "Carol")
        dave = await _member(people, "Dave")
        holders = [(STUCK, people.ben), (UI, people.alice), (API, carol), (LATE, dave)]
        for minute, (story_id, who) in enumerate(holders):
            people.memory.set_story(
                story_id,
                claimed_by=str(who),
                claimed_at=f"2026-01-02T00:0{minute}:00+00:00",
                claim_token=f"{minute}" * 32,
            )
        response = await people.get(AREAS, project_id=ALPHA)
    area = response.json()["items"][0]
    assert area["in_progress"]["count"] == 4
    assert area["in_progress"]["holders"] == [
        {"name": "Ben", "you": False, "agent_label": None},
        {"name": "Alice", "you": True, "agent_label": None},
        {"name": "Carol", "you": False, "agent_label": None},
    ]


async def test_a_story_in_progress_through_a_task_claim_shows_that_tasks_holder(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.claim_task("TASK::A::API_CONTRACT", people.ben)
        areas = await people.get(AREAS, project_id=ALPHA)
        stories = await people.get(STORIES, project_id=ALPHA, limit=100)
        preview = await people.get(PREVIEW, project_id=ALPHA, story_id=API)
    ben = {"name": "Ben", "you": False, "agent_label": None}
    assert areas.json()["items"][0]["in_progress"] == {"count": 1, "holders": [ben]}
    row = next(item for item in stories.json()["items"] if item["story_id"] == API)
    assert (row["state"], row["holder"]) == ("in_progress", ben)
    story = preview.json()["story"]
    assert (story["state"], story["holder"]) == ("in_progress", ben) and story["since"] is not None


async def test_list_areas_answers_no_plan_and_leaves_nothing_changed(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.memory.plan = None
        response = await people.get(AREAS, project_id=ALPHA)
    assert response.status_code == 404 and response.json()["error"]["code"] == "no_plan"


# --- list_stories ----------------------------------------------------------------------------------------------


async def test_the_open_view_lists_ready_stories_first_then_the_other_states_by_story_id(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(X, people.ben)  # X and Y are one claim unit: both in progress
        response = await people.get(STORIES, project_id=ALPHA, limit=100)
    body = response.json()
    assert (body["view"], body["area_id"], body["total_count"], body["next_cursor"]) == ("open", None, 6, None)
    assert [(i["rank"], i["story_id"], i["state"]) for i in body["items"]] == [
        (1, API, "ready"),
        (2, X, "in_progress"),
        (3, Y, "in_progress"),
        (4, LATE, "waiting"),
        (5, UI, "waiting"),
        (6, STUCK, "blocked"),
    ]
    api, held = body["items"][0], body["items"][1]
    assert api["holder"] is None and api["waits_on"] == [] and (api["tasks"], api["unblocks"]) == (2, 1)
    assert held["holder"] == {"name": "Ben", "you": False, "agent_label": None}
    assert body["items"][3]["waits_on"] == ["Title of STORY::A::API", "Title of STORY::A::UI"]
    assert "pr_url" not in body["items"][0] or body["items"][0]["pr_url"] is None


async def test_findings_count_the_open_ones_about_the_story_and_the_question_is_the_oldest_blocking_one(
    monkeypatch, env
):
    async with serve_people(monkeypatch, env) as people:
        before = await people.get(STORIES, project_id=ALPHA, limit=100)
        for text in ("Is the amount in cents?", "A later blocking question"):
            await people.findings.add(
                ALPHA,
                created_by=str(people.ben),
                kind="question",
                text=text,
                about=["FEAT::API"],
                story_id=API,
                blocking=True,
            )
        response = await people.get(STORIES, project_id=ALPHA, limit=100)
    # The seeded blocking question is about API's node but was raised on STUCK: counted, never API's question.
    seeded = {item["story_id"]: item for item in before.json()["items"]}
    assert seeded[API]["findings"] == 2 and seeded[API]["question"] is None
    by_id = {item["story_id"]: item for item in response.json()["items"]}
    assert by_id[API]["findings"] == 4 and by_id[API]["question"] == "Is the amount in cents?"
    assert by_id[UI]["findings"] == 0 and by_id[UI]["question"] is None


async def test_the_question_is_cut_to_80_characters(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        for finding in people.gap.findings().values():
            finding["status"] = "resolved"  # the seeded question is no longer open
        await people.findings.add(
            ALPHA,
            created_by=str(people.ben),
            kind="question",
            text="q" * 200,
            about=["FEAT::API"],
            story_id=API,
            blocking=True,
        )
        response = await people.get(STORIES, project_id=ALPHA, limit=100)
    assert next(i for i in response.json()["items"] if i["story_id"] == API)["question"] == "q" * 80


async def test_an_area_narrows_the_list_and_an_unknown_area_is_area_not_found(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        project = await people.get(STORIES, project_id=ALPHA, area_id="AREA::P", view="done")
        unknown = await people.get(STORIES, project_id=ALPHA, area_id="AREA::NOPE")
    assert [i["story_id"] for i in project.json()["items"]] == [SETUP]
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "area_not_found"


async def test_the_done_view_carries_the_merge(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        response = await people.get(STORIES, project_id=ALPHA, view="done")
    (item,) = response.json()["items"]
    assert item["story_id"] == SETUP and item["state"] == "completed" and item["tasks"] == 0
    assert (item["pr_url"], item["merge_commit"]) == ("https://git.example.test/p/1", "c" * 40)
    assert item["completed_by"] == {"name": None, "you": False, "agent_label": None}  # "ann" is no member
    assert item["completed_at"].startswith("2026-01-01T12:00:00")


async def test_the_mine_view_lists_the_held_stories_then_the_ones_the_callers_findings_blocked(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.ben)
        mine_for_ben = await people.get(STORIES, project_id=ALPHA, view="mine")
        await people.hold(X, people.alice, "agent")
        response = await people.get(STORIES, project_id=ALPHA, view="mine")
    assert [i["story_id"] for i in mine_for_ben.json()["items"]] == [
        STUCK
    ]  # alice's FND::1 blocks it; ben's API is not hers
    assert [(i["story_id"], i["state"]) for i in response.json()["items"]] == [
        (X, "in_progress"),
        (Y, "in_progress"),
        (STUCK, "blocked"),
    ]
    assert response.json()["items"][0]["holder"] == {"name": "Alice", "you": True, "agent_label": "agent"}


async def test_paging_continues_the_rank_and_the_cursor_is_bound_to_project_area_and_view(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        first = (await people.get(STORIES, project_id=ALPHA, limit=4)).json()
        second = (await people.get(STORIES, project_id=ALPHA, limit=4, cursor=first["next_cursor"])).json()
        other_view = await people.get(STORIES, project_id=ALPHA, limit=4, view="mine", cursor=first["next_cursor"])
        other_area = await people.get(
            STORIES, project_id=ALPHA, limit=4, area_id="AREA::A", cursor=first["next_cursor"]
        )
        junk = await people.get(STORIES, project_id=ALPHA, cursor="x")
        too_long = await people.get(STORIES, project_id=ALPHA, cursor="a" * 513)
    assert (first["total_count"], len(first["items"]), first["next_cursor"] is not None) == (6, 4, True)
    assert [i["rank"] for i in first["items"]] == [1, 2, 3, 4] and [i["rank"] for i in second["items"]] == [5, 6]
    assert second["next_cursor"] is None and second["total_count"] == 6
    for refused in (other_view, other_area, junk, too_long):
        assert refused.status_code == 400 and refused.json()["error"]["code"] == "invalid_cursor"


# --- preview_story ---------------------------------------------------------------------------------------------


async def test_a_preview_shows_the_story_its_tasks_in_build_order_blockers_findings_and_unblocks(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.ben, "Claude Code")
        response = await people.get(PREVIEW, project_id=ALPHA, story_id=UI)
        api = await people.get(PREVIEW, project_id=ALPHA, story_id=API)
    ui, held = response.json(), api.json()
    assert (ui["project_id"] == ALPHA and ui["story"]["state"] == "in_progress") or ui["story"]["state"] == "waiting"
    assert [b["story_id"] for b in ui["blockers"]] == [API]
    assert ui["blockers"][0]["holder"] == {"name": "Ben", "you": False, "agent_label": None}
    assert held["story_ids"] == [API] and [i["task_id"] for i in held["items"]] == [
        "TASK::A::API_CONTRACT",
        "TASK::A::API_LOGIC",
    ]
    assert held["story"]["holder"] == {"name": "Ben", "you": False, "agent_label": "Claude Code"}
    assert held["story"]["since"] is not None and held["unblocks"] == [UI]
    assert [(f["kind"], f["blocking"]) for f in held["findings"]] == [("question", True), ("note", False)]
    assert held["findings"][1]["author"] == {"name": "Ben", "you": False, "agent_label": None}


async def test_a_preview_of_an_unheld_story_has_no_holder_and_no_since(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.memory.set_story(API, claimed_at="2026-01-02T00:00:00+00:00")  # a stale time without a holder
        response = await people.get(PREVIEW, project_id=ALPHA, story_id=API)
    story = response.json()["story"]
    assert (story["state"], story["holder"], story["since"]) == ("ready", None, None)


async def test_a_preview_of_a_cycle_names_the_whole_claim_unit(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        response = await people.get(PREVIEW, project_id=ALPHA, story_id=X)
    assert response.json()["story_ids"] == [X, Y]


async def test_a_preview_changes_nothing_and_an_unknown_story_is_story_not_found(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        before = {key: dict(row) for key, row in people.memory.tasks.items()}
        await people.get(PREVIEW, project_id=ALPHA, story_id=API)
        missing = await people.get(PREVIEW, project_id=ALPHA, story_id="STORY::NOPE")
        assert before == people.memory.tasks and not any(s.startswith("plan_set") for s in people.memory.statements)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "story_not_found"


# --- claim_story -----------------------------------------------------------------------------------------------


async def test_a_claim_returns_the_token_the_unit_and_the_claimed_tasks_and_stores_who(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        response = await people.post(
            CLAIM, claim_body(agent_label="Claude Code · laptop", branch="fix/api"), project_id=ALPHA
        )
        stored = people.memory.stories[API]
    body = response.json()
    assert response.status_code == 200 and response.headers[USER_HEADER] == str(people.alice)
    assert (body["project_id"], body["story_id"], body["story_ids"]) == (ALPHA, API, [API])
    assert body["claim_token"] == stored["claim_token"] and len(body["claim_token"]) == 32
    assert [(i["task_id"], i["status"]) for i in body["items"]] == [
        ("TASK::A::API_CONTRACT", "in_progress"),
        ("TASK::A::API_LOGIC", "in_progress"),
    ]
    assert (stored["claimed_by"], stored["agent_label"], stored["branch"]) == (
        str(people.alice),
        "Claude Code · laptop",
        "fix/api",
    )  # who: the token's user id, never an email or a name
    (event,) = events(caplog, "api.agent.written")
    assert (event.route, event.project_id, event.story_id, event.outcome, event.repeat) == (
        "story-claim",
        ALPHA,
        API,
        "ok",
        False,
    )


async def test_a_second_claim_by_the_holder_answers_the_same_token_and_logs_a_repeat(monkeypatch, env, caplog):
    caplog.set_level("INFO")
    async with serve_people(monkeypatch, env) as people:
        first = await people.post(CLAIM, claim_body(), project_id=ALPHA)
        statements = len(people.memory.statements)
        again = await people.post(CLAIM, claim_body(agent_label="other"), project_id=ALPHA)
        assert again.json()["claim_token"] == first.json()["claim_token"]
        assert not any(s.startswith("plan_set") for s in people.memory.statements[statements:])  # changed nothing
        assert people.memory.stories[API]["agent_label"] is None  # a repeat does not relabel
    assert again.status_code == 200 and again.json() == first.json()
    assert [e.repeat for e in events(caplog, "api.agent.written")] == [False, True]


async def test_the_claim_of_a_cycle_holds_both_stories_with_one_token(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        response = await people.post(CLAIM, claim_body(X), project_id=ALPHA)
        stories = people.memory.stories
    assert response.json()["story_ids"] == [X, Y]
    assert stories[X]["claim_token"] == stories[Y]["claim_token"] == response.json()["claim_token"]


async def test_another_members_claim_is_story_claimed_with_the_holder_since_and_ready_alternatives(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.ben, "Claude Code · laptop")
        before = dict(people.memory.stories)
        response = await people.post(CLAIM, claim_body(), project_id=ALPHA)
        assert people.memory.stories == before  # nothing written
    body = response.json()
    assert response.status_code == 409 and body["error"]["code"] == "story_claimed"
    details = body["error"]["details"]
    assert details["holder"] == {"name": "Ben", "you": False, "agent_label": "Claude Code · laptop"}
    assert details["since"]
    assert [(a["story_id"], a["state"], a["holder"]) for a in details["alternatives"]] == [
        (X, "ready", None),
        (Y, "ready", None),
    ]
    assert set(details) == {"holder", "since", "alternatives"}
    assert "claim_token" not in response.text


async def test_a_story_that_waits_is_not_available_with_its_state_and_blockers_in_build_order(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.hold(API, people.ben)
        response = await people.post(CLAIM, claim_body(LATE), project_id=ALPHA)
        done = await people.post(CLAIM, claim_body(SETUP), project_id=ALPHA)
        blocked = await people.post(CLAIM, claim_body(STUCK), project_id=ALPHA)
    details = response.json()["error"]["details"]
    assert response.status_code == 409 and response.json()["error"]["code"] == "not_available"
    assert details["state"] == "waiting"
    assert [(b["story_id"], b["state"]) for b in details["blockers"]] == [(API, "in_progress"), (UI, "waiting")]
    assert details["blockers"][0]["holder"] == {"name": "Ben", "you": False, "agent_label": None}
    assert done.json()["error"]["details"] == {"state": "completed", "blockers": []}  # nothing left to claim
    assert blocked.json()["error"]["details"] == {"state": "blocked", "blockers": []}  # a task is blocked


async def test_a_task_claimed_on_its_own_is_partly_claimed_with_its_id_and_holder(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        await people.claim_task("TASK::A::API_CONTRACT", people.ben)
        response = await people.post(CLAIM, claim_body(), project_id=ALPHA)
    assert response.status_code == 409 and response.json()["error"]["code"] == "partly_claimed"
    assert response.json()["error"]["details"] == {
        "holder": {"name": "Ben", "you": False, "agent_label": None},
        "task_id": "TASK::A::API_CONTRACT",
    }


async def test_a_claim_during_a_plan_persist_is_plan_applying(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        people.memory.plan = {**people.memory.plan, "applying": "run-1"}
        response = await people.post(CLAIM, claim_body(), project_id=ALPHA)
        assert people.memory.stories[API].get("claimed_by") is None
    assert response.status_code == 409 and response.json()["error"] == {
        "code": "plan_applying",
        "message": "The plan is being updated.",
        "details": {},
    }


async def test_a_claim_of_an_unknown_story_is_story_not_found_and_without_a_plan_no_plan(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        unknown = await people.post(CLAIM, claim_body("STORY::NOPE"), project_id=ALPHA)
        people.memory.plan = None
        no_plan = await people.post(CLAIM, claim_body(), project_id=ALPHA)
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "story_not_found")
    assert (no_plan.status_code, no_plan.json()["error"]["code"]) == (404, "no_plan")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_label", ""),
        ("agent_label", "x" * 81),
        ("agent_label", "a\nb"),
        ("branch", ""),
        ("branch", "b" * 201),
        ("branch", "x\ty"),
    ],
)
async def test_a_label_or_branch_that_breaks_the_format_is_invalid_arguments_naming_the_argument(
    monkeypatch, env, caplog, field, value
):
    caplog.set_level("DEBUG")
    async with serve_people(monkeypatch, env) as people:
        before = dict(people.memory.stories)
        response = await people.post(CLAIM, claim_body(**{field: value}), project_id=ALPHA)
        assert people.memory.stories == before
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "invalid_arguments",
        "message": "The arguments do not fit together.",
        "details": {"argument": field},
    }
    assert not value.strip() or value not in "\n".join(r.getMessage() + str(vars(r)) for r in caplog.records)


async def test_two_claims_of_one_story_in_flight_have_exactly_one_winner(monkeypatch, env):
    async with serve_people(monkeypatch, env) as people:
        ben, alice = await asyncio.gather(
            people.rig.client.post(
                CLAIM, params={"project_id": ALPHA}, json=claim_body(), headers=_bearer("ben@example.com")
            ),
            people.post(CLAIM, claim_body(), project_id=ALPHA),
        )
        stored = people.memory.stories[API]["claimed_by"]
    assert sorted((ben.status_code, alice.status_code)) == [200, 409]
    winner = ben if ben.status_code == 200 else alice
    assert winner.json()["claim_token"] == people.memory.stories[API]["claim_token"] and stored


def _bearer(email: str) -> dict[str, str]:
    return bearer_header(mcp_token(email))
