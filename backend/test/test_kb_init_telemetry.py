"""Unit tests for KB init telemetry event."""

from unittest.mock import Mock, patch

from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
from app.services.telemetry import PostHogTelemetry


class TestCaptureKbInitEvent:
    """Tests for PostHogTelemetry.capture_kb_init_event()."""

    def _make_telemetry(self) -> PostHogTelemetry:
        """Create a telemetry instance with a mocked client."""
        t = PostHogTelemetry.__new__(PostHogTelemetry)
        t._client = Mock()
        t._initialized = True
        return t

    @patch("app.services.telemetry.TelemetryContext")
    def test_emits_success_event(self, mock_ctx: Mock) -> None:
        """Scenario: success -> event has status, files, duration."""
        mock_ctx.get_user_email.return_value = "user@test.com"
        mock_ctx.get_workflow.return_value = TelemetryWorkflowLabel.plain("kb_init")

        t = self._make_telemetry()
        t.capture_kb_init_event(
            generation_id="est-1",
            workspace_name="ws1",
            status="success",
            generated_files=["rosetta/CLAUDE.md", "rosetta/docs/CONTEXT.md"],
            duration_seconds=45.3,
        )

        t._client.capture.assert_called_once()
        call_kwargs = t._client.capture.call_args.kwargs
        assert call_kwargs["distinct_id"] == "user@test.com"
        assert call_kwargs["event"] == "kb_init_completed"

        props = call_kwargs["properties"]
        assert props["status"] == "success"
        assert props["generation_id"] == "est-1"
        assert props["generated_files_count"] == 2
        assert props["duration_seconds"] == 45.3
        assert "rosetta/CLAUDE.md" in props["generated_files"]
        assert "error_message" not in props

    @patch("app.services.telemetry.TelemetryContext")
    def test_emits_failed_event_with_error(self, mock_ctx: Mock) -> None:
        """Scenario: failure -> event has error_message, truncated to 500 chars."""
        mock_ctx.get_user_email.return_value = "user@test.com"
        mock_ctx.get_workflow.return_value = None

        t = self._make_telemetry()
        long_error = "x" * 1000
        t.capture_kb_init_event(
            generation_id="est-2",
            workspace_name="ws2",
            status="failed",
            generated_files=[],
            duration_seconds=2.1,
            error_message=long_error,
        )

        props = t._client.capture.call_args.kwargs["properties"]
        assert props["status"] == "failed"
        assert props["generated_files_count"] == 0
        assert len(props["error_message"]) == 500

    @patch("app.services.telemetry.TelemetryContext")
    def test_emits_skipped_event(self, mock_ctx: Mock) -> None:
        """Scenario: skipped -> empty file list, no error."""
        mock_ctx.get_user_email.return_value = "anon@test.com"
        mock_ctx.get_workflow.return_value = None

        t = self._make_telemetry()
        t.capture_kb_init_event(
            generation_id="est-3",
            workspace_name="ws3",
            status="skipped",
            generated_files=[],
        )

        props = t._client.capture.call_args.kwargs["properties"]
        assert props["status"] == "skipped"
        assert props["generated_files"] == []
        assert props["duration_seconds"] == 0

    def test_noop_when_disabled(self) -> None:
        """Scenario: telemetry disabled -> no capture call."""
        t = PostHogTelemetry.__new__(PostHogTelemetry)
        t._client = None
        t._initialized = False

        t.capture_kb_init_event(
            generation_id="est-4",
            workspace_name="ws4",
            status="success",
            generated_files=["rosetta/CLAUDE.md"],
        )
