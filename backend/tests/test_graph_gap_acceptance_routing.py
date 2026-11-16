"""Acceptance tests of graph.gap: Severity and routing (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("06", "AI branch: 11")
def test_fr_grte_01_code_decide_route_kind_severity_gates_model() -> None:
    """Inspection and test: a report with `ai_answerable: true` on a contradiction is routed `ask`."""


@pending("06")
def test_fr_grte_02_blocker_major_gap_always_routed_ask() -> None:
    """Given reports of each severity, then blockers and majors are `ask`."""


@pending("11")
def test_fr_grte_03_minor_gap_routed_ai_kind_ai_kinds() -> None:
    """Given a minor missing-information gap with confidence 0.84, then it is `ask`; with 0.9 and all gates, then
    `ai`.
    """


@pending("06")
def test_fr_grte_04_info_report_become_run_note_graph_node() -> None:
    """Given an info report, then a `gap_note` row exists and no `Gap` node."""


@pending("11", "limits: 08")
def test_fr_grte_05_ai_answers_stop_at_run_limit_or_node_share() -> None:
    """Given 25 AI-eligible gaps, then 20 are answered by AI and 5 are `ask`."""


@pending("06")
def test_fr_grte_06_run_show_at_most_configured_number_new() -> None:
    """Given 30 new `ask` gaps, then the default inbox lists 25."""
