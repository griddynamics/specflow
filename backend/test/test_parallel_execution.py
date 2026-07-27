"""
Tests for workspace configuration loader and parallel executor.
"""

from pathlib import Path
import tempfile
from unittest.mock import Mock

import pytest

from app.core.config import Settings
from app.schemas.agent import AgentResult
from app.schemas.estimate import ComponentEstimation, EstimationMetrics, WorkspaceEstimation
from app.schemas.workspace import WorkspaceSettings
from app.services.parallel_executor import ParallelAgentExecutor, execute_generation_parallel
from app.services.workspace_config_loader import WorkspaceConfigLoader
from app.services.workspace_manager import WorkspaceManager


class TestWorkspaceConfigLoader:
    """Tests for YAML workspace configuration loader."""
    
    def test_load_valid(self):
        """Test loading valid YAML configuration."""
        yaml_content = """
workspaces:
  - workspace_path: /agent/workspace1
    provider: anthropic
    model: claude-sonnet-4-0
    outputs_dir: outputs/claude
    
  - workspace_path: /agent/workspace2
    provider: openrouter
    model: gemini-2.5-pro
    outputs_dir: outputs/gemini
    provider_config:
      app_name: "Test App"
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name
        
        try:
            loader = WorkspaceConfigLoader(yaml_path)
            workspaces = loader.get_workspaces()
            
            assert len(workspaces) == 2
            
            # Check first workspace
            assert workspaces[0].provider == "anthropic"
            assert workspaces[0].model == "claude-sonnet-4-0"
            
            # Check second workspace
            assert workspaces[1].provider == "openrouter"
            assert workspaces[1].model == "gemini-2.5-pro"
            assert workspaces[1].provider_config == {"app_name": "Test App"}
        finally:
            Path(yaml_path).unlink()
    
    def test_load_missing_file(self):
        """Test error handling for missing YAML file."""
        with pytest.raises(FileNotFoundError):
            WorkspaceConfigLoader("/nonexistent/file.yaml").load()
    
    def test_load_invalid_format(self):
        """Test error handling for invalid YAML format."""
        yaml_content = """
invalid:
  - no_workspaces_key: true
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name
        
        try:
            loader = WorkspaceConfigLoader(yaml_path)
            workspaces = loader.get_workspaces()
            # Should return empty list when no workspaces key
            assert len(workspaces) == 0
        finally:
            Path(yaml_path).unlink()
    
    def test_load_missing_required_fields(self):
        """Test handling for missing fields (uses defaults)."""
        yaml_content = """
workspaces:
  - workspace_path: /agent/workspace1
    provider: anthropic
    # missing model field - should use default
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name
        
        try:
            loader = WorkspaceConfigLoader(yaml_path)
            workspaces = loader.get_workspaces()
            # Should use default model
            assert len(workspaces) == 1
            assert workspaces[0].model == "claude-sonnet-4-0"  # default
        finally:
            Path(yaml_path).unlink()
    
    def test_save_to_yaml(self):
        """Test that workspaces can be created from settings."""
        workspaces = [
            WorkspaceSettings(name="test-workspace",
                workspace_path="/agent/workspace1",
                provider="anthropic",
                model="claude-sonnet-4-0"
            ),
            WorkspaceSettings(name="test-workspace",
                workspace_path="/agent/workspace2",
                provider="openrouter",
                model="gemini-2.5-pro",
                provider_config={"app_name": "Test"}
            ),
        ]
        
        # Verify workspaces are created correctly
        assert len(workspaces) == 2
        assert workspaces[0].provider == "anthropic"
        assert workspaces[1].provider == "openrouter"


class TestParallelAgentExecutor:
    """Tests for parallel workspace executor."""
    
    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = Mock(spec=Settings)
        settings.AGENT_BASE_PATH = "/agent"
        settings.ANTHROPIC_API_KEY = "test-key"
        settings.OPENROUTER_API_KEY = "test-or-key"
        settings.OPENROUTER_BASE_URL = "https://openrouter.ai/api"
        return settings
    
    @pytest.fixture
    def workspaces(self):
        """Create test workspaces."""
        return [
            WorkspaceSettings(name="test-workspace",
                workspace_path="/agent/workspace1",
                provider="anthropic",
                model="claude-sonnet-4-0"
            ),
            WorkspaceSettings(name="test-workspace",
                workspace_path="/agent/workspace2",
                provider="openrouter",
                model="gemini-2.5-pro"
            ),
            WorkspaceSettings(name="test-workspace",
                workspace_path="/agent/workspace3",
                provider="openrouter",
                model="gpt-4o"
            ),
        ]
    
    @pytest.mark.asyncio
    async def test_execute_parallel_success(self, mock_settings, workspaces):
        """Test successful parallel execution."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        executor = ParallelAgentExecutor(workspace_manager, logger)
        
        # Mock agent function with correct signature: (workspace, manager, logger)
        async def mock_agent_function(workspace, manager, log):
            return AgentResult(
                result=f"Success from {workspace.provider}/{workspace.model}",
                session_id="test-session"
            )
        
        results = await executor.execute_parallel(
            workspaces=workspaces,
            agent_fn=mock_agent_function,
        )
        
        assert len(results) == 3
        assert all(r.success for r in results)
        assert results[0].workspace_settings.provider == "anthropic"
        assert results[1].workspace_settings.provider == "openrouter"
        assert results[2].workspace_settings.provider == "openrouter"
    
    @pytest.mark.asyncio
    async def test_execute_parallel_with_failures(self, mock_settings, workspaces):
        """Test parallel execution with some failures."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        executor = ParallelAgentExecutor(workspace_manager, logger)
        
        # Mock agent function that fails for second workspace
        async def mock_agent_function(workspace, manager, log):
            if workspace.provider == "openrouter" and workspace.model == "gemini-2.5-pro":
                raise ValueError("Simulated failure")
            return AgentResult(
                result=f"Success from {workspace.provider}/{workspace.model}",
                session_id="test-session"
            )
        
        results = await executor.execute_parallel(
            workspaces=workspaces,
            agent_fn=mock_agent_function,
        )
        
        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False
        assert results[2].success is True
        assert "Simulated failure" in results[1].error
    
    @pytest.mark.asyncio
    async def test_execute_parallel_reraises_cancellation(self, mock_settings, workspaces):
        """A cooperative cancellation must abort the whole fan-out, not degrade to a
        per-workspace failure result absorbed by gather(return_exceptions=True)."""
        from app.state.exceptions import GenerationCancelledError

        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        executor = ParallelAgentExecutor(workspace_manager, logger)

        async def mock_agent_function(workspace, manager, log):
            if workspace.provider == "anthropic":
                raise GenerationCancelledError("gen-1")
            return AgentResult(result="ok", session_id="s")

        with pytest.raises(GenerationCancelledError):
            await executor.execute_parallel(
                workspaces=workspaces,
                agent_fn=mock_agent_function,
            )

    @pytest.mark.asyncio
    async def test_execute_parallel_workflow(self, mock_settings, workspaces):
        """Test parallel workflow execution."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        executor = ParallelAgentExecutor(workspace_manager, logger)
        
        # Mock workflow function with correct signature
        async def mock_workflow(workspace, manager, log):
            return AgentResult(
                result=f"Workflow result for {workspace.provider}/{workspace.model}",
                session_id="test-session"
            )
        
        results = await executor.execute_parallel(
            workspaces=workspaces,
            agent_fn=mock_workflow,
        )
        
        assert len(results) == 3
        assert all(r.success for r in results)
        assert "anthropic" in results[0].result.result
        assert "gemini" in results[1].result.result
        assert "gpt-4o" in results[2].result.result


class TestIntegration:
    """Integration tests for YAML config + parallel execution."""
    
    @pytest.mark.asyncio
    async def test_yaml_to_parallel_execution(self):
        """Test full flow: YAML -> load -> parallel execution."""
        yaml_content = """
workspaces:
  - workspace_path: /agent/workspace1
    provider: anthropic
    model: claude-sonnet-4-0
    
  - workspace_path: /agent/workspace2
    provider: openrouter
    model: gemini-2.5-pro
"""
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name
        
        try:
            # Load workspaces from YAML
            loader = WorkspaceConfigLoader(yaml_path)
            workspaces = loader.get_workspaces()
            assert len(workspaces) == 2
            
            # Setup for parallel execution
            mock_settings = Mock(spec=Settings)
            mock_settings.AGENT_BASE_PATH = "/agent"
            mock_settings.ANTHROPIC_API_KEY = "test-key"
            mock_settings.OPENROUTER_API_KEY = "test-or-key"
            mock_settings.OPENROUTER_BASE_URL = "https://openrouter.ai/api"
            
            logger = Mock()
            workspace_manager = WorkspaceManager(mock_settings, logger)
            executor = ParallelAgentExecutor(workspace_manager, logger)
            
            # Mock agent function
            async def mock_agent(workspace, manager, log):
                return AgentResult(result="Success", session_id="test")
            
            # Execute in parallel
            results = await executor.execute_parallel(
                workspaces=workspaces,
                agent_fn=mock_agent,
            )
            
            assert len(results) == 2
            assert all(r.success for r in results)
        finally:
            Path(yaml_path).unlink()


class TestParallelGenerationExecution:
    """Tests for parallel generation execution."""
    
    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = Mock(spec=Settings)
        settings.AGENT_BASE_PATH = "/agent"
        settings.ANTHROPIC_API_KEY = "test-key"
        settings.OPENROUTER_API_KEY = "test-or-key"
        settings.P10Y_BASE_URL = "https://p10y.test"
        settings.P10Y_API_KEY = "p10y-key"
        return settings
    
    @pytest.fixture
    def workspaces_with_p10y(self):
        """Create test workspaces with P10Y configuration."""
        return [
            WorkspaceSettings(
                name="workspace-1",
                workspace_path="/workspace1",
                provider="anthropic",
                model="claude-sonnet-4-0",
                p10y_repository_id=101,
            ),
            WorkspaceSettings(
                name="workspace-2",
                workspace_path="/workspace2",
                provider="openrouter",
                model="gemini-2.5-pro",
                p10y_repository_id=102,
            ),
            WorkspaceSettings(
                name="workspace-3",
                workspace_path="/workspace3",
                provider="openrouter",
                model="gpt-4o",
                p10y_repository_id=103,
            ),
        ]
    
    @pytest.mark.asyncio
    async def test_parallel_generation_success(self, mock_settings, workspaces_with_p10y):
        """Test successful parallel generation execution."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        
        # Mock generation function that returns WorkspaceEstimation
        async def mock_generation_fn(workspace, **kwargs):
            return WorkspaceEstimation(
                workspace_name=workspace.name,
                workspace_path=str(workspace.workspace_path),
                total_hours=100.0 + workspace.p10y_repository_id,  # Unique value per workspace
                total_effective_output=50.0,
                commits_count=10,
                component_breakdown={
                    "backend": ComponentEstimation(
                        component_name="backend",
                        hours=50.0,
                        new_work=40.0,
                        refactor=8.0,
                        rework=2.0,
                        quality_score=0.85
                    ),
                    "frontend": ComponentEstimation(
                        component_name="frontend",
                        hours=50.0,
                        new_work=40.0,
                        refactor=8.0,
                        rework=2.0,
                        quality_score=0.85
                    ),
                },
                estimation_metrics=EstimationMetrics(
                    new_work=80.0,
                    refactor=16.0,
                    rework=4.0,
                    removed_work=0.0,
                    quality_score=0.85,
                    effective_output=80.0,
                    total_output=100.0
                )
            )
        
        results = await execute_generation_parallel(
            workspaces=workspaces_with_p10y,
            workspace_manager=workspace_manager,
            generation_fn=mock_generation_fn,
            generation_args={},
            logger=logger,
        )
        
        assert len(results) == 3
        assert all(r.success for r in results)
        assert results[0].estimation.total_hours == 201.0  # 100 + 101
        assert results[1].estimation.total_hours == 202.0  # 100 + 102
        assert results[2].estimation.total_hours == 203.0  # 100 + 103
    
    @pytest.mark.asyncio
    async def test_parallel_generation_with_missing_p10y_config(self, mock_settings):
        """Test generation with workspace missing P10Y configuration."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        
        # Workspace without P10Y repository ID
        workspaces = [
            WorkspaceSettings(
                name="no-p10y-workspace",
                workspace_path="/workspace1",
                provider="anthropic",
                model="claude-sonnet-4-0",
                p10y_repository_id=None  # Missing P10Y config
            ),
        ]
        
        async def mock_generation_fn(workspace, **kwargs):
            return WorkspaceEstimation(
                workspace_name=workspace.name,
                workspace_path=str(workspace.workspace_path),
                total_hours=100.0,
                total_effective_output=50.0,
                commits_count=10,
                component_breakdown={},
                estimation_metrics=EstimationMetrics(
                    new_work=80.0,
                    refactor=16.0,
                    rework=4.0,
                    removed_work=0.0,
                    quality_score=0.85,
                    effective_output=80.0,
                    total_output=100.0
                )
            )
        
        results = await execute_generation_parallel(
            workspaces=workspaces,
            workspace_manager=workspace_manager,
            generation_fn=mock_generation_fn,
            generation_args={},
            logger=logger,
        )
        
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].warning is not None
        assert "No P10Y repository ID" in results[0].warning
    
    @pytest.mark.asyncio
    async def test_parallel_generation_with_file_not_found(self, mock_settings, workspaces_with_p10y):
        """Test generation with missing commit files."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        
        # Mock generation function that raises FileNotFoundError for one workspace
        async def mock_generation_fn(workspace, **kwargs):
            if workspace.name == "workspace-2":
                raise FileNotFoundError("git metadata not found")
            return WorkspaceEstimation(
                workspace_name=workspace.name,
                workspace_path=str(workspace.workspace_path),
                total_hours=100.0,
                total_effective_output=50.0,
                commits_count=10,
                component_breakdown={},
                estimation_metrics=EstimationMetrics(
                    new_work=80.0,
                    refactor=16.0,
                    rework=4.0,
                    removed_work=0.0,
                    quality_score=0.85,
                    effective_output=80.0,
                    total_output=100.0
                )
            )
        
        results = await execute_generation_parallel(
            workspaces=workspaces_with_p10y,
            workspace_manager=workspace_manager,
            generation_fn=mock_generation_fn,
            generation_args={},
            logger=logger,
        )
        
        assert len(results) == 3
        assert results[0].success is True
        assert results[1].success is False  # FileNotFoundError
        assert results[2].success is True
        assert "git metadata" in results[1].error
        assert results[1].warning == "Commit files not found - workspace may not have completed code generation"
    
    @pytest.mark.asyncio
    async def test_parallel_generation_with_mixed_results(self, mock_settings, workspaces_with_p10y):
        """Test generation with some successes, some failures, and some missing data."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        
        # Mock generation function with various outcomes
        async def mock_generation_fn(workspace, **kwargs):
            if workspace.name == "workspace-1":
                # Success
                return WorkspaceEstimation(
                    workspace_name=workspace.name,
                    workspace_path=str(workspace.workspace_path),
                    total_hours=150.0,
                    total_effective_output=75.0,
                    commits_count=15,
                    component_breakdown={},
                    estimation_metrics=EstimationMetrics(
                        new_work=120.0,
                        refactor=24.0,
                        rework=6.0,
                        removed_work=0.0,
                        quality_score=0.90,
                        effective_output=120.0,
                        total_output=150.0
                    )
                )
            elif workspace.name == "workspace-2":
                # Returns None (no commits found)
                return None
            else:
                # Error
                raise ValueError("P10Y API error")
        
        results = await execute_generation_parallel(
            workspaces=workspaces_with_p10y,
            workspace_manager=workspace_manager,
            generation_fn=mock_generation_fn,
            generation_args={},
            logger=logger,
        )
        
        assert len(results) == 3
        assert results[0].success is True
        assert results[0].estimation.total_hours == 150.0
        
        assert results[1].success is False
        assert results[1].warning == "No estimation generated (possibly missing commits)"
        
        assert results[2].success is False
        assert "P10Y API error" in results[2].error
    
    @pytest.mark.asyncio
    async def test_parallel_generation_graceful_degradation(self, mock_settings):
        """Test that generation continues even when some workspaces fail."""
        logger = Mock()
        workspace_manager = WorkspaceManager(mock_settings, logger)
        
        workspaces = [
            WorkspaceSettings(
                name=f"workspace-{i}",
                workspace_path=f"/workspace{i}",
                provider="anthropic",
                model="claude-sonnet-4-0",
                p10y_repository_id=100 + i
            )
            for i in range(5)
        ]
        
        # Mock generation that fails for even-numbered workspaces
        async def mock_generation_fn(workspace, **kwargs):
            workspace_num = int(workspace.name.split('-')[1])
            if workspace_num % 2 == 0:
                raise RuntimeError(f"Simulated failure for workspace {workspace_num}")
            return WorkspaceEstimation(
                workspace_name=workspace.name,
                workspace_path=str(workspace.workspace_path),
                total_hours=100.0 * workspace_num,
                total_effective_output=50.0 * workspace_num,
                commits_count=workspace_num * 5,
                component_breakdown={},
                estimation_metrics=EstimationMetrics(
                    new_work=80.0,
                    refactor=16.0,
                    rework=4.0,
                    removed_work=0.0,
                    quality_score=0.85,
                    effective_output=80.0,
                    total_output=100.0
                )
            )
        
        results = await execute_generation_parallel(
            workspaces=workspaces,
            workspace_manager=workspace_manager,
            generation_fn=mock_generation_fn,
            generation_args={},
            logger=logger,
        )
        
        assert len(results) == 5
        
        # Check success/failure pattern
        successes = [r for r in results if r.success]
        failures = [r for r in results if not r.success]
        
        assert len(successes) == 2  # workspaces 1, 3 (odd indices)
        assert len(failures) == 3   # workspaces 0, 2, 4 (even indices)
        
        # Verify successful generations have correct data
        for success_result in successes:
            assert success_result.estimation is not None
            assert success_result.estimation.total_hours > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestWorkspaceAbortEvents:
    """The executor's except-branch is the single workspace_aborted recording site."""

    @pytest.fixture
    def mock_settings(self):
        settings = Mock(spec=Settings)
        settings.AGENT_BASE_PATH = "/agent"
        settings.ANTHROPIC_API_KEY = "test-key"
        settings.OPENROUTER_API_KEY = "test-or-key"
        settings.OPENROUTER_BASE_URL = "https://openrouter.ai/api"
        return settings

    @pytest.fixture
    def workspace(self):
        return [
            WorkspaceSettings(
                name="ws-1",
                workspace_path="/agent/workspace1",
                provider="anthropic",
                model="claude-sonnet-4-0",
            )
        ]

    @pytest.fixture
    def recorders(self):
        from unittest.mock import AsyncMock

        from app.core.telemetry_context import TelemetryContext

        TelemetryContext.clear_context()
        events = AsyncMock()
        states = AsyncMock()
        TelemetryContext.set_agent_error_event_handler(events)
        TelemetryContext.set_workspace_agent_state_handler(states)
        TelemetryContext.set_user_context(generation_id="est-1")
        yield events, states
        TelemetryContext.clear_context()

    @pytest.mark.asyncio
    async def test_failure_records_aborted_event_with_error_type_and_phase(
        self, mock_settings, workspace, recorders
    ):
        from app.schemas.agent_error_events import AgentErrorEventKind, WorkspaceAgentState
        from app.services.claude_code import WorkspaceAbortedError

        events, states = recorders
        executor = ParallelAgentExecutor(WorkspaceManager(mock_settings, Mock()), Mock())

        async def aborting_agent(ws, manager, log):
            raise WorkspaceAbortedError(
                "[ws-1] Phase 12 lost connection to the API. Error: socket connection was closed",
                phase=12,
            )

        results = await executor.execute_parallel(workspaces=workspace, agent_fn=aborting_agent)

        assert results[0].success is False
        events.assert_awaited_once()
        _, event = events.await_args.args
        assert event.kind == AgentErrorEventKind.WORKSPACE_ABORTED
        assert event.workspace_id == "ws-1"
        assert event.phase == 12
        assert event.error_type is not None  # classified connection error
        states.assert_awaited_once()
        assert states.await_args.args[1:] == ("ws-1", WorkspaceAgentState.ABORTED)

    @pytest.mark.asyncio
    async def test_cancellation_records_no_event(self, mock_settings, workspace, recorders):
        from app.state.exceptions import GenerationCancelledError

        events, states = recorders
        executor = ParallelAgentExecutor(WorkspaceManager(mock_settings, Mock()), Mock())

        async def cancelled_agent(ws, manager, log):
            raise GenerationCancelledError("est-1")

        with pytest.raises(GenerationCancelledError):
            await executor.execute_parallel(workspaces=workspace, agent_fn=cancelled_agent)

        events.assert_not_awaited()
        states.assert_not_awaited()
