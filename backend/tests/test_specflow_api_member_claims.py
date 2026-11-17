"""Remove and leave free the person's story claims after the membership delete (api.md "Remove, leave and claims").

Order: the membership delete is committed before the first graph call, so access has ended when the plan is read; a
graph failure of any kind never undoes it. ``no_plan`` is not a failure; a task claimed on its own is counted, not
released. Scripted plan for the order and the failures, the real ``PlanService`` (``people_support``) for the end
state of a real claim board.

Sequence::

    web -> API: DELETE /projects/{pid}/members/{user}
    API -> Postgres: delete member row, COMMIT
    API -> plan: claims(pid); release_story(pid, story, force=True) per story claim of the person
    API -> web: {claims_released, claims_release_failed}
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from graph.plan.errors import GraphError
from people_support import API, serve_people
from specflow_api_agent_support import ALPHA, agent_env, bearer_header, events, log_haystack, web_alice
from specflow_api_members_support import ScriptedPlan, claim, install
from specflow_api_support import bearer

ALICE, BEN = "alice@example.com", "ben@example.com"


async def setup(h: Any, plan: ScriptedPlan | None = None) -> SimpleNamespace:
    alice = await h.repo.upsert_user(None, SimpleNamespace(subject=f"user:{ALICE}", email=ALICE, name="Alice"))
    ben = await h.repo.upsert_user(None, SimpleNamespace(subject=f"user:{BEN}", email=BEN, name="Ben"))
    project = await h.repo.create_project(None, alice.id, "Alpha")
    await h.repo.add_member_or_invite(None, project.id, alice.id, BEN, ["example.com"])
    plan = plan or ScriptedPlan()
    install(h.runtime, plan)
    return SimpleNamespace(alice=alice, ben=ben, project=project, plan=plan)


async def remove(h: Any, s: SimpleNamespace, who: str = ALICE) -> Any:
    return await h.client.delete(f"/api/v1/projects/{s.project.id}/members/{s.ben.id}", headers=bearer(who))


async def leave(h: Any, s: SimpleNamespace) -> Any:
    return await h.client.post(f"/api/v1/projects/{s.project.id}/leave", headers=bearer(BEN))


@pytest.mark.parametrize("route", [remove, leave])
async def test_the_membership_delete_is_committed_before_the_first_graph_call(harness, route):
    seen: list[bool] = []
    plan = ScriptedPlan()
    holder = {}

    def check(_name: str) -> None:
        s = holder["s"]
        seen.append((s.project.id, s.ben.id) in harness.repo.members)

    plan.on_call = check
    s = await setup(harness, plan)
    holder["s"] = s
    plan.rows = [claim("STORY::1", s.ben.id)]
    answer = await route(harness, s)
    assert answer.status_code == 200
    assert seen == [False, False]  # the listing and the release both ran after the delete
    assert plan.order == ["claims", "release:STORY::1"]


@pytest.mark.parametrize("route", [remove, leave])
async def test_every_story_claim_of_the_person_is_force_released_and_counted_others_stay(harness, route):
    plan = ScriptedPlan()
    s = await setup(harness, plan)
    plan.rows = [
        claim("STORY::1", s.ben.id),
        claim("STORY::2", s.alice.id),  # someone else's
        claim("STORY::3", s.ben.id),
        claim("STORY::4", s.ben.id, kind="task"),  # on its own: no forced release
    ]
    answer = await route(harness, s)
    assert (answer.status_code, answer.json()) == (200, {"claims_released": 2, "claims_release_failed": False})
    assert plan.released == [(str(s.project.id), "STORY::1", True), (str(s.project.id), "STORY::3", True)]
    assert [(r.story_id, r.claimed_by) for r in plan.rows] == [
        ("STORY::2", str(s.alice.id)),
        ("STORY::4", str(s.ben.id)),
    ]


async def test_the_event_has_ids_and_counts_only(harness, caplog):
    plan = ScriptedPlan()
    s = await setup(harness, plan)
    plan.rows = [claim("STORY::1", s.ben.id), claim("STORY::T", s.ben.id, kind="task")]
    with caplog.at_level(logging.INFO):
        await remove(harness, s)
    (record,) = events(caplog, "api.member.claims_released")
    assert (record.project_id, record.member_user_id) == (str(s.project.id), str(s.ben.id))
    assert (record.released, record.failed, record.task_claims_left) == (1, False, 1)
    haystack = log_haystack(caplog)
    assert "@" not in haystack and "STORY::1" not in haystack


@pytest.mark.parametrize("route", [remove, leave])
async def test_a_project_without_a_plan_releases_nothing_and_is_not_a_failure(harness, route):
    s = await setup(harness, ScriptedPlan(no_plan=True))
    answer = await route(harness, s)
    assert (answer.status_code, answer.json()) == (200, {"claims_released": 0, "claims_release_failed": False})


@pytest.mark.parametrize("route", [remove, leave])
async def test_a_listing_failure_is_reported_and_the_removal_stands(harness, route, caplog):
    s = await setup(harness, ScriptedPlan(list_error=GraphError("neo4j_unreachable")))
    with caplog.at_level(logging.INFO):
        answer = await route(harness, s)
    assert (answer.status_code, answer.json()) == (200, {"claims_released": 0, "claims_release_failed": True})
    assert (s.project.id, s.ben.id) not in harness.repo.members
    assert "@" not in log_haystack(caplog)


async def test_a_failed_release_is_reported_the_others_are_still_tried_and_the_removal_stands(harness, caplog):
    plan = ScriptedPlan(fail_on={"STORY::1"})
    s = await setup(harness, plan)
    plan.rows = [claim("STORY::1", s.ben.id), claim("STORY::2", s.ben.id)]
    with caplog.at_level(logging.INFO):
        answer = await remove(harness, s)
    assert (answer.status_code, answer.json()) == (200, {"claims_released": 1, "claims_release_failed": True})
    assert plan.order == ["claims", "release:STORY::1", "release:STORY::2"]
    assert (s.project.id, s.ben.id) not in harness.repo.members
    assert "secret@example.com" not in log_haystack(caplog) and "secret" not in answer.text


async def test_a_graph_that_cannot_be_loaded_is_a_failure_not_an_error(harness):
    s = await setup(harness)
    harness.runtime.agent_reads = None  # the harness graph store raises on use
    answer = await remove(harness, s)
    assert (answer.status_code, answer.json()) == (200, {"claims_released": 0, "claims_release_failed": True})


async def test_a_refused_removal_touches_no_claim(harness):
    plan = ScriptedPlan()
    s = await setup(harness, plan)
    plan.rows = [claim("STORY::1", s.ben.id)]
    refused = [
        await harness.client.delete(
            f"/api/v1/projects/{s.project.id}/members/{s.alice.id}", headers=bearer(ALICE)
        ),  # the owner
        await harness.client.delete(f"/api/v1/projects/{s.project.id}/members/{s.ben.id}", headers=bearer(BEN)),  # 403
        await harness.client.post(f"/api/v1/projects/{s.project.id}/leave", headers=bearer(ALICE)),  # owner leaves
        await harness.client.delete(f"/api/v1/projects/{s.project.id}/members/{uuid.uuid4()}", headers=bearer(ALICE)),
    ]
    assert [a.status_code for a in refused] == [409, 403, 409, 404]
    assert plan.order == []


async def test_a_real_claim_board_is_freed_story_claims_only_and_the_person_then_has_no_access(monkeypatch, valid_env):
    """The real ``PlanService`` over the in-memory plan: Ben holds two story units (API; X with Y) and a task of his
    own; removing him frees the stories, leaves the task claim, and Alice's web token still works."""

    async with serve_people(monkeypatch, agent_env(valid_env)) as people:
        await people.hold(API, people.ben)
        await people.hold("STORY::A::X", people.ben)
        await people.claim_task("TASK::A::STUCK_OPEN", people.ben)
        path = f"/api/v1/projects/{ALPHA}/members/{people.ben}"
        answer = await people.rig.client.delete(path, headers=bearer_header(web_alice()))
        board = await people.plan.claims(ALPHA)
    assert (answer.status_code, answer.json()) == (200, {"claims_released": 3, "claims_release_failed": False})
    assert [(row.kind, row.story_id, row.claimed_by) for row in board] == [("task", "STORY::A::STUCK", str(people.ben))]
