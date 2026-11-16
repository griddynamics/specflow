"""Acceptance tests of graph.gap: Close check and lifecycle (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("09")
def test_fr_gcls_01_after_build_gap_run_check_applying_gap() -> None:
    """Given the cancel-paid-order answer applied, then the gap is `resolved` and the clarification is `GROUNDED_IN`
    the amended fragment.
    """


@pending("09")
def test_fr_gcls_02_gap_still_detected_after_answer_was_applied() -> None:
    """Given option C of the cancel-paid-order gap (a fee with no rule), then a follow-up gap opens."""


@pending("09")
def test_fr_gcls_03_node_open_gap_about_left_graph_detector() -> None:
    """Given the question line deleted by a document edit, then the gap node is gone and the event log says obsolete."""


@pending("09")
def test_fr_gcls_04_about_impacts_follow_rekeyed_merged_nodes_change() -> None:
    """Given a merge of A into B, then a gap about A is about B after the run."""


@pending("09")
def test_fr_gcls_05_resolved_gap_and_clarification_stay_in_graph() -> None:
    """Given a resolved gap, then a walk from the new requirement through its fragment reaches the clarification and
    the gap.
    """


@pending("09")
def test_fr_gcls_06_gap_clarification_transition_written_event_log_actor() -> None:
    """Given the full cancel-paid-order flow, then the log lists detected, answered, applying, resolved with actors."""
