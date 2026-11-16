"""Acceptance tests of graph.gap: Reconcile (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("10")
def test_fr_grec_01_new_revision_lineage_active_amendments_arrives_corpus() -> None:
    """Given a re-upload of `orders-spec.md`, then reconcile runs before the build and every amendment has an
    outcome.
    """


@pending("10")
def test_fr_grec_02_amendment_whose_anchor_found_exactly_applied_again() -> None:
    """Given the original file synced again, then the outcome is `applied` and the build reports no change in that
    section.
    """


@pending("10")
def test_fr_grec_03_amendment_whose_anchor_found_small_edits_under() -> None:
    """Given the question line reworded, then the outcome is `reanchored` and the answer is applied."""


@pending("10")
def test_fr_grec_04_new_revision_contains_answer_text_judge_call() -> None:
    """Given the owner pasted the answer, and separately rewrote it in their own words, then both outcomes are
    `absorbed` and the build reads the owner's text.
    """


@pending("10")
def test_fr_grec_05_section_says_something_else_build_read_upload() -> None:
    """Given the owner changed the rule, then the build reads the upload and a follow-up gap with those three options
    `FOLLOWS_UP` the old one.
    """


@pending("10")
def test_fr_grec_06_anchor_section_are_gone_clarification_become_orphaned() -> None:
    """Given the section deleted, then the follow-up gap exists with the move option recommended."""


@pending("10")
def test_fr_grec_07_reconcile_outcome_written_gap_event_log() -> None:
    """Given each outcome above, then an event names the amendment, the revision and the outcome."""


@pending("10")
def test_fr_grec_08_failed_judge_call_leave_amendment_undecided_applied() -> None:
    """Given a failing judge, then the outcome is `undecided` and the build still reads the amended text."""
