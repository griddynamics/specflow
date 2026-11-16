"""Acceptance tests of graph.gap: Non-functional (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("06")
def test_nfr_gsec_01_no_model_output_written_before_code_validated() -> None:
    """Prompt-injection fixtures in the shop corpus change no gap, option or answer beyond what the checks allow."""


@pending("05")
def test_nfr_gsec_02_logs_contain_document_text_questions_answers_quotes() -> None:
    """Log capture of a full run and an answer contains no fixture sentence."""


@pending("06")
def test_nfr_gcost_01_update_run_after_build_changed_no_document() -> None:
    """Gateway fake records zero calls."""


@pending("15")
def test_nfr_gcost_02_update_run_make_at_most_calls_limits() -> None:
    """The run's cost report stays within the bound computed from the rules."""


@pending("15", "manual benchmark; early check: 04")
def test_nfr_gperf_01_detection_over_graph_5_000_spec_nodes() -> None:
    """Benchmark test on a generated graph (manual run, reported in the pull request)."""


@pending("05")
def test_nfr_gport_01_graph_gap_run_graph_store_core_supports() -> None:
    """The gap integration tests run against each configured store."""
