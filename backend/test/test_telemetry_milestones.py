"""Unit tests for PostHog milestone pairs (triggered vs completed)."""

from unittest.mock import Mock, patch

import pytest

from app.services.telemetry import PostHogTelemetry


def _enabled_telemetry() -> PostHogTelemetry:
    t = PostHogTelemetry.__new__(PostHogTelemetry)
    t._client = Mock()
    t._initialized = True
    return t


@patch("app.services.telemetry.TelemetryContext")
def test_capture_spec_check_triggered(mock_ctx: Mock) -> None:
    mock_ctx.get_mcp_props.return_value = {}
    t = _enabled_telemetry()
    t.capture_spec_check_triggered(
        generation_id="e1",
        workspace_id="w1",
        user_email="u@example.com",
    )
    mock_ctx.get_user_email.assert_not_called()
    kw = t._client.capture.call_args.kwargs
    assert kw["distinct_id"] == "u@example.com"
    assert kw["event"] == "spec_check_triggered"
    assert kw["properties"]["generation_id"] == "e1"
    assert kw["properties"]["workspace_id"] == "w1"


@patch("app.services.telemetry.TelemetryContext")
def test_capture_workspace_coding_completed(mock_ctx: Mock) -> None:
    mock_ctx.get_user_email.return_value = "dev@example.com"
    mock_ctx.get_workflow.return_value = None
    mock_ctx.get_mcp_props.return_value = {}
    t = _enabled_telemetry()
    t.capture_workspace_coding_completed(
        generation_id="e2",
        workspace_ids=["a", "b"],
        duration_seconds=123.456,
    )
    kw = t._client.capture.call_args.kwargs
    assert kw["event"] == "workspace_coding_completed"
    props = kw["properties"]
    assert props["workspace_count"] == 2
    assert props["duration_seconds"] == 123.46
    assert props["workspace_ids"] == ["a", "b"]


# ---------------------------------------------------------------------------
# Blank-config no-op regression tests for PostHog (FR-10 / S4.1 insurance)
# ---------------------------------------------------------------------------

def test_posthog_disabled_flag_no_network_call_no_error() -> None:
    """POSTHOG_ENABLED=False → PostHogTelemetry._client stays None; capture_event is a no-op."""
    with patch("app.services.telemetry.settings") as mock_settings:
        mock_settings.POSTHOG_ENABLED = False
        mock_settings.POSTHOG_API_KEY = None
        t = PostHogTelemetry()
    assert t._client is None
    assert t.is_enabled() is False
    # capture_event must not raise when disabled
    t.capture_event("u@example.com", "any_event", {})


def test_posthog_enabled_but_no_api_key_no_network_call_no_error() -> None:
    """POSTHOG_ENABLED=True but blank API key → client stays None; no error."""
    with patch("app.services.telemetry.settings") as mock_settings:
        mock_settings.POSTHOG_ENABLED = True
        mock_settings.POSTHOG_API_KEY = None
        t = PostHogTelemetry()
    assert t._client is None
    assert t.is_enabled() is False
    t.capture_event("u@example.com", "any_event", {})


@pytest.mark.parametrize(
    "method,kwargs",
    [
        ("capture_spec_check_triggered", {"generation_id": "g1", "workspace_id": "w1", "user_email": "u@x.com"}),
        ("capture_kb_init_triggered", {"generation_id": "g1", "workspace_name": "ws"}),
    ],
    ids=["spec_check_triggered", "kb_init_triggered"],
)
def test_posthog_disabled_milestone_methods_are_noop(method: str, kwargs: dict) -> None:
    """All milestone capture methods silently no-op when PostHog is disabled (no error)."""
    t = PostHogTelemetry.__new__(PostHogTelemetry)
    t._client = None
    t._initialized = False
    getattr(t, method)(**kwargs)
