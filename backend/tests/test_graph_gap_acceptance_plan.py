"""Acceptance tests of graph.gap: Plan link (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from pathlib import Path

from agent_contracts import AGENT_ROUTES
from gap_acceptance_support import Planted, pending
from gap_agent_support import ALPHA, API_LOGIC, API_STORY, QUESTION, serve_gaps
from gap_close_support import PLACE_STORY, cancel_answered_and_built, plan_update
from specflow_api_agent_support import agent_env


@pending("00", "covered by test_specflow_plan_host and test_graph_plan_host_live")
def test_fr_gpln_01_host_pass_plan_run_build_change_report() -> None:
    """Given two builds after a plan, when the plan runs, then it receives both reports and runs in mode `update`,
    not `replan`.
    """


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


@pending("13")
def test_fr_gpln_03_task_implements_node_open_blocker_gap_about() -> None:
    """Given a blocker gap about `REQ::FR::PLACE_ORDER`, when an agent claims the Place order story, then the claim
    is refused naming the gap id.
    """


@pending("13")
def test_fr_gpln_04_held_tasks_released_gap_resolves_dismissed_becomes() -> None:
    """Given the blocker gap resolved, then the story can be claimed."""


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
