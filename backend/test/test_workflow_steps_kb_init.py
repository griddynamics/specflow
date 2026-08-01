"""Unit tests for run_kb_init_agent workflow step (provisioned Rosetta plugin)."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.core.config import Settings
from app.schemas.llm_tier import WorkflowName
from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
from app.schemas.workspace import WorkspaceSettings
from app.services.workflow_steps import (
    WorkflowContext,
    run_kb_init_agent,
    _generate_mock_kb_init_artifacts,
)
from app.services.workspace_manager import WorkspaceManager


def _make_ctx(
    workspace_root: str = "/workspaces/ws1",
    rosetta_plugin_path: str | None = None,
    outputs_dir: str = "docs",
) -> WorkflowContext:
    """Build a minimal WorkflowContext for testing plugin availability."""
    settings = Mock(spec=Settings)
    settings.ROSETTA_PLUGIN_PATH = rosetta_plugin_path
    settings.STANDARDS_DIR_NAME = "standards"
    settings.LLM_HIGH = "anthropic/claude-opus-4.5"
    settings.LLM_MEDIUM = "anthropic/claude-sonnet-4.6"
    settings.LLM_LOW = "anthropic/claude-haiku-4.5"

    primary_ws = Mock(spec=WorkspaceSettings)
    primary_ws.workspace_path = Mock()
    primary_ws.workspace_path.name = "ws1"
    primary_ws.get_isolated_root.return_value = workspace_root

    logger = Mock()
    manager = Mock(spec=WorkspaceManager)

    return WorkflowContext(
        request=Mock(generation_id="est-123", outputs_dir=outputs_dir),
        settings=settings,
        logger=logger,
        workspace_ids=["ws1"],
        workspaces=[primary_ws],
        workspace_manager=manager,
        primary_workspace=primary_ws,
        llm_overrides={},
    )


class TestCollectProvisionedPluginManifest:
    """Tests for _collect_provisioned_plugin_manifest — reports only what KB init produced."""

    def test_excludes_pre_existing_uploads_and_includes_root_claude_md(self, tmp_path: Path) -> None:
        """outputs_dir holds uploaded specs/plans before KB init; only new files + root CLAUDE.md count."""
        from app.services.workflow_steps import (
            _collect_provisioned_plugin_manifest,
            _snapshot_outputs_dir_files,
        )

        ws = tmp_path
        docs = ws / "docs"
        (docs / "analysis").mkdir(parents=True)
        (docs / "analysis" / "specification_completeness.md").write_text("uploaded")
        (docs / "planning").mkdir()
        (docs / "planning" / "IMPLEMENTATION_PLAN.md").write_text("uploaded")

        # Snapshot what exists *before* KB init runs.
        pre_existing = _snapshot_outputs_dir_files(str(ws), "docs")

        # KB init then writes new docs + a root CLAUDE.md.
        (docs / "CONTEXT.md").write_text("kb")
        (docs / "ARCHITECTURE.md").write_text("kb")
        (ws / "CLAUDE.md").write_text("kb root")

        result = _collect_provisioned_plugin_manifest(str(ws), "docs", pre_existing)

        assert result == [
            "CLAUDE.md",
            "docs/ARCHITECTURE.md",
            "docs/CONTEXT.md",
        ]
        # The pre-existing uploads must NOT be reported as KB output.
        assert "docs/analysis/specification_completeness.md" not in result
        assert "docs/planning/IMPLEMENTATION_PLAN.md" not in result


class TestRunKbInitAgent:
    """Tests for run_kb_init_agent step function."""

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    @patch("app.services.workflow_steps.telemetry")
    async def test_skips_when_no_plugin_configured(
        self, mock_telemetry: Mock, mock_agent_query: AsyncMock,
    ) -> None:
        """Scenario: no ROSETTA_PLUGIN_PATH on disk -> logs skip, emits skipped event, no agent."""
        ctx = _make_ctx(rosetta_plugin_path=None)

        await run_kb_init_agent(ctx)

        ctx.logger.info.assert_any_call(
            "KB init skipped: ROSETTA_PLUGIN_PATH is not configured"
        )
        mock_agent_query.assert_not_called()
        mock_telemetry.capture_kb_init_event.assert_called_once_with(
            generation_id="est-123",
            workspace_name="ws1",
            status="skipped",
            generated_files=[],
        )
        mock_telemetry.capture_kb_init_triggered.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    @patch("app.services.workflow_steps.telemetry")
    async def test_skips_when_plugin_path_is_missing(
        self,
        mock_telemetry: Mock,
        mock_agent_query: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Scenario: configured plugin directory is missing -> skip with an accurate reason."""
        missing_path = tmp_path / "missing-plugin"
        ctx = _make_ctx(rosetta_plugin_path=str(missing_path))

        await run_kb_init_agent(ctx)

        ctx.logger.info.assert_any_call(
            f"KB init skipped: ROSETTA_PLUGIN_PATH is not an existing directory: {missing_path}"
        )
        mock_agent_query.assert_not_called()
        mock_telemetry.capture_kb_init_event.assert_called_once_with(
            generation_id="est-123",
            workspace_name="ws1",
            status="skipped",
            generated_files=[],
        )

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.TelemetryContext")
    @patch("app.services.workflow_steps.telemetry")
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    @patch("app.services.workflow_steps.create_agent_logger")
    async def test_provisions_plugin_and_runs_agent_without_mcp(
        self,
        mock_create_logger: Mock,
        mock_agent_query: AsyncMock,
        mock_telemetry: Mock,
        mock_telemetry_ctx: Mock,
        tmp_path: Path,
    ) -> None:
        """Scenario: ROSETTA_PLUGIN_PATH points at an on-disk plugin.

        The bundled plugin is provisioned into the primary workspace (so its skills are
        discoverable via project scope), and agent_query receives the plugin entry-point tools
        with an empty optional-MCP configuration.
        """
        plugin_dir = tmp_path / "rosetta-plugin"
        plugin_dir.mkdir()
        ctx = _make_ctx(rosetta_plugin_path=str(plugin_dir))
        mock_create_logger.return_value = Mock()
        mock_agent_query.return_value = Mock(result="ok", session_id="s1")

        await run_kb_init_agent(ctx)

        # Plugin copied into the primary workspace before the agent runs.
        ctx.workspace_manager.provision_rosetta_plugin.assert_called_once_with(
            ctx.primary_workspace
        )
        mock_agent_query.assert_awaited_once()
        call_kwargs = mock_agent_query.call_args.kwargs
        assert not call_kwargs["mcp_servers"]
        assert {"Skill", "SlashCommand"} <= set(call_kwargs["allowed_tools"])
        assert "## Rosetta Plugin Usage" in call_kwargs["system_prompt"]

        # Regression: the workspace name MUST be set on TelemetryContext before the agent runs,
        # otherwise build_stream_publisher_from_context() returns None and the TUI live-message
        # stream is empty for the entire KB-init step.
        mock_telemetry_ctx.set_workspace_name.assert_called_once_with("ws1")
        mock_telemetry_ctx.set_workflow.assert_called_once_with(
            TelemetryWorkflowLabel.plain(WorkflowName.KB_INIT)
        )
        mock_telemetry.capture_kb_init_triggered.assert_called_once()
        event_kwargs = mock_telemetry.capture_kb_init_event.call_args.kwargs
        assert event_kwargs["status"] == "success"

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.telemetry")
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    @patch("app.services.workflow_steps.create_agent_logger")
    async def test_catches_exceptions_and_emits_failed_event(
        self,
        mock_create_logger: Mock,
        mock_agent_query: AsyncMock,
        mock_telemetry: Mock,
        tmp_path: Path,
    ) -> None:
        """Scenario: agent_query raises -> exception caught, failed event emitted."""
        plugin_dir = tmp_path / "rosetta-plugin"
        plugin_dir.mkdir()
        ctx = _make_ctx(rosetta_plugin_path=str(plugin_dir))
        mock_create_logger.return_value = Mock()
        mock_agent_query.side_effect = RuntimeError("agent run failed")

        await run_kb_init_agent(ctx)

        mock_telemetry.capture_kb_init_triggered.assert_called_once_with(
            generation_id="est-123",
            workspace_name="ws1",
        )
        ctx.logger.error.assert_called_once()
        error_msg = ctx.logger.error.call_args[0][0]
        assert "non-fatal" in error_msg

        mock_telemetry.capture_kb_init_event.assert_called_once()
        event_kwargs = mock_telemetry.capture_kb_init_event.call_args.kwargs
        assert event_kwargs["status"] == "failed"
        assert "agent run failed" in event_kwargs["error_message"]

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.TelemetryContext")
    @patch("app.services.workflow_steps.telemetry")
    @patch("app.services.workflow_steps.is_skip_mode_enabled", return_value=True)
    async def test_skip_mode_creates_mock_artifacts_and_returns(
        self,
        _mock_skip: Mock,
        mock_telemetry: Mock,
        mock_telemetry_ctx: Mock,
        tmp_path: Path,
    ) -> None:
        """Scenario: SKIP_MODE enabled -> mock artifacts written to final locations, no agent_query.

        The mock mirrors the real plugin path: CLAUDE.md at the workspace root and a doc under
        outputs_dir (here the default ``docs/``). The plugin owns ``.claude/`` and is provisioned
        later in prepare_parallel_workspaces.
        """
        plugin_dir = tmp_path / "rosetta-plugin"
        plugin_dir.mkdir()
        ws_root = tmp_path / "ws"
        ws_root.mkdir()
        ctx = _make_ctx(workspace_root=str(ws_root), rosetta_plugin_path=str(plugin_dir))

        await run_kb_init_agent(ctx)

        assert (ws_root / "CLAUDE.md").exists()
        assert (ws_root / "docs" / "CONTEXT.md").exists()

        ctx.logger.warning.assert_any_call(
            "[SKIP_MODE] KB init agent skipped — created 2 mock KB artifacts"
        )
        mock_telemetry_ctx.set_workflow.assert_called_once_with(
            TelemetryWorkflowLabel.plain(WorkflowName.KB_INIT)
        )
        mock_telemetry.capture_kb_init_triggered.assert_not_called()
        mock_telemetry.capture_kb_init_event.assert_called_once()
        event_kwargs = mock_telemetry.capture_kb_init_event.call_args.kwargs
        assert event_kwargs["status"] == "success"
        assert set(event_kwargs["generated_files"]) == {"CLAUDE.md", "docs/CONTEXT.md"}
        assert event_kwargs["duration_seconds"] == 0.0


class TestGenerateMockKbInitArtifacts:
    """Tests for _generate_mock_kb_init_artifacts helper (final-location output)."""

    def test_creates_expected_files(self, tmp_path: Path) -> None:
        logger = Mock()
        _generate_mock_kb_init_artifacts(str(tmp_path), "docs", logger)

        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / "docs" / "CONTEXT.md").exists()
        assert "[SKIP_MODE]" in (tmp_path / "CLAUDE.md").read_text()
        # The plugin owns .claude/ — the mock must not write there.
        assert not (tmp_path / ".claude").exists()

    def test_honors_custom_outputs_dir(self, tmp_path: Path) -> None:
        logger = Mock()
        _generate_mock_kb_init_artifacts(str(tmp_path), "specflow", logger)

        assert (tmp_path / "CLAUDE.md").exists()
        assert (tmp_path / "specflow" / "CONTEXT.md").exists()

    def test_idempotent_on_existing_dir(self, tmp_path: Path) -> None:
        logger = Mock()
        _generate_mock_kb_init_artifacts(str(tmp_path), "docs", logger)
        _generate_mock_kb_init_artifacts(str(tmp_path), "docs", logger)

        assert (tmp_path / "CLAUDE.md").exists()
