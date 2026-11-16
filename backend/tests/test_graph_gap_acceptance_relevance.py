"""Acceptance tests of graph.gap: Relevance filter (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("04")
def test_fr_grel_01_relevance_step_classify_section_spec_tracking_mixed() -> None:
    """Given the shop corpus, then `roadmap.md > Release plan` and `sprint-14.md > Sprint scope` are `tracking` or
    `mixed`, the spec documents' sections are `spec`, and at most one model call per uncertain batch is made.
    """


@pending("04")
def test_fr_grel_02_section_relevance_cached_section_text_hash() -> None:
    """Given an unchanged corpus, then the second run makes no `gap_relevance` call."""


@pending("04")
def test_fr_grel_03_structural_detectors_produce_no_seed_node_whose() -> None:
    """Given the shop corpus, then no seed names `REQ::FR::RELEASE_2_3_HARDENING` or the SHOP-412 item."""


@pending("04")
def test_fr_grel_04_text_detectors_ignore_relevance_filter() -> None:
    """Given a contradiction whose one side is in a `tracking` section, then CT-VALUE still seeds it."""


@pending("04")
def test_fr_grel_05_mixed_section_node_obligation_wording_stay_eligible() -> None:
    """Given the shop corpus, then the "R2 must support refunds through Payments" requirement is a weak seed."""


@pending("04")
def test_fr_grel_06_noise_rule_added_person_mark_scope_tracking() -> None:
    """Given a noise rule on `roadmap.md > Release plan`, then the next run gives no structural seed from it and the
    noise report says `person`.
    """


@pending("04")
def test_fr_grel_07_not_spec_report_records_the_site_not_spec() -> None:
    """Given a fake report `not_spec`, then no gap is written and the noise report names the nodes."""
