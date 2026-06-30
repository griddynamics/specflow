"""Unit tests for the pure TUI formatters (tui/render.py).

These exercise the rendering logic against fixture /status payloads with no
network and no Textual import — render.py must stay pure.
"""

from tui.constants import StepState
from tui import render


def _running_payload() -> dict:
    return {
        "status": "running",
        "checkpoint": "generation_started",
        "workspace_count": 3,
        "num_turns": 410,
        "total_tokens_used_display": "12.4M",
        "current_phase": "Generating code",
        "progress": {
            "workspace_phases": {
                "ws-01-2": {"last_completed_phase": 5, "total_phases": 9, "phase_name": "Payments"},
                "ws-01-1": {"last_completed_phase": 6, "total_phases": 9, "phase_name": "Auth API"},
            }
        },
    }


class TestStatusPill:
    def test_known_status(self):
        text, style = render.status_pill("running")
        assert "RUNNING" in text
        assert style == "green"

    def test_unknown_falls_back(self):
        text, _ = render.status_pill("weird")
        assert "UNKNOWN" in text

    def test_none_falls_back(self):
        text, _ = render.status_pill(None)
        assert "UNKNOWN" in text


class TestPipelineSteps:
    def test_running_marks_done_active_pending(self):
        steps = render.pipeline_steps(_running_payload())
        labels = {s.label: s.state for s in steps}
        # checkpoint == generation_started → that step and earlier are DONE
        assert labels["Generation started"] is StepState.DONE
        assert labels["KB init"] is StepState.DONE
        # next step is the active one
        assert labels["Generating code"] is StepState.ACTIVE
        # later steps pending
        assert labels["Deploy & E2E"] is StepState.PENDING
        assert labels["Estimation (P10Y)"] is StepState.PENDING

    def test_completed_marks_all_done(self):
        steps = render.pipeline_steps({"status": "completed", "checkpoint": "estimation_done"})
        assert all(s.state is StepState.DONE for s in steps)

    def test_unknown_checkpoint_leaves_first_active(self):
        steps = render.pipeline_steps({"status": "running", "checkpoint": "nonsense"})
        assert steps[0].state is StepState.ACTIVE
        assert all(s.state is StepState.PENDING for s in steps[1:])

    def test_step_symbol_matches_state(self):
        steps = render.pipeline_steps(_running_payload())
        active = next(s for s in steps if s.state is StepState.ACTIVE)
        assert active.symbol == "●"

    def test_local_only_hides_deploy_step(self):
        payload = {
            "status": "running",
            "checkpoint": "generation_done",
            "last_spec_readiness": "LOCAL_ONLY",
        }
        steps = render.pipeline_steps(payload)
        labels = {s.label: s.state for s in steps}
        assert "Deploy & E2E" not in labels
        # The next real step after generation is active, not the (absent) deploy step.
        assert labels["Outputs archived"] is StepState.ACTIVE

    def test_local_only_is_case_insensitive(self):
        payload = {
            "status": "running",
            "checkpoint": "kb_init_done",
            "last_spec_readiness": "local_only",
        }
        assert all(s.label != "Deploy & E2E" for s in render.pipeline_steps(payload))

    def test_integration_run_keeps_deploy_step(self):
        payload = {
            "status": "running",
            "checkpoint": "generation_done",
            "last_spec_readiness": "INTEGRATION_TESTS_READY",
        }
        labels = {s.label: s.state for s in render.pipeline_steps(payload)}
        assert labels["Deploy & E2E"] is StepState.ACTIVE


class TestWorkspaceBars:
    def test_parses_and_sorts_by_id(self):
        bars = render.workspace_bars(_running_payload())
        assert [b.workspace_id for b in bars] == ["ws-01-1", "ws-01-2"]

    def test_fraction_and_percent(self):
        bars = render.workspace_bars(_running_payload())
        ws1 = bars[0]
        assert abs(ws1.fraction - 6 / 9) < 1e-9
        assert ws1.percent == 67
        assert ws1.phase_label == "Phase 6/9"

    def test_reads_top_level_workspace_phases(self):
        payload = {
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 3, "total_phases": 9, "phase_name": "Auth API"},
            }
        }
        bar = render.workspace_bars(payload)[0]
        assert bar.workspace_id == "ws-01-1"
        assert bar.phase_name == "Auth API"
        assert bar.percent == 33

    def test_top_level_takes_precedence_over_progress(self):
        payload = {
            "workspace_phases": {"ws-1": {"last_completed_phase": 1, "total_phases": 2}},
            "progress": {
                "workspace_phases": {"ws-OLD": {"last_completed_phase": 0, "total_phases": 2}}
            },
        }
        assert [b.workspace_id for b in render.workspace_bars(payload)] == ["ws-1"]

    def test_empty_when_no_phases(self):
        assert render.workspace_bars({"progress": {}}) == []

    def test_unknown_total_is_safe(self):
        payload = {"progress": {"workspace_phases": {"ws-1": {"last_completed_phase": 2}}}}
        bar = render.workspace_bars(payload)[0]
        assert bar.fraction == 0.0
        assert bar.percent == 0
        assert bar.phase_label == "Phase 2/?"


class TestProgressBar:
    def test_full_and_empty(self):
        assert render.progress_bar(1.0, 4) == "████"
        assert render.progress_bar(0.0, 4) == "░░░░"

    def test_clamps_out_of_range(self):
        assert render.progress_bar(5.0, 3) == "███"
        assert render.progress_bar(-1.0, 3) == "░░░"


class TestTokensSummary:
    def test_includes_display_and_turns(self):
        out = render.tokens_summary(_running_payload())
        assert "12.4M" in out and "410 turns" in out

    def test_empty_when_absent(self):
        assert render.tokens_summary({}) == ""


class TestEstimatePanel:
    def test_none_when_no_result(self):
        assert render.estimate_panel(None) is None

    def test_full_result(self):
        result = {
            "summary": {
                "average_hours": 318,
                "min_hours": 291,
                "max_hours": 344,
                "coefficient_of_variation": 0.08,
                "variance_assessment": "low",
                "risk_assessment": {
                    "status": "Approved",
                    "total_buffer_pct": 12,
                    "final_estimate": 356,
                },
            },
            "workspace_estimations": [
                {"workspace_name": "ws-01-1", "total_hours": 305},
                {"workspace_name": "ws-01-2", "total_hours": 331},
            ],
            "total_usd_cost": 94.1,
        }
        panel = render.estimate_panel(result)
        assert panel.average_hours == 318
        assert panel.risk_status == "Approved"
        assert panel.per_workspace == [("ws-01-1", 305.0), ("ws-01-2", 331.0)]
        assert panel.total_usd_cost == 94.1

    def test_partial_result_is_tolerant(self):
        panel = render.estimate_panel({"summary": {"average_hours": 100}})
        assert panel.average_hours == 100
        assert panel.risk_status is None
        assert panel.per_workspace == []


class _Event:
    """Minimal attr-compatible stand-in for tui.stream.AgentStreamEvent."""

    def __init__(self, **kw):
        self.timestamp = kw.get("timestamp")
        self.kind = kw.get("kind", "unknown")
        self.message = kw.get("message", "")
        self.tool_name = kw.get("tool_name")
        self.subagent_name = kw.get("subagent_name")


class TestStreamRow:
    def test_formats_time_kind_and_message(self):
        row = render.stream_row(
            _Event(timestamp="2026-06-26T14:02:31.123456+00:00", kind="assistant_text", message="hi")
        )
        assert row.time == "14:02:31"
        assert row.kind == "assistant_text"
        assert row.message == "hi"
        assert row.label == ""

    def test_subagent_name_preferred_over_tool_name(self):
        row = render.stream_row(
            _Event(kind="tool_use", tool_name="Task", subagent_name="explore", message="x")
        )
        assert row.label == "explore"

    def test_tool_name_used_when_no_subagent(self):
        row = render.stream_row(_Event(kind="tool_use", tool_name="Bash", message="ls"))
        assert row.label == "Bash"

    def test_bad_timestamp_is_blank(self):
        row = render.stream_row(_Event(timestamp="not-a-date", kind="result", message="done"))
        assert row.time == ""

    def test_missing_timestamp_is_blank(self):
        assert render.stream_row(_Event(kind="system", message="init")).time == ""


class TestKindStyle:
    def test_known_kinds_have_styles(self):
        assert render.kind_style("tool_use") == "cyan"
        assert render.kind_style("result") == "bold green"

    def test_unknown_kind_returns_empty(self):
        assert render.kind_style("nope") == ""


def _usage_payload() -> dict:
    return {
        "workspace_phases": {
            "ws-01-1": {
                "last_completed_phase": 3,
                "total_phases": 9,
                "phase_name": "Auth API",
                "models": ["claude-sonnet-4"],
                "usage": {
                    "num_turns": 12,
                    "input_tokens": 1_200_000,
                    "output_tokens": 240_000,
                    "cache_write_tokens": 5_000,
                    "cache_read_tokens": 800,
                    "total_tokens": 1_445_800,
                },
            },
            "ws-01-2": {"last_completed_phase": 0, "total_phases": 9, "phase_name": ""},
        }
    }


class TestWorkspaceStats:
    def test_full_usage(self):
        stats = render.workspace_stats(_usage_payload(), "ws-01-1")
        assert stats.workspace_id == "ws-01-1"
        assert stats.models == ["claude-sonnet-4"]
        assert stats.phase_name == "Auth API"
        assert stats.phase_label == "Phase 3/9"
        assert stats.percent == 33
        assert stats.num_turns == 12
        assert stats.input_tokens == 1_200_000
        assert stats.total_tokens == 1_445_800

    def test_missing_usage_is_optional(self):
        stats = render.workspace_stats(_usage_payload(), "ws-01-2")
        assert stats.models == []
        assert stats.num_turns is None
        assert stats.total_tokens is None
        assert stats.percent == 0

    def test_unknown_workspace_returns_none(self):
        assert render.workspace_stats(_usage_payload(), "ws-99-9") is None

    def test_reads_legacy_progress_nesting(self):
        payload = {"progress": {"workspace_phases": {"ws-1": {"last_completed_phase": 1, "total_phases": 2}}}}
        stats = render.workspace_stats(payload, "ws-1")
        assert stats.percent == 50


class TestFormatTokens:
    def test_none_is_dash(self):
        assert render.format_tokens(None) == "—"

    def test_small_is_plain(self):
        assert render.format_tokens(940) == "940"

    def test_thousands(self):
        assert render.format_tokens(12_400) == "12.4K"

    def test_millions(self):
        assert render.format_tokens(1_445_800) == "1.4M"


class TestSetNumberHelpers:
    def test_set_number_from_workspace_id(self):
        assert render.set_number_from_workspace_id("ws-01-1") == 1
        assert render.set_number_from_workspace_id("ws-12-3") == 12
        assert render.set_number_from_workspace_id("bad") is None
        assert render.set_number_from_workspace_id("ws-xx-1") is None

    def test_run_set_number_typical_payload(self):
        assert render.run_set_number(_running_payload()) == 1

    def test_run_set_number_empty_or_missing(self):
        assert render.run_set_number({}) is None
        assert render.run_set_number(None) is None

    def test_run_set_number_inconsistent_sets(self):
        payload = {
            "progress": {
                "workspace_phases": {
                    "ws-01-1": {"last_completed_phase": 1},
                    "ws-02-1": {"last_completed_phase": 1},
                }
            }
        }
        assert render.run_set_number(payload) is None


class TestClearWsEligibility:
    def test_eligible_when_set_in_cleaning(self):
        assert render.clear_ws_eligible(1, {1, 2}) is True

    def test_ineligible_when_set_missing_or_unknown(self):
        assert render.clear_ws_eligible(None, {1}) is False
        assert render.clear_ws_eligible(3, {1, 2}) is False

    def test_ineligible_message_running(self):
        msg = render.clear_ws_ineligible_message({"status": "running"})
        assert "still running" in msg

    def test_ineligible_message_terminal(self):
        msg = render.clear_ws_ineligible_message({"status": "completed"})
        assert "Nothing to clear" in msg
