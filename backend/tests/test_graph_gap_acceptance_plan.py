"""Acceptance tests of graph.gap: Plan link (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_contracts import AGENT_ROUTES
from gap_acceptance_support import Planted
from gap_agent_support import ALPHA, API_LOGIC, API_STORY, QUESTION, serve_gaps
from gap_close_support import PLACE_STORY, cancel_answered_and_built, plan_update
from gap_development_support import CLAIM, MEMBER, dev_rig
from graph.gap import NewGap
from specflow_api_agent_support import agent_env


async def test_fr_gpln_01_host_pass_plan_run_build_change_report(tmp_path, caplog) -> None:
    """Given two builds after a plan, when the plan runs, then it receives both reports and runs in mode `update`,
    not `replan`.

    The host's plan runner (``test_specflow_plan_host``): builds 3 and 4 after the plan's build 1 (a failed build 2
    in between changed nothing); the plan run gets both reports in build order and ``replan`` false. Live on Neo4j:
    ``test_graph_plan_host_live``.
    """

    import test_specflow_plan_host as checks  # noqa: PLC0415 (imported here so pytest collects its tests once)

    await checks.test_fr_gpln_01_several_builds_after_the_plan_pass_every_report_in_build_order(tmp_path, caplog)


async def test_fr_gpln_02_gap_service_satisfies_the_plan_findings_port(monkeypatch, valid_env) -> None:
    """Given an open gap about a requirement, then the brief of the task implementing it lists the gap with its
    question.

    After a gap run on the shop corpus, the stated question "Can a customer cancel a paid order?" is an open gap
    ``ABOUT`` ``REQ::FR::PLACE_ORDER``; the task ``TASK::A::API_LOGIC`` implements that requirement. The plan service
    reads gaps through the host's findings port (``OpenGaps`` over ``GapService``), so the task brief (agent route),
    the story brief, the story preview and the story counts show it, with no change to ``graph.plan``.
    """

    async with serve_gaps(monkeypatch, agent_env(valid_env)) as gaps:
        gap_id = gaps.question_gap()
        task = (await gaps.get(AGENT_ROUTES["get_task_brief"], project_id=ALPHA, task_id=API_LOGIC)).json()
        story = await gaps.plan.story_brief(ALPHA, API_STORY)
        preview = await gaps.plan.story_preview(ALPHA, API_STORY)
        counts = {item.story_id: item for item in await gaps.plan.story_findings(ALPHA, [API_STORY, "STORY::A::UI"])}

    listed = {item["finding_id"]: item for item in task["findings"]}
    assert listed[gap_id]["text"] == QUESTION and listed[gap_id]["kind"] == "open_question"
    assert Planted.PLACE_ORDER in listed[gap_id]["about"] and listed[gap_id]["severity"] == "major"
    assert gap_id in {item.finding_id for item in story.findings}
    assert gap_id in {item.finding_id for item in preview.findings}
    assert counts[API_STORY].count >= 1 and counts["STORY::A::UI"].count == 0


async def _blocker(dev: Any) -> str:
    raised = await dev.gaps.raise_gap(
        ALPHA,
        NewGap(
            kind="missing",
            question="Which payment methods may a customer place an order with?",
            about=(Planted.PLACE_ORDER,),
            severity="blocker",
            origin="person",
            created_by=MEMBER,
        ),
    )
    assert raised.held == (API_LOGIC,)
    return raised.gap_id


async def test_fr_gpln_03_task_implements_node_open_blocker_gap_about(monkeypatch, valid_env) -> None:
    """Given a blocker gap about `REQ::FR::PLACE_ORDER`, when an agent claims the Place order story, then the claim
    is refused naming the gap id.
    """

    async with dev_rig(monkeypatch, valid_env) as dev:
        gap_id = await _blocker(dev)

        refused = await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)

    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "not_available")
    assert [item["finding_id"] for item in refused.json()["error"]["details"]["findings"]] == [gap_id]


async def test_fr_gpln_04_held_tasks_released_gap_resolves_dismissed_becomes(monkeypatch, valid_env) -> None:
    """Given the blocker gap resolved, then the story can be claimed.

    Resolved by a pointer, dismissed or obsolete: every end of the gap releases its held tasks (the pointer and the
    close check's resolution are covered in test_api_gap_development and FR-GDEV-04; here the dismissal)."""

    async with dev_rig(monkeypatch, valid_env) as dev:
        gap_id = await _blocker(dev)
        await dev.gaps.dismiss_gap(ALPHA, gap_id, reason="Decided in the call.", by=MEMBER)
        claimed = await dev.rig.post(CLAIM, {"story_id": API_STORY}, project_id=ALPHA)

    assert claimed.status_code == 200 and dev.store.tasks[API_LOGIC]["status"] == "in_progress"


async def test_fr_gpln_05_gap_answer_re_plan_stories_whose_members(tmp_path: Path) -> None:
    """Given the cancel-paid-order answer applied, then only `STORY::ORDERS_SERVICE::PLACE_ORDER` is re-planned and
    every other story is reused.

    The plan is made of the corpus graph with planners that answer from the request; the apply build is simulated
    (``gap_close_support.apply_build``) and the plan runs ``update`` over its change report.
    """

    rig, _, _, rebuilt = await cancel_answered_and_built(1)

    planned = await plan_update(rig.world, rebuilt, tmp_path)

    assert planned.stories == {PLACE_STORY}
    assert planned.kept_stories and PLACE_STORY not in planned.kept_stories
