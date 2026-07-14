"""Tests for the TUI poller and milestone tracker (tui/poller.py)."""

from unittest.mock import AsyncMock, patch

import pytest

from tui.poller import MilestoneTracker, fire_milestones, poll_once


class TestMilestoneTracker:
    def test_first_pending_poll_announces_start(self):
        # A freshly queued/started run announces "started" on first observation.
        tracker = MilestoneTracker("gen_abc123def456")
        out = tracker.process({"status": "pending", "checkpoint": ""})
        assert len(out) == 1
        assert "started" in out[0].title

    def test_attaching_to_running_is_silent_baseline(self):
        # Attaching to an already-running session must not replay a "started"
        # ping — the first observed "running" poll is a silent baseline.
        tracker = MilestoneTracker("gen_abc123def456")
        assert tracker.process({"status": "running", "checkpoint": "kb_init_done"}) == []

    def test_checkpoint_advance_fires_once(self):
        tracker = MilestoneTracker("gen_abc123def456")
        # Baseline (already running on attach) is silent.
        assert tracker.process({"status": "running", "checkpoint": "kb_init_done"}) == []
        out = tracker.process(
            {"status": "running", "checkpoint": "generation_started", "current_phase": "Generating"}
        )
        assert len(out) == 1
        assert "Generating" in out[0].message
        # Same checkpoint again → no repeat.
        assert tracker.process({"status": "running", "checkpoint": "generation_started"}) == []

    def test_completed_fires_exactly_once(self):
        tracker = MilestoneTracker("gen_abc123def456")
        tracker.process({"status": "running", "checkpoint": "estimation_done"})
        first = tracker.process({"status": "completed", "checkpoint": "estimation_done"})
        assert len(first) == 1
        assert "completed" in first[0].message
        assert tracker.process({"status": "completed", "checkpoint": "estimation_done"}) == []

    def test_failed_fires(self):
        tracker = MilestoneTracker("gen_abc123def456")
        tracker.process({"status": "running", "checkpoint": "generation_started"})
        out = tracker.process({"status": "failed", "checkpoint": "generation_started"})
        assert any("failed" in m.message for m in out)

    def test_cancelled_fires_neutrally_once(self):
        # A user cancellation is announced as "cancelled", never as a failure.
        tracker = MilestoneTracker("gen_abc123def456")
        tracker.process({"status": "running", "checkpoint": "generation_started"})
        out = tracker.process({"status": "cancelled", "checkpoint": "generation_started"})
        assert len(out) == 1
        assert "cancelled" in out[0].title.lower()
        assert "cancelled" in out[0].message
        assert not any("failed" in m.message for m in out)
        # Terminal — fires exactly once.
        assert tracker.process({"status": "cancelled", "checkpoint": "generation_started"}) == []

    def test_workspace_phase_progress_fires_once_per_change(self):
        tracker = MilestoneTracker("gen_abc123def456")
        tracker.process(
            {
                "status": "running",
                "checkpoint": "generation_started",
                "workspace_phases": {
                    "ws-01-1": {"last_completed_phase": 1, "phase_name": "Auth"},
                },
            }
        )

        out = tracker.process(
            {
                "status": "running",
                "checkpoint": "generation_started",
                "workspace_phases": {
                    "ws-01-1": {"last_completed_phase": 2, "phase_name": "Payments"},
                },
            }
        )

        assert len(out) == 1
        assert "ws-01-1" in out[0].message
        assert "Payments" in out[0].message
        assert (
            tracker.process(
                {
                    "status": "running",
                    "checkpoint": "generation_started",
                    "workspace_phases": {
                        "ws-01-1": {"last_completed_phase": 2, "phase_name": "Payments"},
                    },
                }
            )
            == []
        )

    def test_kb_init_phase_label_change_without_progress_is_silent(self):
        tracker = MilestoneTracker("gen_abc123def456")
        tracker.process(
            {
                "status": "running",
                "checkpoint": "generation_started",
                "workspace_phases": {
                    "ws-01-1": {
                        "last_completed_phase": 0,
                        "phase_name": "Knowledge Base Initialization with Rosetta",
                    },
                },
            }
        )

        assert (
            tracker.process(
                {
                    "status": "running",
                    "checkpoint": "generation_started",
                    "workspace_phases": {
                        "ws-01-1": {"last_completed_phase": 0, "phase_name": ""},
                    },
                }
            )
            == []
        )

    def test_none_payload_is_noop(self):
        assert MilestoneTracker("gen_x").process(None) == []


class TestFireMilestones:
    def test_calls_notify_desktop_per_milestone(self):
        from tui.poller import Milestone

        with patch("tui.poller.notify_desktop") as notify:
            fire_milestones([Milestone("t1", "m1"), Milestone("t2", "m2")])
        assert notify.call_count == 2


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_delegates_to_check_status_safe(self):
        with patch(
            "tui.poller.check_status_safe", new=AsyncMock(return_value={"status": "running"})
        ) as m:
            result = await poll_once("gen_abc")
        assert result == {"status": "running"}
        m.assert_awaited_once_with("gen_abc")
