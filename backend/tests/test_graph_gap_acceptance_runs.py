"""Acceptance tests of graph.gap: Gap runs (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from gap_acceptance_support import pending


@pending("05")
def test_fr_grun_01_build_returns_auto_run_after_build_set() -> None:
    """Given an update build, then the run log shows the build, the gap run and the plan run in that order for the
    project.
    """


@pending("05")
def test_fr_grun_02_mode_update_run_gap_run_detector_over() -> None:
    """Given an update build that changed one fragment, then detectors report seeds for the whole graph and
    investigation runs only on sites touching that fragment's nodes.
    """


@pending("05")
def test_fr_grun_03_site_s_input_hash_equals_cached_verdict() -> None:
    """Given two consecutive update runs with no document change, then the second run makes no model call."""


@pending("06")
def test_fr_grun_04_run_fail_because_model_failure_site_recorded() -> None:
    """Given a gateway fake that fails one site, then the run completes, the site is `deferred`, and the next run
    investigates it.
    """


@pending("05")
def test_fr_grun_05_run_respect_limits_rules_file_defer_lowest() -> None:
    """Given 40 eligible sites in an update run, then 30 are investigated and 10 are `deferred`."""


@pending("05")
def test_fr_grun_06_estimate_gap_make_no_model_call_write() -> None:
    """Given a gateway fake that fails every call, then `estimate_gap` returns site and call counts and the stores
    are unchanged.
    """


@pending("05")
def test_fr_grun_07_run_interrupted_after_step_resume_last_completed() -> None:
    """Given a run stopped after `detect`, when resumed, then seeds are not recomputed and the run completes."""


@pending("05")
def test_fr_grun_08_run_report_metrics_cost_per_gateway_role() -> None:
    """Given a full run of the shop corpus, then the result names seeds per detector, outcomes per site, calls and
    tokens per role, and the tracking sections.
    """
