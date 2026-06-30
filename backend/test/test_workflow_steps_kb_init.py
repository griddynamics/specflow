"""Unit tests for run_kb_init_agent workflow step."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from app.core.config import ROSETTA_SERVER_KEY, Settings
from app.schemas.llm_tier import WorkflowName
from app.schemas.telemetry_workflow import TelemetryWorkflowLabel
from app.schemas.workspace import WorkspaceSettings
from app.services.workflow_steps import (
    WorkflowContext,
    run_kb_init_agent,
    _collect_rosetta_file_manifest,
    _generate_mock_rosetta_artifacts,
)
from app.services.workspace_manager import WorkspaceManager


def _make_ctx(
    rosetta_enabled: bool = False,
    workspace_root: str = "/workspaces/ws1",
    rosetta_plugin_path: str | None = None,
    outputs_dir: str = "docs",
) -> WorkflowContext:
    """Build a minimal WorkflowContext for testing."""
    settings = Mock(spec=Settings)
    settings.ROSETTA_MCP_ENABLED = rosetta_enabled
    settings.ROSETTA_MCP_COMMAND = "uvx"
    settings.ROSETTA_MCP_ARGS = "ims-mcp@latest"
    settings.ROSETTA_SERVER_URL = "https://ims.example.com/"
    settings.ROSETTA_API_KEY = None
    settings.ROSETTA_USER_EMAIL = "e@x.com"
    settings.ROSETTA_IMS_VERSION = "r2"
    settings.ROSETTA_OUTPUT_DIR = "rosetta"
    # Default None so disabled-MCP means "skip" (not provisioned-plugin) unless a test opts in.
    # When a test opts in it must point at a real directory: provisioned-plugin mode requires the
    # path to exist on disk (resolve_rosetta_kb_mode), matching the production gate.
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


class TestCollectRosettaFileManifest:
    """Tests for _collect_rosetta_file_manifest helper."""

    def test_returns_empty_when_dir_missing(self, tmp_path: Path) -> None:
        result = _collect_rosetta_file_manifest(str(tmp_path), "rosetta")
        assert result == []

    def test_collects_files_recursively(self, tmp_path: Path) -> None:
        rosetta = tmp_path / "rosetta"
        rosetta.mkdir()
        (rosetta / "CLAUDE.md").write_text("instructions")
        agents = rosetta / "agents"
        agents.mkdir(parents=True)
        (agents / "backend.md").write_text("agent")
        docs = rosetta / "docs"
        docs.mkdir()
        (docs / "CONTEXT.md").write_text("ctx")

        result = _collect_rosetta_file_manifest(str(tmp_path), "rosetta")

        assert "rosetta/CLAUDE.md" in result
        assert "rosetta/agents/backend.md" in result
        assert "rosetta/docs/CONTEXT.md" in result
        assert result == sorted(result)


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
    @patch("app.services.workflow_steps.telemetry")
    async def test_skips_when_disabled(self, mock_telemetry: Mock) -> None:
        """Scenario: ROSETTA_MCP_ENABLED=False -> logs skip, emits skipped event."""
        ctx = _make_ctx(rosetta_enabled=False)

        await run_kb_init_agent(ctx)

        ctx.logger.info.assert_any_call(
            "KB init skipped: Rosetta MCP disabled and no ROSETTA_PLUGIN_PATH configured"
        )
        mock_telemetry.capture_kb_init_event.assert_called_once_with(
            generation_id="est-123",
            workspace_name="ws1",
            status="skipped",
            generated_files=[],
        )
        mock_telemetry.capture_kb_init_triggered.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.TelemetryContext")
    @patch("app.services.workflow_steps.telemetry")
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    @patch("app.services.workflow_steps.create_agent_logger")
    @patch("app.services.workflow_steps._collect_rosetta_file_manifest")
    async def test_plugin_mode_provisions_plugin_and_runs_agent_without_mcp(
        self,
        mock_manifest: Mock,
        mock_create_logger: Mock,
        mock_agent_query: AsyncMock,
        mock_telemetry: Mock,
        mock_telemetry_ctx: Mock,
        tmp_path: Path,
    ) -> None:
        """Scenario: MCP disabled but ROSETTA_PLUGIN_PATH points at an on-disk plugin -> provisioned
        -plugin mode.

        The bundled plugin is provisioned into the primary workspace (so its skills are
        discoverable via project scope), agent_query is called with no MCP servers and no SDK
        ``plugins=`` param, and the prompt is rendered in plugin mode (no `mcp__KnowledgeBase__`).
        The plugin path must exist on disk — that is the gate the mode resolver enforces.
        """
        plugin_dir = tmp_path / "rosetta-plugin"
        plugin_dir.mkdir()
        ctx = _make_ctx(rosetta_enabled=False, rosetta_plugin_path=str(plugin_dir))
        mock_create_logger.return_value = Mock()
        mock_agent_query.return_value = Mock(result="ok", session_id="s1")
        mock_manifest.return_value = ["rosetta/CLAUDE.md"]

        await run_kb_init_agent(ctx)

        # Plugin copied into the primary workspace before the agent runs.
        ctx.workspace_manager.provision_rosetta_plugin.assert_called_once_with(
            ctx.primary_workspace
        )
        mock_agent_query.assert_awaited_once()
        call_kwargs = mock_agent_query.call_args.kwargs
        assert "plugins" not in call_kwargs  # SDK plugins= loader intentionally unused
        assert not call_kwargs["mcp_servers"]  # empty dict -> no MCP server attached
        assert "## Rosetta Plugin Usage" in call_kwargs["system_prompt"]
        assert "mcp__" not in call_kwargs["system_prompt"]

        mock_telemetry.capture_kb_init_triggered.assert_called_once()
        event_kwargs = mock_telemetry.capture_kb_init_event.call_args.kwargs
        assert event_kwargs["status"] == "success"

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.TelemetryContext")
    @patch("app.services.workflow_steps.telemetry")
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    @patch("app.services.workflow_steps.create_agent_logger")
    @patch("app.services.workflow_steps._collect_rosetta_file_manifest")
    async def test_calls_agent_and_emits_success_event(
        self,
        mock_manifest: Mock,
        mock_create_logger: Mock,
        mock_agent_query: AsyncMock,
        mock_telemetry: Mock,
        mock_telemetry_ctx: Mock,
    ) -> None:
        """Scenario: enabled, agent succeeds -> emits success event with file list."""
        ctx = _make_ctx(rosetta_enabled=True)
        mock_create_logger.return_value = Mock()
        mock_agent_query.return_value = Mock(result="ok", session_id="s1")
        mock_manifest.return_value = [
            "rosetta/CLAUDE.md",
            "rosetta/agents/backend.md",
        ]

        await run_kb_init_agent(ctx)

        mock_telemetry_ctx.set_workflow.assert_called_once_with(
            TelemetryWorkflowLabel.plain(WorkflowName.KB_INIT)
        )
        # Regression: the workspace name MUST be set on TelemetryContext before the
        # agent runs, otherwise build_stream_publisher_from_context() returns None
        # and the TUI live-message stream is empty for the entire KB-init step
        # (only generation_id is set elsewhere; both are required).
        mock_telemetry_ctx.set_workspace_name.assert_called_once_with("ws1")
        mock_agent_query.assert_awaited_once()
        call_kwargs = mock_agent_query.call_args.kwargs
        assert "mcp_servers" in call_kwargs
        assert ROSETTA_SERVER_KEY in call_kwargs["mcp_servers"]

        mock_telemetry.capture_kb_init_triggered.assert_called_once_with(
            generation_id="est-123",
            workspace_name="ws1",
        )
        mock_telemetry.capture_kb_init_event.assert_called_once()
        event_kwargs = mock_telemetry.capture_kb_init_event.call_args.kwargs
        assert event_kwargs["status"] == "success"
        assert event_kwargs["generation_id"] == "est-123"
        assert len(event_kwargs["generated_files"]) == 2
        assert event_kwargs["duration_seconds"] >= 0

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.telemetry")
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    @patch("app.services.workflow_steps.create_agent_logger")
    @patch("app.services.workflow_steps._collect_rosetta_file_manifest")
    async def test_catches_exceptions_and_emits_failed_event(
        self,
        mock_manifest: Mock,
        mock_create_logger: Mock,
        mock_agent_query: AsyncMock,
        mock_telemetry: Mock,
    ) -> None:
        """Scenario: agent_query raises -> exception caught, failed event emitted."""
        ctx = _make_ctx(rosetta_enabled=True)
        mock_create_logger.return_value = Mock()
        mock_agent_query.side_effect = RuntimeError("MCP connection failed")
        mock_manifest.return_value = []

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
        assert "MCP connection failed" in event_kwargs["error_message"]

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
        """Scenario: SKIP_MODE enabled -> mock artifacts created, agent_query never called."""
        ctx = _make_ctx(rosetta_enabled=True, workspace_root=str(tmp_path))

        await run_kb_init_agent(ctx)

        rosetta = tmp_path / "rosetta"
        assert (rosetta / "CLAUDE.md").exists()
        assert (rosetta / "agents" / "default.md").exists()
        assert (rosetta / "docs" / "CONTEXT.md").exists()

        ctx.logger.warning.assert_any_call(
            "[SKIP_MODE] KB init agent skipped — created 3 "
            "mock artifacts under rosetta/"
        )
        mock_telemetry_ctx.set_workflow.assert_called_once_with(
            TelemetryWorkflowLabel.plain(WorkflowName.KB_INIT)
        )
        mock_telemetry.capture_kb_init_triggered.assert_not_called()
        mock_telemetry.capture_kb_init_event.assert_called_once()
        event_kwargs = mock_telemetry.capture_kb_init_event.call_args.kwargs
        assert event_kwargs["status"] == "success"
        assert len(event_kwargs["generated_files"]) == 3
        assert event_kwargs["duration_seconds"] == 0.0


# ---------------------------------------------------------------------------
# Blank-config no-op regression tests for Rosetta (FR-10 / S4.1 insurance)
# ---------------------------------------------------------------------------

class TestRosettaBlankConfigNoOpKbInit:
    """ROSETTA_MCP_ENABLED=False with all creds blank → no agent_query call, no error."""

    @pytest.mark.asyncio
    @patch("app.services.workflow_steps.telemetry")
    @patch("app.services.workflow_steps.agent_query", new_callable=AsyncMock)
    async def test_disabled_with_blank_creds_skips_agent_query(
        self,
        mock_agent_query: AsyncMock,
        mock_telemetry: Mock,
    ) -> None:
        """Scenario: ROSETTA_MCP_ENABLED=False + all creds None → agent_query never called, no error."""
        ctx = _make_ctx(rosetta_enabled=False)
        # Blank out all Rosetta credentials
        ctx.settings.ROSETTA_API_KEY = None
        ctx.settings.ROSETTA_USER_EMAIL = None
        ctx.settings.ROSETTA_SERVER_URL = None
        ctx.settings.ROSETTA_IMS_VERSION = ""

        await run_kb_init_agent(ctx)

        mock_agent_query.assert_not_called()
        mock_telemetry.capture_kb_init_triggered.assert_not_called()
        # Skipped event is still emitted so observability works
        mock_telemetry.capture_kb_init_event.assert_called_once()
        event_kwargs = mock_telemetry.capture_kb_init_event.call_args.kwargs
        assert event_kwargs["status"] == "skipped"


class TestGenerateMockRosettaArtifacts:
    """Tests for _generate_mock_rosetta_artifacts helper."""

    def test_creates_expected_files(self, tmp_path: Path) -> None:
        logger = Mock()
        _generate_mock_rosetta_artifacts(str(tmp_path), "rosetta", logger)

        assert (tmp_path / "rosetta" / "CLAUDE.md").exists()
        assert (tmp_path / "rosetta" / "agents" / "default.md").exists()
        assert (tmp_path / "rosetta" / "docs" / "CONTEXT.md").exists()
        assert "[SKIP_MODE]" in (tmp_path / "rosetta" / "CLAUDE.md").read_text()

    def test_idempotent_on_existing_dir(self, tmp_path: Path) -> None:
        logger = Mock()
        _generate_mock_rosetta_artifacts(str(tmp_path), "rosetta", logger)
        _generate_mock_rosetta_artifacts(str(tmp_path), "rosetta", logger)

        assert (tmp_path / "rosetta" / "CLAUDE.md").exists()
