"""Unit tests for MCP configuration properties on existing PostHog events."""

from unittest.mock import Mock

from app.core.mcp_config import EnabledMcpsResolution
from app.core.telemetry_context import TelemetryContext
from app.services.telemetry import PostHogTelemetry


def _make_telemetry() -> PostHogTelemetry:
    t = PostHogTelemetry.__new__(PostHogTelemetry)
    t._client = Mock()
    t._initialized = True
    return t


class TestMcpPropsInTelemetryContext:
    def setup_method(self) -> None:
        TelemetryContext.clear_context()

    def teardown_method(self) -> None:
        TelemetryContext.clear_context()

    def test_set_mcp_resolution_stores_props(self) -> None:
        res = EnabledMcpsResolution(
            enabled=frozenset({"playwright"}),
            source="form",
            raw_request_string="playwright,unknown",
        )
        TelemetryContext.set_mcp_resolution(res)
        props = TelemetryContext.get_mcp_props()
        assert props["mcp_configuration_source"] == "form"
        assert props["mcps_user_requested"] == "playwright,unknown"
        assert props["mcp_enabled_resolved_canonical"] == "playwright"
        assert props["mcps_used"] == ["playwright"]

    def test_get_mcp_props_empty_when_not_set(self) -> None:
        assert TelemetryContext.get_mcp_props() == {}

    def test_set_mcp_spec_prune_result_merges_into_mcp_props(self) -> None:
        TelemetryContext.set_mcp_spec_prune_result("figma,playwright", "figma", '{"figma":"evidence"}')
        props = TelemetryContext.get_mcp_props()
        assert props["mcp_spec_prune_before"] == "figma,playwright"
        assert props["mcp_spec_prune_after"] == "figma"
        assert "evidence" in props["mcp_spec_prune_reasons"]

    def test_set_mcp_resolution_preserves_existing_context(self) -> None:
        TelemetryContext.set_user_context(user_email="u@example.com", generation_id="est-1")
        res = EnabledMcpsResolution(
            enabled=frozenset({"figma"}),
            source="backend_settings",
            raw_request_string="figma",
        )
        TelemetryContext.set_mcp_resolution(res)
        assert TelemetryContext.get_user_email() == "u@example.com"
        assert TelemetryContext.get_generation_id() == "est-1"
        assert TelemetryContext.get_mcp_props()["mcp_configuration_source"] == "backend_settings"


class TestMcpPropsOnPlanningEvent:
    def setup_method(self) -> None:
        TelemetryContext.clear_context()

    def teardown_method(self) -> None:
        TelemetryContext.clear_context()

    def test_planning_event_includes_mcp_props_from_context(self) -> None:
        t = _make_telemetry()
        res = EnabledMcpsResolution(
            enabled=frozenset({"playwright"}),
            source="form",
            raw_request_string="playwright",
        )
        TelemetryContext.set_mcp_resolution(res)

        t.capture_planning_event(
            event="planning_triggered",
            generation_id="est-1",
            workspace_name="ws-abc",
        )

        t._client.capture.assert_called_once()
        props = t._client.capture.call_args.kwargs["properties"]
        assert props["mcp_configuration_source"] == "form"
        assert props["mcps_used"] == ["playwright"]
        assert props["mcp_enabled_resolved_canonical"] == "playwright"
        assert props["duration_seconds"] == 0.0

    def test_planning_event_omits_mcp_props_when_not_set(self) -> None:
        t = _make_telemetry()
        t.capture_planning_event(
            event="planning_triggered",
            generation_id="est-2",
            workspace_name="ws-xyz",
        )
        props = t._client.capture.call_args.kwargs["properties"]
        assert "mcp_configuration_source" not in props
        assert "mcps_used" not in props

    def test_noop_when_disabled(self) -> None:
        t = PostHogTelemetry.__new__(PostHogTelemetry)
        t._client = None
        t._initialized = False
        res = EnabledMcpsResolution(
            enabled=frozenset(),
            source="backend_settings",
            raw_request_string="",
        )
        TelemetryContext.set_mcp_resolution(res)
        # Must not raise
        t.capture_planning_event(
            event="planning_triggered",
            generation_id="est-3",
            workspace_name="ws",
        )
