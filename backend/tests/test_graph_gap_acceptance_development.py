"""Acceptance tests of graph.gap: Development gaps (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("13")
def test_fr_gdev_01_finding_reported_over_mcp_become_gap_origin() -> None:
    """Given `add_finding` with kind question, then an `open_question` gap with origin `development` exists."""


@pending("13")
def test_fr_gdev_02_blocking_development_gap_holds_the_claimed_story() -> None:
    """Given a blocking finding, then the story's claimed tasks are `blocked` with `blocked_by` the gap id."""


@pending("13")
def test_fr_gdev_03_development_gap_investigated_at_once_mode_site() -> None:
    """Given a new development gap, then options exist before the next build and the question is the agent's text."""


@pending("13")
def test_fr_gdev_04_spec_already_answers_development_gap_resolve_pointer() -> None:
    """Given a question answered in `payments-spec.md`, then the story is released and the brief shows the answering
    lines.
    """


@pending("13")
def test_fr_gdev_05_development_gap_always_routed_ask_needs_answer() -> None:
    """Given a minor development gap with every gate holding, then it is `ask`."""


@pending("13")
def test_fr_gdev_06_not_spec_development_finding_closes_as_a_note() -> None:
    """Given a finding about a library bug, then no gap stays open and the story is released."""


@pending("13")
def test_fr_gdev_07_existing_first_slice_findings_migrated_gaps_origin() -> None:
    """Given a project with open and resolved findings, when `ensure_gap_schema` runs, then each has a gap with the
    mapped status and its old id resolves.
    """
