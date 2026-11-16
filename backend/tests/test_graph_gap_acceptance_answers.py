"""Acceptance tests of graph.gap: Answers (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("08")
def test_fr_gans_01_answer_gap_create_clarification_answers_gap_mode() -> None:
    """Given option A of the cancel-paid-order gap, then the clarification exists with mode `option` and the gap is
    `answered`.
    """


@pending("08")
def test_fr_gans_02_own_answer_turned_into_edits_gap_edits() -> None:
    """Given own text, then `preview_edits` returns the target section before and after, and `answer_gap` with those
    edits stores them unchanged.
    """


@pending("08")
def test_fr_gans_03_answer_rejected_stalegaperror_gap_changed_after_caller() -> None:
    """Given a gap updated by a run after it was read, then `answer_gap` raises `StaleGapError`."""


@pending("08")
def test_fr_gans_04_every_edit_is_one_amendment() -> None:
    """Given the review-limit contradiction answered with "500 EUR everywhere", then one amendment replaces the line
    in `orders-spec.md` and none touches `payments-spec.md`.
    """


@pending("09")
def test_fr_gans_05_contradiction_answered_amend_placement_replace_losing_line() -> None:
    """Given the review-limit answer applied, then the build's graph holds one review-limit rule of 500 EUR."""


@pending("09")
def test_fr_gans_06_answer_no_single_home_go_project_clarifications() -> None:
    """Given an answer with placement `document`, then the next corpus read lists the clarifications document with
    that section.
    """


@pending("09")
def test_fr_gans_07_applied_text_carry_answer_marker_naming_gap() -> None:
    """Given an applied amendment, then the effective text contains the marker line with the gap id."""


@pending("08")
def test_fr_gans_08_answers_applied_after_quiet_period_apply_now() -> None:
    """Given three answers within the quiet period, then one build is requested; given a blocking development gap
    answered, then a build is requested at once.
    """


@pending("08")
def test_fr_gans_09_answer_changed_before_applied_replace_pending_clarification() -> None:
    """Given both cases, then the graph shows one pending clarification in the first and a `REPLACES` relation in the
    second.
    """


@pending("11")
def test_fr_gans_10_ai_answers_listed_review_revert_clarification_mark() -> None:
    """Given an AI answer reverted, then after the build its text is gone from the graph and a follow-up gap
    `FOLLOWS_UP` the old one.
    """


@pending("08")
def test_fr_gans_11_dismiss_gap_require_reason_delete_gap_node() -> None:
    """Given a dismissal, then the node is gone, `gap_suppression` holds the signature and the event log the reason."""


@pending("08")
def test_fr_gans_12_raise_gap_origin_person_create_open_gap() -> None:
    """Given a person's question about two ids, then the gap exists at once and has options after the site run."""


@pending("08")
def test_fr_gans_13_any_project_member_able_answer_dismiss_revert() -> None:
    """Given a member who is not the owner, then the answer succeeds and `answered_by` names them."""
