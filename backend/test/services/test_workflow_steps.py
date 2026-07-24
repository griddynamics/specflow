"""
Tests for workflow_steps.py — WorkflowContext defaults, resolve helpers, and resume logic.
"""
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.schemas.specification import SpecReadiness


# ---------------------------------------------------------------------------
# WorkflowContext default — Phase 3 sub-change
# ---------------------------------------------------------------------------

class TestWorkflowContextDefaults:
    def test_integration_readiness_defaults_to_not_ready(self):
        """
        WorkflowContext.integration_readiness must default to NOT_READY.
        LOCAL_ONLY as a default silently masks cases where build_workflow_context()
        was skipped or failed. NOT_READY makes uninitialized state explicit.
        """
        from app.services.workflow_steps import WorkflowContext
        ctx = WorkflowContext(
            request=MagicMock(),
            settings=MagicMock(),
            logger=MagicMock(),
            workspace_ids=["ws-1"],
            workspaces=[],
            workspace_manager=MagicMock(),
        )
        assert ctx.integration_readiness == SpecReadiness.NOT_READY, (
            f"Expected NOT_READY but got {ctx.integration_readiness}. "
            "LOCAL_ONLY as a default silently masks missing build_workflow_context() calls."
        )


# ---------------------------------------------------------------------------
# _resolve_workspace_id
# ---------------------------------------------------------------------------

class TestResolveWorkspaceId:
    def test_resolves_by_identity(self):
        from app.services.workflow_steps import _resolve_workspace_id
        from app.schemas.workspace import WorkspaceSettings

        ws = WorkspaceSettings(name="ws-1", workspace_path="/mnt/ws-1", provider="openrouter", model="claude-sonnet-4")
        result = _resolve_workspace_id(ws, [ws], ["ws-01-1"])
        assert result == "ws-01-1"

    def test_resolves_by_name_and_path(self):
        from app.services.workflow_steps import _resolve_workspace_id
        from app.schemas.workspace import WorkspaceSettings

        ws_original = WorkspaceSettings(name="ws-1", workspace_path="/mnt/ws-1", provider="openrouter", model="claude-sonnet-4")
        ws_copy = WorkspaceSettings(name="ws-1", workspace_path="/mnt/ws-1", provider="openrouter", model="claude-sonnet-4")
        result = _resolve_workspace_id(ws_copy, [ws_original], ["ws-01-1"])
        assert result == "ws-01-1"

    def test_returns_none_when_not_found(self):
        from app.services.workflow_steps import _resolve_workspace_id
        from app.schemas.workspace import WorkspaceSettings

        ws = WorkspaceSettings(name="ws-1", workspace_path="/mnt/ws-1", provider="openrouter", model="claude-sonnet-4")
        other = WorkspaceSettings(name="ws-2", workspace_path="/mnt/ws-2", provider="openrouter", model="claude-sonnet-4")
        result = _resolve_workspace_id(ws, [other], ["ws-01-2"])
        assert result is None

    def test_returns_none_when_index_out_of_bounds(self):
        from app.services.workflow_steps import _resolve_workspace_id
        from app.schemas.workspace import WorkspaceSettings

        ws = WorkspaceSettings(name="ws-1", workspace_path="/mnt/ws-1", provider="openrouter", model="claude-sonnet-4")
        # workspace exists in list but workspace_ids is shorter
        result = _resolve_workspace_id(ws, [ws], [])
        assert result is None


# ---------------------------------------------------------------------------
# _planning_result_from_dict
# ---------------------------------------------------------------------------

class TestPlanningResultFromDict:
    def test_reconstructs_phase_count_and_phases(self):
        from app.services.workflow_steps import _planning_result_from_dict

        d = {
            "phase_count": 2,
            "phases": [
                {"number": 1, "name": "Deploy", "description": "desc", "estimated_commits": 1},
                {"number": 2, "name": "Verify", "description": "desc2", "estimated_commits": 2},
            ],
            "plan_file_path": "./specflow/e2e-test-plan.md",
        }
        result = _planning_result_from_dict(d)
        assert result.phase_count == 2
        assert len(result.phases) == 2
        assert result.phases[0].name == "Deploy"
        assert result.plan_file_path == "./specflow/e2e-test-plan.md"

    def test_falls_back_to_len_phases_when_phase_count_missing(self):
        from app.services.workflow_steps import _planning_result_from_dict

        d = {
            "phases": [
                {"number": 1, "name": "A", "description": "", "estimated_commits": 1},
            ],
            "plan_file_path": "",
        }
        result = _planning_result_from_dict(d)
        assert result.phase_count == 1


# ---------------------------------------------------------------------------
# run_deploy_and_e2e — resume logic
# ---------------------------------------------------------------------------

def _make_generation_session_service_with_repo(repo_url="https://github.com/test-org/test-repo"):
    """Return a minimal mock generation service with get_workspace_repo_url set."""
    from unittest.mock import AsyncMock
    svc = AsyncMock()
    svc.get_workspace_repo_url = AsyncMock(return_value=repo_url)
    return svc


def _make_deploy_ctx(
    tmp_path,
    workspace_phases_deployment_data: dict = None,
    e2e_planning_data=None,
    generation_session_service=None,
):
    """Build a minimal WorkflowContext for run_deploy_and_e2e tests."""
    if generation_session_service is None:
        generation_session_service = _make_generation_session_service_with_repo()
    from app.services.workflow_steps import WorkflowContext
    from app.schemas.workspace import WorkspaceSettings

    primary = MagicMock()
    primary.get_isolated_root.return_value = str(tmp_path)

    request = MagicMock()
    request.outputs_dir = "specflow"
    request.generation_id = "est-test"

    ws = WorkspaceSettings(name="ws-1", workspace_path=str(tmp_path / "ws-1"), provider="openrouter", model="claude-sonnet-4")

    ctx = WorkflowContext(
        request=request,
        settings=MagicMock(),
        logger=logging.getLogger("test"),
        workspace_ids=["ws-01-1"],
        workspaces=[ws],
        workspace_manager=MagicMock(),
        primary_workspace=primary,
        # PR255 Bug 1: run_deploy_and_e2e now self-guards on this value, so these
        # body-exercising tests must declare the spec INTEGRATION_TESTS_READY.
        integration_readiness=SpecReadiness.INTEGRATION_TESTS_READY,
        generation_session_service=generation_session_service,
        workspace_phases_deployment_data=workspace_phases_deployment_data or {},
        e2e_planning_data=e2e_planning_data,
    )
    return ctx


def _make_e2e_planning_data(phase_count: int = 3):
    from app.schemas.planning import PlanningResult, PhaseInfo
    return PlanningResult(
        phase_count=phase_count,
        phases=[
            PhaseInfo(number=i, name=f"Phase {i}", description="", estimated_commits=1)
            for i in range(1, phase_count + 1)
        ],
        plan_file_path="./specflow/e2e-test-plan.md",
    )


class TestRunDeployAndE2eResume:
    @pytest.mark.asyncio
    async def test_uses_e2e_planning_data_from_ctx_when_available(self, tmp_path):
        """On retry, planning data from Firestore is used without reading the file."""
        from app.services.workflow_steps import run_deploy_and_e2e
        from unittest.mock import patch

        e2e_plan = _make_e2e_planning_data(phase_count=2)
        ctx = _make_deploy_ctx(tmp_path, e2e_planning_data=e2e_plan)
        # No e2e-test-plan.md file created — would fail if file was read

        captured_phases = []

        async def mock_execute_all_phases(**kwargs):
            captured_phases.append({
                "start_phase": kwargs.get("start_phase"),
                "end_phase": kwargs.get("end_phase"),
                "planning_data": kwargs.get("planning_data"),
            })
            return []

        with patch("app.services.workflow_steps.execute_all_phases", side_effect=mock_execute_all_phases):
            with patch("app.services.workflow_steps.ParallelAgentExecutor") as mock_exec_cls:
                async def run_parallel(workspaces, agent_fn):
                    for ws in workspaces:
                        await agent_fn(ws, MagicMock(), logging.getLogger("test"))
                mock_exec_cls.return_value.execute_parallel = run_parallel
                await run_deploy_and_e2e(ctx)

        assert len(captured_phases) == 1
        assert captured_phases[0]["planning_data"] is e2e_plan

    @pytest.mark.asyncio
    async def test_skips_workspace_when_all_deploy_phases_complete(self, tmp_path):
        """When last_completed_phase >= total_phases, workspace is skipped."""
        from app.services.workflow_steps import run_deploy_and_e2e
        from unittest.mock import patch

        e2e_plan = _make_e2e_planning_data(phase_count=3)
        phases_data = {
            "ws-01-1": {"last_completed_phase": 3, "total_phases": 3}
        }
        ctx = _make_deploy_ctx(
            tmp_path,
            e2e_planning_data=e2e_plan,
            workspace_phases_deployment_data=phases_data,
        )

        execute_called = []

        async def mock_execute_all_phases(**kwargs):
            execute_called.append(True)
            return []

        with patch("app.services.workflow_steps.execute_all_phases", side_effect=mock_execute_all_phases):
            with patch("app.services.workflow_steps.ParallelAgentExecutor") as mock_exec_cls:
                async def run_parallel(workspaces, agent_fn):
                    for ws in workspaces:
                        await agent_fn(ws, MagicMock(), logging.getLogger("test"))
                mock_exec_cls.return_value.execute_parallel = run_parallel
                await run_deploy_and_e2e(ctx)

        assert len(execute_called) == 0

    @pytest.mark.asyncio
    async def test_passes_start_phase_when_partially_complete(self, tmp_path):
        """When last_completed_phase > 0 but < total, start_phase is set for resume."""
        from app.services.workflow_steps import run_deploy_and_e2e
        from unittest.mock import patch

        e2e_plan = _make_e2e_planning_data(phase_count=4)
        phases_data = {
            "ws-01-1": {"last_completed_phase": 2, "total_phases": 4}
        }
        ctx = _make_deploy_ctx(
            tmp_path,
            e2e_planning_data=e2e_plan,
            workspace_phases_deployment_data=phases_data,
        )

        captured = []

        async def mock_execute_all_phases(**kwargs):
            captured.append({
                "start_phase": kwargs.get("start_phase"),
                "end_phase": kwargs.get("end_phase"),
            })
            return []

        with patch("app.services.workflow_steps.execute_all_phases", side_effect=mock_execute_all_phases):
            with patch("app.services.workflow_steps.ParallelAgentExecutor") as mock_exec_cls:
                async def run_parallel(workspaces, agent_fn):
                    for ws in workspaces:
                        await agent_fn(ws, MagicMock(), logging.getLogger("test"))
                mock_exec_cls.return_value.execute_parallel = run_parallel
                await run_deploy_and_e2e(ctx)

        assert len(captured) == 1
        assert captured[0]["start_phase"] == 3
        assert captured[0]["end_phase"] == 4

    @pytest.mark.asyncio
    async def test_no_start_phase_on_first_run(self, tmp_path):
        """First run (no checkpoint data) passes start_phase=None."""
        from app.services.workflow_steps import run_deploy_and_e2e
        from unittest.mock import patch

        e2e_plan = _make_e2e_planning_data(phase_count=2)
        ctx = _make_deploy_ctx(
            tmp_path,
            e2e_planning_data=e2e_plan,
            workspace_phases_deployment_data={},  # no prior progress
        )

        captured = []

        async def mock_execute_all_phases(**kwargs):
            captured.append({
                "start_phase": kwargs.get("start_phase"),
                "end_phase": kwargs.get("end_phase"),
            })
            return []

        with patch("app.services.workflow_steps.execute_all_phases", side_effect=mock_execute_all_phases):
            with patch("app.services.workflow_steps.ParallelAgentExecutor") as mock_exec_cls:
                async def run_parallel(workspaces, agent_fn):
                    for ws in workspaces:
                        await agent_fn(ws, MagicMock(), logging.getLogger("test"))
                mock_exec_cls.return_value.execute_parallel = run_parallel
                await run_deploy_and_e2e(ctx)

        assert len(captured) == 1
        assert captured[0]["start_phase"] is None
        assert captured[0]["end_phase"] is None

    @pytest.mark.asyncio
    async def test_pre_deploy_milestone_notification_on_first_entry(self, tmp_path):
        """
        First deploy entry sends coding-complete milestone with notification_kind pre-deploy
        when generation doc has recipient and workflow_usage_metrics.
        """
        from app.services.workflow_steps import run_deploy_and_e2e

        e2e_plan = _make_e2e_planning_data(phase_count=2)
        generation_session_service = AsyncMock()
        generation_session_service.db = MagicMock()
        generation_session_service.get_generation_session_status = AsyncMock(
            return_value={
                "parameters": {"notification_email": "owner@example.com"},
                "user_email": "owner@example.com",
                "workflow_usage_metrics": {},
            }
        )
        ctx = _make_deploy_ctx(
            tmp_path,
            e2e_planning_data=e2e_plan,
            workspace_phases_deployment_data={},
            generation_session_service=generation_session_service,
        )

        async def mock_execute_all_phases(**kwargs):
            return []

        with patch(
            "app.services.workflow_steps.notifications.notify_generation_session_complete"
        ) as mock_notify:
            with patch(
                "app.services.workflow_steps.execute_all_phases",
                side_effect=mock_execute_all_phases,
            ):
                with patch("app.services.workflow_steps.ParallelAgentExecutor") as mock_exec_cls:
                    async def run_parallel(workspaces, agent_fn):
                        for ws in workspaces:
                            await agent_fn(ws, MagicMock(), logging.getLogger("test"))

                    mock_exec_cls.return_value.execute_parallel = run_parallel
                    await run_deploy_and_e2e(ctx)

        mock_notify.assert_called_once()
        kw = mock_notify.call_args.kwargs
        assert kw["notification_kind"] == "coding_complete_pre_deploy"
        assert kw["recipient_email"] == "owner@example.com"
        assert kw["generation_id"] == "est-test"


# ---------------------------------------------------------------------------
# planning_mcp_servers_and_tools
# ---------------------------------------------------------------------------

class TestPlanningMcpServersAndTools:
    """Tests for planning_mcp_servers_and_tools(): optional Figma only."""

    def _ctx(self, *, enabled_mcps=frozenset(), figma_token=None):
        from unittest.mock import Mock
        from app.services.workflow_steps import WorkflowContext

        ctx = Mock(spec=WorkflowContext)
        ctx.enabled_mcps = frozenset(enabled_mcps)

        s = Mock()
        s.FIGMA_MCP_COMMAND = "npx"
        s.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
        s.FIGMA_ACCESS_TOKEN = figma_token
        s.FIGMA_API_KEY = None
        ctx.settings = s
        return ctx

    def test_nothing_enabled_returns_empty(self):
        from app.core.mcp_config import planning_mcp_servers_and_tools as _planning_mcp_servers_and_tools

        ctx = self._ctx()
        servers, tools = _planning_mcp_servers_and_tools(ctx.settings, ctx.enabled_mcps)
        assert servers == {}
        assert tools == []

    def test_figma_only_with_token(self):
        from app.core.mcp_config import planning_mcp_servers_and_tools as _planning_mcp_servers_and_tools
        from app.core.tool_usage import get_figma_mcp_tools

        ctx = self._ctx(enabled_mcps={"figma"}, figma_token="secret")
        servers, tools = _planning_mcp_servers_and_tools(ctx.settings, ctx.enabled_mcps)
        assert "Figma" in servers
        assert tools == get_figma_mcp_tools()

    def test_figma_requested_but_token_missing_skips_figma(self):
        from app.core.mcp_config import planning_mcp_servers_and_tools as _planning_mcp_servers_and_tools

        ctx = self._ctx(enabled_mcps={"figma"}, figma_token=None)
        servers, tools = _planning_mcp_servers_and_tools(ctx.settings, ctx.enabled_mcps)
        assert "Figma" not in servers
        assert tools == []

    def test_playwright_not_wired_for_planning(self):
        from app.core.mcp_config import planning_mcp_servers_and_tools as _planning_mcp_servers_and_tools

        ctx = self._ctx(enabled_mcps={"playwright"})
        servers, tools = _planning_mcp_servers_and_tools(ctx.settings, ctx.enabled_mcps)
        assert "playwright" not in servers
        assert tools == []

