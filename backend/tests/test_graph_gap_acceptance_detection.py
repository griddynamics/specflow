"""Acceptance tests of graph.gap: Detection and sites (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("04")
def test_fr_gdet_01_seed_carry_spec_ids_witness_quoted_line() -> None:
    """Given a full run, then every `gap_seed` row has a witness that the check step resolves."""


@pending("04")
def test_fr_gdet_02_oq_stated_seed_openquestion_node_still_graph() -> None:
    """Given the shop corpus, then a seed names `QST::CANCEL_PAID_ORDER` and `REQ::FR::PLACE_ORDER`."""


@pending("04")
def test_fr_gdet_03_ct_value_seed_near_duplicate_pair_build() -> None:
    """Given the shop corpus, then one seed names both review-limit rules with both lines as witness."""


@pending("04")
def test_fr_gdet_04_ms_provider_ms_store_flag_seed_graph() -> None:
    """Given the shop corpus, then the interface without a provider and the assumption are seeded."""


@pending("04")
def test_fr_gdet_05_am_vague_seed_requirement_whose_text_holds() -> None:
    """Given the shop corpus, then "The admin panel should be fast." is seeded."""


@pending("04")
def test_fr_gdet_06_weak_seed_become_part_site_corroboration_signal() -> None:
    """Given an ownerless node without obligation wording, a named system part or a planned dependency, then it is
    not in any site; given the CSV export requirement, then it is.
    """


@pending("04")
def test_fr_gdet_07_seed_whose_signature_matches_suppression_dropped() -> None:
    """Given a dismissed gap, when its nodes and witness are unchanged, then no seed reaches a site; when its witness
    line changes, then it does.
    """


@pending("04")
def test_fr_gdet_08_seeds_sharing_about_node_owner_merge_into() -> None:
    """Given two stated questions about the same requirement, then they form one site; given a site reaching two
    stories and one reaching none, then the first ranks higher.
    """


@pending("04")
def test_fr_gdet_09_two_openquestion_nodes_asking_question_two_documents() -> None:
    """Given the same question in two documents, then one `Gap` has `ABOUT` to both question nodes."""


@pending("04")
def test_fr_gdet_10_failing_detector_stop_run_reported_run_metrics() -> None:
    """Given a detector that raises, then the other detectors' seeds are recorded and `detector_failures` names it."""
