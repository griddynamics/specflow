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
        # The two former "Generation started" / "Generating code" rows are one.
        assert "Generation started" not in labels
        # checkpoint == generation_started → earlier steps are DONE
        assert labels["KB init"] is StepState.DONE
        # generation is in flight → the collapsed "Generating code" row is active
        assert labels["Generating code"] is StepState.ACTIVE
        # later steps pending
        assert labels["Deploy & E2E"] is StepState.PENDING
        assert labels["Estimation (P10Y)"] is StepState.PENDING

    def test_generation_done_marks_generating_code_done(self):
        # Once generation_done is reached, the collapsed row is DONE and the next
        # real step (Deploy, for an integration run) becomes active.
        payload = {
            "status": "running",
            "checkpoint": "generation_done",
            "last_spec_readiness": "INTEGRATION_TESTS_READY",
        }
        labels = {s.label: s.state for s in render.pipeline_steps(payload)}
        assert labels["Generating code"] is StepState.DONE
        assert labels["Deploy & E2E"] is StepState.ACTIVE

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

    def test_local_only_skips_deploy_step(self):
        payload = {
            "status": "running",
            "checkpoint": "generation_done",
            "last_spec_readiness": "LOCAL_ONLY",
        }
        steps = render.pipeline_steps(payload)
        labels = {s.label: s.state for s in steps}
        # Deploy is shown (struck-through), not hidden, so the pipeline shape reads fully.
        assert labels["Deploy & E2E"] is StepState.SKIPPED
        # A skipped step never claims the active slot — the next real step is active.
        assert labels["Outputs archived"] is StepState.ACTIVE

    def test_skipped_step_symbol(self):
        payload = {
            "status": "running",
            "checkpoint": "generation_done",
            "last_spec_readiness": "LOCAL_ONLY",
        }
        deploy = next(s for s in render.pipeline_steps(payload) if s.label == "Deploy & E2E")
        assert deploy.state is StepState.SKIPPED
        assert deploy.symbol == "⊘"

    def test_local_only_is_case_insensitive(self):
        payload = {
            "status": "running",
            "checkpoint": "kb_init_done",
            "last_spec_readiness": "local_only",
        }
        labels = {s.label: s.state for s in render.pipeline_steps(payload)}
        assert labels["Deploy & E2E"] is StepState.SKIPPED

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
        panel = render.estimate_panel({"result": result})
        assert panel.average_hours == 318
        assert panel.risk_status == "Approved"
        assert panel.per_workspace == [("ws-01-1", 305.0), ("ws-01-2", 331.0)]
        assert panel.total_usd_cost == 94.1
        assert panel.phase_comparison == []

    def test_partial_result_is_tolerant(self):
        panel = render.estimate_panel({"result": {"summary": {"average_hours": 100}}})
        assert panel.average_hours == 100
        assert panel.risk_status is None
        assert panel.per_workspace == []

    def test_phase_comparison_sorted_by_phase_number(self):
        result = {
            "summary": {},
            "comparative_analysis": {
                "phase_comparison": {
                    "13": {"phase_number": 13, "phase_name": "Frontend", "average": 20.0, "variance_percentage": 25.0},
                    "06": {"phase_number": 6, "phase_name": "Backend", "average": 40.0, "variance_percentage": 5.0},
                }
            },
        }
        panel = render.estimate_panel({"result": result})
        # Plan/timeline order (ascending by phase number), not variance order.
        assert [row.phase_number for row in panel.phase_comparison] == [6, 13]
        assert [row.phase_name for row in panel.phase_comparison] == ["Backend", "Frontend"]
        assert panel.phase_comparison[0].average_hours == 40.0

    def test_unphased_row_sorts_last(self):
        result = {
            "summary": {},
            "comparative_analysis": {
                "phase_comparison": {
                    "unphased": {"phase_number": None, "phase_name": "unphased", "average": 5.0, "variance_percentage": 0.0},
                    "06": {"phase_number": 6, "phase_name": "Backend", "average": 40.0, "variance_percentage": 5.0},
                }
            },
        }
        panel = render.estimate_panel({"result": result})
        assert [row.phase_number for row in panel.phase_comparison] == [6, None]


class TestTruncate:
    def test_short_string_unchanged(self):
        assert render.truncate("Backend", 32) == "Backend"

    def test_long_string_gets_ellipsis(self):
        result = render.truncate("A very long phase description name", 10)
        assert result == "A very lo…"
        assert len(result) == 10

    def test_none_is_safe(self):
        assert render.truncate(None, 5) == ""


class _Event:
    """Minimal attr-compatible stand-in for tui.stream.AgentStreamEvent."""

    def __init__(self, **kw):
        self.timestamp = kw.get("timestamp")
        self.kind = kw.get("kind", "unknown")
        self.message = kw.get("message", "")
        self.tool_name = kw.get("tool_name")
        self.subagent_name = kw.get("subagent_name")


class TestFormatLocal:
    """Backend timestamps are UTC; the TUI must show them on the viewer's clock."""

    def test_utc_timestamp_shown_in_local_time(self, local_timezone):
        with local_timezone("Europe/Warsaw"):  # UTC+2 in July
            assert render.format_local("2026-07-28T15:24:05+00:00") == "17:24:05"

    def test_offset_naive_timestamp_read_as_utc(self, local_timezone):
        with local_timezone("Europe/Warsaw"):
            assert render.format_local("2026-07-28T15:24:05") == "17:24:05"

    def test_non_utc_offset_converted_to_local(self, local_timezone):
        with local_timezone("Europe/Warsaw"):  # 15:24+05:00 is 10:24 UTC is 12:24 local
            assert render.format_local("2026-07-28T15:24:05+05:00") == "12:24:05"

    def test_date_format_rolls_over_with_local_day(self, local_timezone):
        with local_timezone("Europe/Warsaw"):
            formatted = render.format_local(
                "2026-06-30T23:30:00+00:00", render.LOCAL_DATE_TIME_FORMAT
            )
        assert formatted == "Jul 01 01:30"

    def test_missing_and_unparseable_are_blank(self):
        assert render.format_local(None) == ""
        assert render.format_local("") == ""
        assert render.format_local("not-a-date") == ""


class TestStreamRow:
    def test_formats_time_kind_and_message(self, local_timezone):
        with local_timezone("Europe/Warsaw"):  # UTC+2 in June
            row = render.stream_row(
                _Event(
                    timestamp="2026-06-26T14:02:31.123456+00:00",
                    kind="assistant_text",
                    message="hi",
                )
            )
        assert row.time == "16:02:31"
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


def _event(ws="ws-01-1", kind="agent_crash", message="connection lost", phase=12, at="2026-07-23T18:28:40+00:00"):
    return {"at": at, "workspace_id": ws, "kind": kind, "message": message, "phase": phase}


class TestAgentErrorEventRows:
    def test_parses_events_into_rows(self, local_timezone):
        payload = {"agent_error_events": [_event()]}
        with local_timezone("Europe/Warsaw"):  # UTC+2 in July
            rows = render.agent_error_event_rows(payload)
        assert len(rows) == 1
        row = rows[0]
        assert row.time == "20:28:40"
        assert row.workspace_id == "ws-01-1"
        assert row.phase_label == "phase 12"
        assert row.kind == "agent_crash"
        assert row.message == "connection lost"

    def test_missing_phase_gives_empty_label(self):
        payload = {"agent_error_events": [{**_event(), "phase": None}]}
        assert render.agent_error_event_rows(payload)[0].phase_label == ""

    def test_empty_or_missing_key_returns_empty(self):
        assert render.agent_error_event_rows({}) == []
        assert render.agent_error_event_rows({"agent_error_events": []}) == []
        assert render.agent_error_event_rows(None) == []

    def test_non_dict_entries_are_skipped(self):
        payload = {"agent_error_events": ["garbage", _event()]}
        assert len(render.agent_error_event_rows(payload)) == 1

    def test_workspaces_with_events(self):
        payload = {"agent_error_events": [_event(ws="ws-01-1"), _event(ws="ws-01-3")]}
        assert render.workspaces_with_events(payload) == {"ws-01-1", "ws-01-3"}

    def test_workspace_warning_rows_filters_and_pins_model_fallback_first(self):
        payload = {
            "agent_error_events": [
                _event(ws="ws-01-1", kind="agent_crash", message="crash 1"),
                _event(ws="ws-01-2", kind="agent_crash", message="other ws"),
                _event(ws="ws-01-1", kind="model_fallback", message="switched to sonnet"),
            ]
        }
        rows = render.workspace_warning_rows(payload, "ws-01-1")
        assert [r.message for r in rows] == ["switched to sonnet", "crash 1"]


class TestAgentStateBadge:
    def test_known_states(self):
        assert render.agent_state_badge("retrying") == ("RETRYING", "bold yellow")
        assert render.agent_state_badge("aborted") == ("ABORTED", "bold red")

    def test_none_and_unknown(self):
        assert render.agent_state_badge(None) is None
        assert render.agent_state_badge("") is None
        assert render.agent_state_badge("weird") is None

    def test_workspace_bars_carry_agent_state(self):
        payload = {
            "workspace_phases": {
                "ws-01-1": {"last_completed_phase": 3, "total_phases": 9, "agent_state": "retrying"},
                "ws-01-2": {"last_completed_phase": 4, "total_phases": 9},
            }
        }
        bars = render.workspace_bars(payload)
        assert bars[0].agent_state == "retrying"
        assert bars[1].agent_state is None


class TestErrorKindStyle:
    def test_error_stream_kind_has_style(self):
        assert render.kind_style("error") == "bold red"


# ---------------------------------------------------------------------------
# Workspace-pool management formatters
# ---------------------------------------------------------------------------


def _pool_member(ws_id: str, **overrides) -> dict:
    row = {
        "workspace_id": ws_id,
        "status": "available",
        "repo_url": f"https://github.com/acme/{ws_id}",
        "clean_verified": True,
        "locked_by": None,
        "owner_generation_status": None,
        "remaining_grace_seconds": None,
        "stuck_reason": None,
        "error": None,
        "last_used_by": None,
        "reclaimable": False,
        "retry_lost_on_reclaim": False,
        "removable": True,
        "not_removable_reason": None,
    }
    row.update(overrides)
    return row


def _pool_payload(**set_overrides) -> dict:
    entry = {
        "workspace_pool": "default",
        "set_number": 1,
        "allocatable": True,
        "blocked_reason": None,
        "members": [_pool_member("ws-01-1"), _pool_member("ws-01-2"), _pool_member("ws-01-3")],
    }
    entry.update(set_overrides)
    return {"sets": [entry]}


class TestRepoName:
    def test_strips_host_and_git_suffix(self):
        assert render.repo_name("https://github.com/acme/specflow-workspace1") == "acme/specflow-workspace1"
        assert render.repo_name("https://github.com/acme/ws.git") == "acme/ws"

    def test_trailing_slash_and_missing(self):
        assert render.repo_name("https://github.com/acme/ws/") == "acme/ws"
        assert render.repo_name(None) == "—"
        assert render.repo_name("") == "—"


class TestWorkspaceStatusPill:
    def test_known_statuses(self):
        assert render.workspace_status_pill("available")[0] == "○ AVAILABLE"
        assert render.workspace_status_pill("allocated")[0] == "● ALLOCATED"
        assert render.workspace_status_pill("cleaning")[0] == "◐ CLEANING"
        assert render.workspace_status_pill("stuck")[1] == "bold red"

    def test_unknown_and_none_fall_back(self):
        assert render.workspace_status_pill(None)[0] == "? UNKNOWN"
        assert render.workspace_status_pill("weird")[0] == "? UNKNOWN"


class TestWorkspaceDetail:
    """The trailing column must answer 'why can't I use this?' at a glance."""

    def test_allocated_names_owner_and_status(self):
        detail = render.workspace_detail(
            _pool_member("ws-01-1", status="allocated", locked_by="gen_x", owner_generation_status="running")
        )
        assert detail == "gen_x (running)"

    def test_allocated_flags_lost_retry(self):
        detail = render.workspace_detail(
            _pool_member(
                "ws-01-1",
                status="allocated",
                locked_by="gen_f",
                owner_generation_status="failed",
                retry_lost_on_reclaim=True,
            )
        )
        assert "retry would be lost" in detail

    def test_cleaning_shows_grace_countdown(self):
        detail = render.workspace_detail(
            _pool_member("ws-01-1", status="cleaning", remaining_grace_seconds=90 * 60)
        )
        assert detail == "cleaning, 1h 30min of grace left"

    def test_stuck_prefers_reason_then_error(self):
        assert "disk full" in render.workspace_detail(
            _pool_member("ws-01-1", status="stuck", stuck_reason="disk full")
        )
        assert "push failed" in render.workspace_detail(
            _pool_member("ws-01-1", status="stuck", stuck_reason=None, error="push failed")
        )

    def test_available_but_unverified_says_it_cannot_allocate(self):
        detail = render.workspace_detail(_pool_member("ws-01-1", clean_verified=False))
        assert detail == "not clean-verified — cannot be allocated"

    def test_available_falls_back_to_last_run(self):
        assert render.workspace_detail(_pool_member("ws-01-1", last_used_by="gen_old")) == "last run gen_old"
        assert render.workspace_detail(_pool_member("ws-01-1")) == "never used"


class TestPoolSetRows:
    def test_labels_and_members(self):
        rows = render.pool_set_rows(_pool_payload())
        assert len(rows) == 1
        assert rows[0].label == "Set 01"
        assert rows[0].allocatable is True
        assert [m.workspace_id for m in rows[0].members] == ["ws-01-1", "ws-01-2", "ws-01-3"]

    def test_non_default_pool_is_marked_in_the_label(self):
        rows = render.pool_set_rows(_pool_payload(workspace_pool="testpool"))
        assert rows[0].label == "Set 01 [testpool]"

    def test_unnumbered_set_is_labelled_not_crashed(self):
        rows = render.pool_set_rows(_pool_payload(set_number=None))
        assert rows[0].label == "Unassigned"

    def test_blocked_reason_carried_through(self):
        rows = render.pool_set_rows(
            _pool_payload(allocatable=False, blocked_reason="ws-01-2 is allocated")
        )
        assert rows[0].allocatable is False
        assert rows[0].blocked_reason == "ws-01-2 is allocated"

    def test_reclaimable_member_ids_and_retry_flag(self):
        rows = render.pool_set_rows(
            _pool_payload(
                members=[
                    _pool_member("ws-01-1"),
                    _pool_member("ws-01-2", status="cleaning", reclaimable=True),
                    _pool_member(
                        "ws-01-3", status="allocated", reclaimable=True, retry_lost_on_reclaim=True
                    ),
                ]
            )
        )
        assert rows[0].reclaimable_member_ids == ["ws-01-2", "ws-01-3"]
        assert rows[0].retry_lost is True

    def test_set_with_nothing_reclaimable(self):
        rows = render.pool_set_rows(_pool_payload())
        assert rows[0].reclaimable_member_ids == []
        assert rows[0].retry_lost is False

    def test_empty_and_missing_payload(self):
        assert render.pool_set_rows(None) == []
        assert render.pool_set_rows({}) == []
        assert render.pool_set_rows({"sets": []}) == []


class TestPoolSummaryLine:
    def test_counts_sets_workspaces_and_statuses(self):
        line = render.pool_summary_line(
            _pool_payload(
                allocatable=False,
                members=[
                    _pool_member("ws-01-1"),
                    _pool_member("ws-01-2", status="allocated"),
                    _pool_member("ws-01-3", status="stuck"),
                ],
            )
        )
        assert "1 set(s), 3 workspace(s)" in line
        assert "0 ready to allocate" in line
        assert "allocated 1" in line
        assert "available 1" in line
        assert "stuck 1" in line

    def test_empty_pool_message(self):
        assert render.pool_summary_line(None) == "No workspaces in the pool."
        assert render.pool_summary_line({"sets": []}) == "No workspaces in the pool."


class TestReclaimConfirmMessage:
    """The confirm dialog must be accurate about what is and is not destroyed."""

    def test_workspace_message_names_the_workspace(self):
        rows = render.pool_set_rows(
            _pool_payload(members=[_pool_member("ws-01-1", status="cleaning", reclaimable=True)])
        )
        msg = render.reclaim_confirm_message(rows[0].members[0])
        assert "Reclaim ws-01-1?" in msg
        # Reassurance is part of the contract: code is archived, not lost.
        assert "archived to its generation branch" in msg
        assert "WARNING" not in msg

    def test_set_message_lists_only_reclaimable_members(self):
        rows = render.pool_set_rows(
            _pool_payload(
                members=[
                    _pool_member("ws-01-1"),
                    _pool_member("ws-01-2", status="cleaning", reclaimable=True),
                    _pool_member("ws-01-3", status="stuck", reclaimable=True),
                ]
            )
        )
        msg = render.reclaim_confirm_message(rows[0])
        assert "2 workspace(s): ws-01-2, ws-01-3" in msg
        assert "ws-01-1" not in msg

    def test_retry_loss_is_warned_about(self):
        rows = render.pool_set_rows(
            _pool_payload(
                members=[
                    _pool_member(
                        "ws-01-1", status="allocated", reclaimable=True, retry_lost_on_reclaim=True
                    )
                ]
            )
        )
        assert "could still be retried" in render.reclaim_confirm_message(rows[0])
        assert "could still be retried" in render.reclaim_confirm_message(rows[0].members[0])


class TestReclaimResultSummary:
    def test_lists_every_member_outcome(self):
        summary = render.reclaim_result_summary(
            {
                "total": 2,
                "success": 1,
                "failed": 1,
                "details": [
                    {"workspace_id": "ws-01-1", "action": "force_clean", "success": True, "message": "ok"},
                    {
                        "workspace_id": "ws-01-2",
                        "action": "blocked",
                        "success": False,
                        "message": "gen_x is running",
                    },
                ],
            }
        )
        assert "1 reclaimed, 1 not reclaimed." in summary
        assert "OK   ws-01-1 [force_clean] — ok" in summary
        assert "FAIL ws-01-2 [blocked] — gen_x is running" in summary

    def test_empty_and_missing(self):
        assert render.reclaim_result_summary(None) == "No response from the server."
        assert (
            render.reclaim_result_summary({"total": 0, "success": 0, "failed": 0, "details": []})
            == "Nothing was reclaimed — no matching workspaces."
        )


class TestRemovability:
    """Shrink eligibility must mirror the backend precondition exactly."""

    def test_clean_available_is_removable(self):
        rows = render.pool_set_rows(_pool_payload())
        assert rows[0].removable is True
        assert all(m.removable for m in rows[0].members)

    def test_unverified_available_is_not_removable(self):
        """The verdict and its wording both come from the backend."""
        rows = render.pool_set_rows(
            _pool_payload(
                members=[
                    _pool_member(
                        "ws-01-1",
                        clean_verified=False,
                        removable=False,
                        not_removable_reason="ws-01-1 is not clean-verified",
                    )
                ]
            )
        )
        assert rows[0].members[0].removable is False
        assert rows[0].members[0].not_removable_reason == "ws-01-1 is not clean-verified"
        assert rows[0].removable is False

    def test_busy_member_blocks_the_set(self):
        rows = render.pool_set_rows(
            _pool_payload(
                members=[
                    _pool_member("ws-01-1"),
                    _pool_member(
                        "ws-01-2",
                        status="allocated",
                        clean_verified=False,
                        removable=False,
                        not_removable_reason="ws-01-2 is allocated",
                    ),
                ]
            )
        )
        assert rows[0].removable is False
        assert rows[0].members[1].not_removable_reason == "ws-01-2 is allocated"

    def test_empty_set_is_not_removable(self):
        rows = render.pool_set_rows(_pool_payload(members=[]))
        assert rows[0].removable is False


class TestShrinkMessages:
    def test_confirm_message_promises_the_repos_survive(self):
        """The single most important reassurance — 'remove' must not read as 'delete'."""
        rows = render.pool_set_rows(_pool_payload())
        msg = render.shrink_confirm_message(rows[0])
        assert "Remove Set 01 from the pool" in msg
        assert "NOT deleted" in msg
        assert "archived generation branch stays" in msg
        assert "ws-01-1, ws-01-2, ws-01-3" in msg

    def test_blocked_message_lists_each_blocker_and_the_remedy(self):
        rows = render.pool_set_rows(
            _pool_payload(
                members=[
                    _pool_member("ws-01-1"),
                    _pool_member(
                        "ws-01-2",
                        status="cleaning",
                        clean_verified=False,
                        removable=False,
                        not_removable_reason="ws-01-2 is cleaning",
                    ),
                    _pool_member(
                        "ws-01-3",
                        clean_verified=False,
                        removable=False,
                        not_removable_reason="ws-01-3 is not clean-verified",
                    ),
                ]
            )
        )
        msg = render.shrink_blocked_message(rows[0])
        assert "ws-01-2 is cleaning" in msg
        assert "ws-01-3 is not clean-verified" in msg
        assert "ws-01-1" not in msg
        assert "Reclaim it first (c)" in msg

    def test_result_summary(self):
        summary = render.shrink_result_summary(
            {
                "total": 2,
                "success": 1,
                "failed": 1,
                "details": [
                    {"workspace_id": "ws-01-1", "success": True, "message": "Removed"},
                    {"workspace_id": "ws-01-2", "success": False, "message": "reclaim it first"},
                ],
            }
        )
        assert "1 removed, 1 not removed." in summary
        assert "OK   ws-01-1 — Removed" in summary
        assert "FAIL ws-01-2 — reclaim it first" in summary

    def test_empty_and_missing(self):
        assert render.shrink_result_summary(None) == "No response from the server."
        assert (
            render.shrink_result_summary({"total": 0, "success": 0, "failed": 0, "details": []})
            == "Nothing was removed — no matching workspaces."
        )
