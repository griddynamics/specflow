"""
Tests for _phase_label — the human-readable phase computation in check_status.

Covers all GenerationStatus branches and the checkpoint label lookup.
"""

from server import _phase_label


class TestInitializingStatus:
    def test_initializing_returns_allocating(self):
        assert _phase_label("initializing", None) == "Allocating workspaces"

    def test_initializing_ignores_checkpoint(self):
        assert _phase_label("initializing", "files_uploaded") == "Allocating workspaces"


class TestCompletedStatus:
    def test_completed_returns_done(self):
        assert _phase_label("completed", "estimation_done") == "Done — outputs ready"

    def test_completed_ignores_checkpoint(self):
        assert _phase_label("completed", None) == "Done — outputs ready"


class TestFailedStatus:
    def test_failed_returns_failed(self):
        assert _phase_label("failed", "generation_done") == "Failed"

    def test_failed_ignores_checkpoint(self):
        assert _phase_label("failed", None) == "Failed"


class TestRunningStatus:
    def test_running_files_uploaded(self):
        assert _phase_label("running", "files_uploaded") == "Files received"

    def test_running_contract_validated(self):
        assert _phase_label("running", "contract_validated") == "Files validated — preparing agents"

    def test_running_kb_init_done(self):
        assert _phase_label("running", "kb_init_done") == "Knowledge base initialized"

    def test_running_generation_started(self):
        assert _phase_label("running", "generation_started") == "Generating code"

    def test_running_generation_done(self):
        assert _phase_label("running", "generation_done") == "Code generated — deploying"

    def test_running_deploy_and_e2e_done(self):
        assert _phase_label("running", "deploy_and_e2e_done") == "Deploy and E2E complete"

    def test_running_outputs_archived(self):
        assert _phase_label("running", "outputs_archived") == "Outputs archived"

    def test_running_estimation_done(self):
        assert _phase_label("running", "estimation_done") == "Estimation complete"

    def test_running_unknown_checkpoint_fallback(self):
        assert _phase_label("running", "some_future_checkpoint") == "Running"

    def test_running_none_checkpoint_fallback(self):
        assert _phase_label("running", None) == "Running"


class TestFallbacks:
    def test_pending_fallback(self):
        assert _phase_label("pending", None) == "Status: pending"

    def test_unknown_status_fallback(self):
        assert _phase_label("some_future_status", "some_checkpoint") == "Status: some_future_status"


class TestStatusChatMessageAgentWarnings:
    """RUNNING message mentions agent warnings when the payload carries events."""

    def _event(self, ws="ws-01-1", phase=12):
        return {"workspace_id": ws, "phase": phase, "kind": "agent_crash", "message": "x"}

    def test_running_with_events_mentions_count_and_latest(self):
        from server import _status_chat_message

        msg = _status_chat_message(
            {
                "status": "running",
                "checkpoint": "generation_started",
                "agent_error_events": [self._event(), self._event(ws="ws-01-2", phase=3)],
            }
        )
        assert "2 agent warning(s)" in msg
        assert "ws-01-2, phase 3" in msg
        assert "retrying automatically" in msg

    def test_running_without_events_unchanged(self):
        from server import _status_chat_message

        msg = _status_chat_message({"status": "running", "checkpoint": "generation_started"})
        assert "warning" not in msg.lower()
        assert "in progress" in msg

    def test_failed_message_does_not_add_warning_clause(self):
        from server import _status_chat_message

        msg = _status_chat_message(
            {"status": "failed", "error": "boom", "agent_error_events": [self._event()]}
        )
        assert "warning" not in msg.lower()
