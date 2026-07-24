"""
execute_all_phases: per-phase optional MCP resolution (plan ∩ generation ∩ SUPPORTED)
and prompt/MCP wiring, with phase_agent_query path mocked (no real agents).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.schemas.agent import AgentResult
from app.schemas.planning import PhaseInfo, PlanningResult
from app.schemas.specification import GenerateAppRequest, SpecReadiness


def _mock_workspace(tmp_path: Path) -> Mock:
    ws = Mock()
    ws.workspace_path = tmp_path
    ws.get_isolated_root = Mock(return_value=str(tmp_path))
    ws.model = "m"
    ws.provider = "openrouter"
    return ws


def _mock_manager() -> Mock:
    mgr = Mock()
    mgr.settings = Mock()
    mgr.settings.PLAYWRIGHT_MCP_COMMAND = "npx"
    mgr.settings.PLAYWRIGHT_MCP_ARGS = "-y @playwright/mcp@latest"
    mgr.settings.FIGMA_MCP_COMMAND = "npx"
    mgr.settings.FIGMA_MCP_ARGS = "-y figma-developer-mcp --stdio"
    mgr.settings.FIGMA_ACCESS_TOKEN = "tok"
    mgr.commit_and_push_outstanding = AsyncMock(return_value=True)
    return mgr


def _planning_two_phases(
    *,
    p1_mcp: tuple[str, ...] | None = None,
    p2_mcp: tuple[str, ...] | None = None,
) -> PlanningResult:
    return PlanningResult(
        phase_count=2,
        phases=[
            PhaseInfo(
                number=1,
                name="Backend",
                description="API",
                estimated_commits=1,
                applicable_agent_mcps=p1_mcp,
            ),
            PhaseInfo(
                number=2,
                name="UI",
                description="React",
                estimated_commits=2,
                applicable_agent_mcps=p2_mcp,
            ),
        ],
        plan_file_path="specflow/plan.md",
    )


@pytest.mark.asyncio
async def test_execute_all_phases_omitted_applicable_uses_full_generation_mcps(tmp_path: Path) -> None:
    """When applicable_agent_mcps is None, phase uses generation enabled_mcps for MCP config."""
    from app.services.claude_code import execute_all_phases

    planning = _planning_two_phases(p1_mcp=None, p2_mcp=None)
    enabled = frozenset({"playwright", "figma"})
    mock_ws = _mock_workspace(tmp_path)
    mock_mgr = _mock_manager()

    request = GenerateAppRequest(
        spec_path="specs",
        outputs_dir="specflow",
        generation_id="e1",
    )

    captured: list[frozenset[str]] = []

    async def fake_phase_fn(**kwargs):
        mcp = kwargs.get("phase_mcp_servers") or {}
        norm: set[str] = set()
        if "playwright" in mcp:
            norm.add("playwright")
        if "Figma" in mcp:
            norm.add("figma")
        captured.append(frozenset(norm))
        return AgentResult(result="ok", session_id=None)

    with patch("app.services.claude_code.phase_agent_fn", side_effect=fake_phase_fn):
        with patch("app.services.claude_code.is_skip_mode_enabled", return_value=False):
            await execute_all_phases(
                planning_data=planning,
                workspace=mock_ws,
                manager=mock_mgr,
                request=request,
                logger=Mock(),
                enabled_mcps=enabled,
            )

    assert len(captured) == 2
    assert captured[0] == frozenset({"playwright", "figma"})
    assert captured[1] == frozenset({"playwright", "figma"})


@pytest.mark.asyncio
async def test_execute_all_phases_intersects_plan_with_generation_mcps(tmp_path: Path) -> None:
    """Plan asks for playwright+figma but generation only enables playwright → phases get playwright only."""
    from app.services.claude_code import execute_all_phases

    planning = _planning_two_phases(
        p1_mcp=None,
        p2_mcp=("playwright", "figma"),
    )
    enabled = frozenset({"playwright"})
    mock_ws = _mock_workspace(tmp_path)
    mock_mgr = _mock_manager()

    request = GenerateAppRequest(spec_path="specs", outputs_dir="specflow", generation_id="e1")

    captured_mcps: list[frozenset[str]] = []

    async def fake_phase_fn(**kwargs):
        mcp = kwargs.get("phase_mcp_servers") or {}
        norm: set[str] = set()
        if "playwright" in mcp:
            norm.add("playwright")
        if "Figma" in mcp:
            norm.add("figma")
        captured_mcps.append(frozenset(norm))
        return AgentResult(result="ok", session_id=None)

    with patch("app.services.claude_code.phase_agent_fn", side_effect=fake_phase_fn):
        with patch("app.services.claude_code.is_skip_mode_enabled", return_value=False):
            await execute_all_phases(
                planning_data=planning,
                workspace=mock_ws,
                manager=mock_mgr,
                request=request,
                logger=Mock(),
                enabled_mcps=enabled,
            )

    assert captured_mcps[0] == frozenset({"playwright"})
    assert captured_mcps[1] == frozenset({"playwright"})


@pytest.mark.asyncio
async def test_execute_all_phases_empty_applicable_agent_mcps_disables_optional_mcps(tmp_path: Path) -> None:
    """applicable_agent_mcps [] → no Playwright/Figma (intersection empty); Rosetta off in settings."""
    from app.services.claude_code import execute_all_phases

    planning = _planning_two_phases(p1_mcp=(), p2_mcp=None)
    enabled = frozenset({"playwright", "figma"})
    mock_ws = _mock_workspace(tmp_path)
    mock_mgr = _mock_manager()

    request = GenerateAppRequest(spec_path="specs", outputs_dir="specflow", generation_id="e1")

    servers_per_phase: list[dict | None] = []

    async def fake_phase_fn(**kwargs):
        servers_per_phase.append(kwargs.get("phase_mcp_servers"))
        return AgentResult(result="ok", session_id=None)

    with patch("app.services.claude_code.phase_agent_fn", side_effect=fake_phase_fn):
        with patch("app.services.claude_code.is_skip_mode_enabled", return_value=False):
            await execute_all_phases(
                planning_data=planning,
                workspace=mock_ws,
                manager=mock_mgr,
                request=request,
                logger=Mock(),
                enabled_mcps=enabled,
            )

    assert servers_per_phase[0] in (None, {})
    assert servers_per_phase[1] is not None
    assert "playwright" in servers_per_phase[1]
    assert "Figma" in servers_per_phase[1]


@pytest.mark.asyncio
async def test_execute_all_phases_deploy_mode_passes_intersected_mcps_to_template(tmp_path: Path) -> None:
    """is_deployment=True still uses the same phase_mcps resolution for the deploy prompt path."""
    from app.services.claude_code import execute_all_phases

    planning = PlanningResult(
        phase_count=1,
        phases=[
            PhaseInfo(
                number=1,
                name="Deploy",
                description="Ship",
                estimated_commits=0,
                applicable_agent_mcps=("playwright", "figma"),
            )
        ],
        plan_file_path="specflow/plan.md",
    )
    enabled = frozenset({"playwright"})

    mock_ws = _mock_workspace(tmp_path)
    mock_mgr = _mock_manager()

    request = GenerateAppRequest(spec_path="specs", outputs_dir="specflow", generation_id="e1")
    deploy_ctx = Mock()
    deploy_ctx.github_repo = "o/r"
    deploy_ctx.github_ref = "main"
    deploy_ctx.deploy_workflow = "deploy.yml"

    prompts: list[str] = []

    async def fake_phase_fn(**kwargs):
        prompts.append(kwargs["phase_prompt"])
        return AgentResult(result="ok", session_id=None)

    with patch("app.services.claude_code.phase_agent_fn", side_effect=fake_phase_fn):
        with patch("app.services.claude_code.is_skip_mode_enabled", return_value=False):
            with patch("app.services.claude_code.github_cli_env_for_generation", return_value={"GH_TOKEN": "test-token"}):
                await execute_all_phases(
                    planning_data=planning,
                    workspace=mock_ws,
                    manager=mock_mgr,
                    request=request,
                    logger=Mock(),
                    integration_readiness=SpecReadiness.LOCAL_ONLY,
                    is_deployment=True,
                    phase_prefix="deploy-",
                    deploy_github_context=deploy_ctx,
                    enabled_mcps=enabled,
                )

    assert len(prompts) == 1
    deploy_prompt = prompts[0]
    assert "**Phase MCP scope:**" in deploy_prompt
    assert "optional agent mcps: playwright" in deploy_prompt.lower()
    assert "optional agent mcps: playwright, figma" not in deploy_prompt.lower()


@pytest.mark.asyncio
async def test_execute_all_phases_rosetta_not_attached_as_mcp(tmp_path: Path) -> None:
    """The Rosetta KB is a provisioned plugin, not an MCP — it never appears in phase_mcp_servers."""
    from app.services.claude_code import execute_all_phases

    planning = _planning_two_phases(p1_mcp=None, p2_mcp=None)
    enabled = frozenset({"playwright"})

    mock_ws = _mock_workspace(tmp_path)
    mock_mgr = _mock_manager()

    request = GenerateAppRequest(spec_path="specs", outputs_dir="specflow", generation_id="e1")

    captured_servers: list[dict] = []

    async def fake_phase_fn(**kwargs):
        captured_servers.append(kwargs.get("phase_mcp_servers") or {})
        return AgentResult(result="ok", session_id=None)

    with patch("app.services.claude_code.phase_agent_fn", side_effect=fake_phase_fn):
        with patch("app.services.claude_code.is_skip_mode_enabled", return_value=False):
            await execute_all_phases(
                planning_data=planning,
                workspace=mock_ws,
                manager=mock_mgr,
                request=request,
                logger=Mock(),
                enabled_mcps=enabled,
            )

    assert len(captured_servers) == 2
    for phase_servers in captured_servers:
        assert "KnowledgeBase" not in phase_servers
        # Only the user-selectable Playwright MCP is wired here.
        assert set(phase_servers) <= {"playwright"}
