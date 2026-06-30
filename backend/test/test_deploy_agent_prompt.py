"""Unit tests for deploy phase agent prompt and supporting utilities."""

import pytest

from app.core.config import LLM_MEDIUM_DEFAULT_FIRST_MODEL, WORKSPACE_DEFAULT_BRANCH, WORKSPACE_DEPLOY_WORKFLOW
from app.prompts.agents_claude_code import (
    DEPLOY_FAILURE_REPORT_FILE,
    E2E_TEST_PLAN_FILE,
    generate_deploy_phase_agent_template,
)
from app.schemas.deploy_context import DeployGithubContext
from app.schemas.planning import PhaseInfo
from app.schemas.specification import SpecReadiness


PHASE_INFO = PhaseInfo(
    number=1,
    name="Initial deployment and smoke test",
    description="Deploy to staging and verify health check.",
    estimated_commits=0,
)


class TestDeployFailureReportFileConstant:
    def test_constant_defined(self):
        assert DEPLOY_FAILURE_REPORT_FILE == "deploy_failure_report.md"


class TestGenerateDeployPhaseAgentTemplate:
    def _prompt(self, **kwargs) -> str:
        defaults = dict(
            model=LLM_MEDIUM_DEFAULT_FIRST_MODEL,
            phase_number=1,
            phase_info=PHASE_INFO,
            workspace_root="/workspaces/ws-test",
            spec_path="./specifications",
            outputs_dir="./specflow",
            integration_readiness=SpecReadiness.INTEGRATION_TESTS_READY,
            workspace_id="ws-abc123",
            generation_id="est-xyz789",
            github_repo="myorg/myrepo",
            github_ref=WORKSPACE_DEFAULT_BRANCH,
            deploy_workflow=WORKSPACE_DEPLOY_WORKFLOW,
        )
        defaults.update(kwargs)
        return generate_deploy_phase_agent_template(**defaults)

    # --- Context injection ---

    def test_workspace_root_paths_in_prompt(self):
        """Agents must see the real isolated cwd, not assume /workspace/results/..."""
        prompt = self._prompt(
            workspace_root="/workspaces/ws-09-1",
            outputs_dir="results",
        )
        assert "/workspaces/ws-09-1/results/planning/IMPLEMENTATION_PLAN.md" in prompt
        assert "do not guess `/workspace/...`" in prompt.lower()

    def test_workspace_id_in_prompt(self):
        prompt = self._prompt(workspace_id="ws-abc123")
        assert "ws-abc123" in prompt

    def test_generation_id_in_prompt(self):
        prompt = self._prompt(generation_id="est-xyz789")
        assert "est-xyz789" in prompt

    def test_github_repo_in_prompt(self):
        prompt = self._prompt(github_repo="myorg/myrepo")
        assert "myorg/myrepo" in prompt

    def test_github_ref_in_prompt(self):
        prompt = self._prompt(github_ref="feature/my-branch")
        assert "feature/my-branch" in prompt

    def test_deploy_workflow_in_prompt(self):
        prompt = self._prompt(deploy_workflow="custom.yml")
        assert "custom.yml" in prompt

    def test_none_values_become_unknown(self):
        prompt = self._prompt(workspace_id=None, generation_id=None, github_repo=None, github_ref=None)
        assert "unknown" in prompt

    # --- gh polling pattern ---

    def test_poll_pattern_present(self):
        prompt = self._prompt()
        assert "gh run list" in prompt
        assert "gh run view" in prompt

    def test_gh_run_watch_forbidden(self):
        """The prompt must explicitly forbid gh run watch (does not exit)."""
        prompt = self._prompt()
        # The string appears only in the NEVER/FORBIDDEN context, not as a recommended command
        assert "NEVER use `gh run watch`" in prompt or "gh run watch" in prompt.lower()

    def test_sleep_polling_loop(self):
        prompt = self._prompt()
        assert "sleep" in prompt

    def test_gh_workflow_run_includes_quoted_dispatch_inputs(self):
        """Regression HF-run2 L1: trigger must pass generation_id and workspace_id explicitly."""
        prompt = self._prompt()
        assert '-f generation_id="$GENERATION_ID"' in prompt
        assert '-f workspace_id="$WORKSPACE_ID"' in prompt

    def test_deploy_header_uses_e2e_plan_not_completeness_for_qa_loop(self):
        """Deploy/E2E execution follows e2e-test-plan.md; completeness.md is upstream spec gap analysis."""
        prompt = self._prompt()
        deploy_start = prompt.index("## Deploy Phase Context")
        deploy_block = prompt[deploy_start : prompt.index("## Log Download 403")]
        assert E2E_TEST_PLAN_FILE in deploy_block
        assert "primary driver for the qa loop" in deploy_block.lower()
        assert "127.0.0.1,::1" not in deploy_block

    def test_deploy_header_includes_phase_mcp_scope_from_enabled_mcps(self):
        """Deploy context states actual optional MCPs attached (same intersection as codegen phase loop)."""
        prompt = self._prompt(enabled_mcps=frozenset({"playwright", "figma"}))
        deploy_start = prompt.index("## Deploy Phase Context")
        deploy_block = prompt[deploy_start : prompt.index("## GitHub Actions")]
        assert "**Phase MCP scope:**" in deploy_block
        assert "figma" in deploy_block
        assert "playwright" in deploy_block
        assert "not npm e2e" in deploy_block.lower()

    def test_deploy_header_phase_mcp_scope_none_when_empty(self):
        prompt = self._prompt(enabled_mcps=frozenset())
        deploy_start = prompt.index("## Deploy Phase Context")
        deploy_block = prompt[deploy_start : prompt.index("## GitHub Actions")]
        assert "optional agent MCPs: (none)" in deploy_block

    def test_deploy_does_not_duplicate_phase_mcp_scope_from_plan_field(self):
        """Plan applicable_agent_mcps is not echoed in the nested phase header when deploy supplies scope."""
        info = PhaseInfo(
            number=1,
            name="Deploy",
            description="Go.",
            estimated_commits=0,
            applicable_agent_mcps=("playwright", "figma"),
        )
        prompt = self._prompt(phase_info=info, enabled_mcps=frozenset({"playwright"}))
        # Single **Phase MCP scope:** before phase header body — harness truth is playwright only
        assert prompt.count("**Phase MCP scope:**") == 1
        scope_pos = prompt.index("**Phase MCP scope:**")
        phase_impl = prompt.index("You are implementing Phase")
        assert scope_pos < phase_impl
        assert "optional agent MCPs: playwright" in prompt[scope_pos:phase_impl]
        assert "figma" not in prompt[scope_pos:phase_impl]

    # --- Failure report protocol ---

    def test_failure_report_file_referenced(self):
        prompt = self._prompt(outputs_dir="./specflow")
        assert DEPLOY_FAILURE_REPORT_FILE in prompt

    def test_failure_report_includes_context_labels(self):
        prompt = self._prompt()
        assert "DEPLOY FAILURE REPORT" in prompt
        assert "FAILED STEP" in prompt
        assert "ACCOUNTS PROVIDED TO AGENT" in prompt
        assert "ACTION REQUIRED" in prompt

    def test_stop_on_auth_failure_instruction(self):
        prompt = self._prompt()
        assert "STOP" in prompt

    # --- Log 403 handling ---

    def test_403_non_fatal_instruction(self):
        prompt = self._prompt()
        assert "403" in prompt
        assert "Non-Fatal" in prompt or "non-fatal" in prompt.lower()

    # --- Hard stop / prohibited workarounds ---

    def test_no_secret_extraction(self):
        prompt = self._prompt()
        assert "secret" in prompt.lower()
        assert "STRICTLY FORBIDDEN" in prompt or "HARD STOP" in prompt

    def test_no_direct_cloud_api_calls(self):
        prompt = self._prompt()
        assert "curl" in prompt
        assert "boto3" in prompt or "google-cloud" in prompt

    # --- Base template included ---

    def test_includes_base_phase_header(self):
        prompt = self._prompt(phase_number=2)
        assert "Phase 2" in prompt

    def test_includes_no_long_running_processes_rule(self):
        prompt = self._prompt()
        assert "NEVER START LONG-RUNNING PROCESSES" in prompt

    def test_includes_work_complete_instruction(self):
        prompt = self._prompt()
        assert "WORK_COMPLETE" in prompt

    # --- Outputs dir wired into failure report path ---

    def test_custom_outputs_dir_in_failure_report_path(self):
        prompt = self._prompt(outputs_dir="./output")
        assert "./output" in prompt
        assert DEPLOY_FAILURE_REPORT_FILE in prompt


class TestDeployGithubContext:
    """Tests for the DeployGithubContext dataclass."""

    def test_fields_accessible_as_attributes(self):
        ctx = DeployGithubContext(
            github_repo="org/repo",
            github_ref=WORKSPACE_DEFAULT_BRANCH,
            deploy_workflow=WORKSPACE_DEPLOY_WORKFLOW,
        )
        assert ctx.github_repo == "org/repo"
        assert ctx.github_ref == WORKSPACE_DEFAULT_BRANCH
        assert ctx.deploy_workflow == WORKSPACE_DEPLOY_WORKFLOW

    def test_github_repo_can_be_none(self):
        ctx = DeployGithubContext(github_repo=None, github_ref="main", deploy_workflow="deploy.yml")
        assert ctx.github_repo is None


class TestBuildDeployGithubContext:
    """Tests for _build_deploy_github_context."""

    def _make_service(self, repo_url):
        class FakeService:
            async def get_workspace_repo_url(self, workspace_id):
                return repo_url
        return FakeService()

    @pytest.mark.asyncio
    async def test_reads_repo_url_verbatim(self):
        from app.services.workflow_steps import _build_deploy_github_context
        url = "https://github.com/myorg/generation-workspace1"
        ctx = await _build_deploy_github_context(self._make_service(url), "ws-001")
        assert ctx.github_repo == url

    @pytest.mark.asyncio
    async def test_branch_is_always_workspace_default(self):
        from app.services.workflow_steps import _build_deploy_github_context
        ctx = await _build_deploy_github_context(self._make_service("https://github.com/org/ws1"), "ws-001")
        assert ctx.github_ref == WORKSPACE_DEFAULT_BRANCH

    @pytest.mark.asyncio
    async def test_deploy_workflow_uses_constant(self):
        from app.services.workflow_steps import _build_deploy_github_context
        ctx = await _build_deploy_github_context(self._make_service("https://github.com/org/ws1"), "ws-001")
        assert ctx.deploy_workflow == WORKSPACE_DEPLOY_WORKFLOW

    @pytest.mark.asyncio
    async def test_no_generation_session_service(self):
        from app.services.workflow_steps import _build_deploy_github_context
        ctx = await _build_deploy_github_context(None, "ws-001")
        assert ctx.github_repo is None
        assert ctx.github_ref == WORKSPACE_DEFAULT_BRANCH

    @pytest.mark.asyncio
    async def test_workspace_not_found(self):
        from app.services.workflow_steps import _build_deploy_github_context
        ctx = await _build_deploy_github_context(self._make_service(None), "ws-001")
        assert ctx.github_repo is None


class TestRunDeployAndE2EGuards:
    """Guard: run_deploy_and_e2e must raise before phase 1 if repo_url is missing."""

    @pytest.mark.asyncio
    async def test_raises_if_github_repo_is_none(self):
        """Regression: workspace with no repo_url must fail fast, not pass 'unknown' to gh CLI."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.services.workflow_steps import run_deploy_and_e2e, WorkflowContext
        from app.schemas.workspace import WorkspaceSettings
        import logging

        svc = AsyncMock()
        svc.get_workspace_repo_url = AsyncMock(return_value=None)

        request = MagicMock()
        request.outputs_dir = "specflow"
        request.generation_id = "est-test"

        import tempfile
        import pathlib
        tmp = pathlib.Path(tempfile.mkdtemp())
        ws = WorkspaceSettings(name="ws-1", workspace_path=str(tmp / "ws-1"), provider="openrouter", model="claude-sonnet-4")

        from app.schemas.planning import PlanningResult, PhaseInfo
        e2e_plan = PlanningResult(
            phase_count=1,
            phases=[PhaseInfo(number=1, name="Deploy", description="", estimated_commits=1)],
            plan_file_path="./specflow/e2e-test-plan.md",
        )

        ctx = WorkflowContext(
            request=request,
            settings=MagicMock(),
            logger=logging.getLogger("test"),
            workspace_ids=["ws-1"],
            workspaces=[ws],
            workspace_manager=MagicMock(),
            primary_workspace=MagicMock(),
            # PR255 Bug 1: the repo-missing guard runs only when deploy actually starts,
            # which now requires INTEGRATION_TESTS_READY (run_deploy_and_e2e self-guards).
            integration_readiness=SpecReadiness.INTEGRATION_TESTS_READY,
            generation_session_service=svc,
            workspace_phases_deployment_data={},
            e2e_planning_data=e2e_plan,
        )

        with patch("app.services.workflow_steps.ParallelAgentExecutor") as mock_exec_cls:
            async def run_parallel(workspaces, agent_fn):
                for w in workspaces:
                    await agent_fn(w, MagicMock(), logging.getLogger("test"))
            mock_exec_cls.return_value.execute_parallel = run_parallel

            with pytest.raises(ValueError, match="repo_url is not set"):
                await run_deploy_and_e2e(ctx)
