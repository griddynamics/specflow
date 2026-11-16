"""Acceptance tests of graph.gap: Plan link (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("00", "covered by test_specflow_plan_host and test_graph_plan_host_live")
def test_fr_gpln_01_host_pass_plan_run_build_change_report() -> None:
    """Given two builds after a plan, when the plan runs, then it receives both reports and runs in mode `update`,
    not `replan`.
    """


@pending("07")
def test_fr_gpln_02_gap_service_satisfies_the_plan_findings_port() -> None:
    """Given an open gap about a requirement, then the brief of the task implementing it lists the gap with its
    question.
    """


@pending("13")
def test_fr_gpln_03_task_implements_node_open_blocker_gap_about() -> None:
    """Given a blocker gap about `REQ::FR::PLACE_ORDER`, when an agent claims the Place order story, then the claim
    is refused naming the gap id.
    """


@pending("13")
def test_fr_gpln_04_held_tasks_released_gap_resolves_dismissed_becomes() -> None:
    """Given the blocker gap resolved, then the story can be claimed."""


@pending("09")
def test_fr_gpln_05_gap_answer_re_plan_stories_whose_members() -> None:
    """Given the cancel-paid-order answer applied, then only `STORY::ORDERS_SERVICE::PLACE_ORDER` is re-planned and
    every other story is reused.
    """
