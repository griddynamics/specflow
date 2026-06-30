"""
Unit tests for workflow stats collection.
"""

import pytest
from dataclasses import dataclass

from pydantic import BaseModel

from app.schemas.workflow_stats import AgentQueryMetrics, WorkflowExecutionStats
from app.services.workflow_stats import (
    get_workflow_stats,
    set_workflow_stats,
    record_agent_query_metrics,
    workflow_metrics,
    _add_stats_to_result,
    extract_workspace_codegen_usage,
)


class TestAgentQueryMetrics:
    """Tests for AgentQueryMetrics dataclass."""

    def test_create_metrics(self):
        """Test creating AgentQueryMetrics."""
        metrics = AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="test-session",
            total_cost_usd=0.05,
            usage={"input_tokens": 100, "output_tokens": 200},
            timestamp="2024-01-01T00:00:00Z"
        )
        assert metrics.duration_ms == 1000
        assert metrics.duration_api_ms == 800
        assert metrics.is_error is False
        assert metrics.num_turns == 3
        assert metrics.session_id == "test-session"
        assert metrics.total_cost_usd == 0.05
        assert metrics.usage == {"input_tokens": 100, "output_tokens": 200}
        assert metrics.tool_usage_breakdown is None

    def test_metrics_with_tool_usage_breakdown_in_to_dict(self):
        """Scenario: workflow log query entry includes tool_usage_breakdown."""
        stats = WorkflowExecutionStats(workflow_name="wf")
        breakdown = {
            "main_agent_tool_usage": {"tools_usage_count": [], "tools_tokens": None},
            "main_agent_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
            "subagents_tool_usage": {"tools_usage_count": [], "tools_tokens": None},
            "subagents_mcp_usage": {"tools_usage_count": [], "tools_tokens": None},
        }
        stats.add_query(
            AgentQueryMetrics(
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="s",
                total_cost_usd=0.0,
                usage={},
                timestamp="2024-01-01T00:00:00Z",
                tool_usage_breakdown=breakdown,
            )
        )
        d = stats.to_dict()
        assert d["queries"][0]["tool_usage_breakdown"] == breakdown


class TestWorkflowExecutionStats:
    """Tests for WorkflowExecutionStats dataclass."""

    def test_create_stats(self):
        """Test creating WorkflowExecutionStats."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        assert stats.workflow_name == "test_workflow"
        assert stats.agent_queries == []
        assert stats.workspace_stats == {}
        assert stats.workspace_name is None
        assert stats.completed_at is None
        assert isinstance(stats.started_at, str)

    def test_add_query(self):
        """Test adding agent query metrics."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        metrics = AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="test-session",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        )
        stats.add_query(metrics)
        assert len(stats.agent_queries) == 1
        assert stats.agent_queries[0] == metrics

    def test_add_multiple_queries(self):
        """Test adding multiple queries."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        for i in range(3):
            metrics = AgentQueryMetrics(
                duration_ms=1000 + i,
                duration_api_ms=800,
                is_error=False,
                num_turns=3,
                session_id=f"session-{i}",
                total_cost_usd=0.05,
                usage={},
                timestamp="2024-01-01T00:00:00Z"
            )
            stats.add_query(metrics)
        assert len(stats.agent_queries) == 3

    def test_total_cost_usd(self):
        """Test total_cost_usd property."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="session-1",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="session-2",
            total_cost_usd=0.10,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        assert abs(stats.total_cost_usd - 0.15) < 1e-10

    def test_total_cost_with_workspaces(self):
        """Test total_cost_usd includes workspace costs."""
        parent_stats = WorkflowExecutionStats(workflow_name="parent")
        parent_stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="parent-session",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        
        workspace_stats = WorkflowExecutionStats(
            workflow_name="workspace",
            workspace_name="workspace-1"
        )
        workspace_stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="workspace-session",
            total_cost_usd=0.10,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        
        parent_stats.add_workspace_stats("workspace-1", workspace_stats)
        assert abs(parent_stats.total_cost_usd - 0.15) < 1e-10

    def test_total_duration_ms(self):
        """Test total_duration_ms property."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="session-1",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        stats.add_query(AgentQueryMetrics(
            duration_ms=2000,
            duration_api_ms=1600,
            is_error=False,
            num_turns=3,
            session_id="session-2",
            total_cost_usd=0.10,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        assert stats.total_duration_ms == 3000

    def test_total_turns(self):
        """Test total_turns property."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="session-1",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=5,
            session_id="session-2",
            total_cost_usd=0.10,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        assert stats.total_turns == 8

    def test_add_workspace_stats(self):
        """Test adding workspace stats."""
        parent_stats = WorkflowExecutionStats(workflow_name="parent")
        workspace_stats = WorkflowExecutionStats(
            workflow_name="workspace",
            workspace_name="workspace-1"
        )
        parent_stats.add_workspace_stats("workspace-1", workspace_stats)
        assert "workspace-1" in parent_stats.workspace_stats
        assert parent_stats.workspace_stats["workspace-1"] == workspace_stats

    def test_mark_completed(self):
        """Test marking stats as completed."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        assert stats.completed_at is None
        stats.mark_completed()
        assert stats.completed_at is not None
        assert isinstance(stats.completed_at, str)

    def test_to_dict(self):
        """Test serialization to dict."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="test-session",
            total_cost_usd=0.05,
            usage={"input_tokens": 100, "output_tokens": 200},
            timestamp="2024-01-01T00:00:00Z"
        ))
        stats.mark_completed()
        
        result = stats.to_dict()
        assert result["workflow_name"] == "test_workflow"
        assert result["workspace_name"] is None
        assert result["total_cost_usd"] == 0.05
        assert result["total_duration_ms"] == 1000
        assert result["total_turns"] == 3
        assert result["num_queries"] == 1
        assert len(result["queries"]) == 1
        assert result["queries"][0]["session_id"] == "test-session"
        assert result["completed_at"] is not None

    def test_to_dict_with_workspaces(self):
        """Test serialization with workspace stats."""
        parent_stats = WorkflowExecutionStats(workflow_name="parent")
        parent_stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="parent-session",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        
        workspace_stats = WorkflowExecutionStats(
            workflow_name="workspace",
            workspace_name="workspace-1"
        )
        workspace_stats.add_query(AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="workspace-session",
            total_cost_usd=0.10,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        ))
        
        parent_stats.add_workspace_stats("workspace-1", workspace_stats)
        result = parent_stats.to_dict()
        
        assert "workspaces" in result
        assert "workspace-1" in result["workspaces"]
        assert result["workspaces"]["workspace-1"]["workflow_name"] == "workspace"
        assert result["workspaces"]["workspace-1"]["workspace_name"] == "workspace-1"


class TestContextManagement:
    """Tests for context variable management."""

    def test_get_workflow_stats_returns_none_when_not_set(self):
        """Test that get_workflow_stats returns None when not set."""
        # Clear any existing context
        set_workflow_stats(None)
        stats = get_workflow_stats()
        assert stats is None

    def test_set_and_get_workflow_stats(self):
        """Test setting and getting workflow stats."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        set_workflow_stats(stats)
        retrieved_stats = get_workflow_stats()
        assert retrieved_stats == stats
        assert retrieved_stats.workflow_name == "test_workflow"

    def test_record_agent_query_metrics(self):
        """Test recording agent query metrics."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        set_workflow_stats(stats)
        
        metrics = AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="test-session",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        )
        
        record_agent_query_metrics(metrics)
        assert len(stats.agent_queries) == 1
        assert stats.agent_queries[0] == metrics

    def test_record_agent_query_metrics_no_context(self):
        """Test that recording metrics without context doesn't raise."""
        # Clear context
        set_workflow_stats(None)
        
        metrics = AgentQueryMetrics(
            duration_ms=1000,
            duration_api_ms=800,
            is_error=False,
            num_turns=3,
            session_id="test-session",
            total_cost_usd=0.05,
            usage={},
            timestamp="2024-01-01T00:00:00Z"
        )
        
        # Should not raise
        record_agent_query_metrics(metrics)


class TestWorkflowMetricsDecorator:
    """Tests for workflow_metrics decorator."""

    @pytest.mark.asyncio
    async def test_decorator_creates_stats_context(self):
        """Test that decorator creates stats context."""
        @workflow_metrics("test_workflow")
        async def test_func():
            stats = get_workflow_stats()
            assert stats is not None
            assert stats.workflow_name == "test_workflow"
            return "result"
        
        result = await test_func()
        # Default behavior (add_to_result=False) - result is not wrapped
        assert result == "result"
        
        # Context should be cleared after function completes
        assert get_workflow_stats() is None

    @pytest.mark.asyncio
    async def test_decorator_adds_stats_to_dict_result(self):
        """Test that decorator adds stats to dict result when add_to_result=True."""
        @workflow_metrics("test_workflow", add_to_result=True)
        async def test_func():
            return {"key": "value"}
        
        result = await test_func()
        assert isinstance(result, dict)
        assert "key" in result
        assert result["key"] == "value"
        assert "stats" in result
        assert result["stats"]["workflow_name"] == "test_workflow"

    @pytest.mark.asyncio
    async def test_decorator_wraps_non_dict_result(self):
        """Test that decorator wraps non-dict result when add_to_result=True."""
        @workflow_metrics("test_workflow", add_to_result=True)
        async def test_func():
            return "simple_string"
        
        result = await test_func()
        assert isinstance(result, dict)
        assert result["result"] == "simple_string"
        assert "stats" in result

    @pytest.mark.asyncio
    async def test_decorator_handles_workflow_error(self):
        """Test that decorator handles workflow errors gracefully."""
        @workflow_metrics("test_workflow")
        async def test_func():
            raise ValueError("Test error")
        
        with pytest.raises(ValueError, match="Test error"):
            await test_func()
        
        # Context should be cleared even after error
        assert get_workflow_stats() is None

    @pytest.mark.asyncio
    async def test_decorator_marks_completed_on_success(self):
        """Test that decorator marks stats as completed on success."""
        @workflow_metrics("test_workflow")
        async def test_func():
            stats = get_workflow_stats()
            assert stats is not None
            assert stats.completed_at is None
            return "result"
        
        result = await test_func()
        # Default behavior (add_to_result=False) - result is not wrapped
        assert result == "result"
        # Stats are logged but not included in result

    @pytest.mark.asyncio
    async def test_decorator_with_add_to_result_false(self):
        """Test decorator with add_to_result=False."""
        @workflow_metrics("test_workflow", add_to_result=False)
        async def test_func():
            return {"key": "value"}
        
        result = await test_func()
        # Result should not have stats added
        assert result == {"key": "value"}
        assert "stats" not in result

    @pytest.mark.asyncio
    async def test_decorator_with_workspace_name(self):
        """Test decorator with workspace_name."""
        @workflow_metrics("test_workflow", workspace_name="workspace-1")
        async def test_func():
            stats = get_workflow_stats()
            assert stats is not None
            assert stats.workspace_name == "workspace-1"
            return "result"
        
        result = await test_func()
        # Default behavior (add_to_result=False) - result is not wrapped
        assert result == "result"


class TestAddStatsToResult:
    """Tests for _add_stats_to_result helper function."""

    def test_add_stats_to_dict(self):
        """Test adding stats to dict result."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        result = {"key": "value"}
        new_result = _add_stats_to_result(result, stats)
        assert new_result == result  # Same dict object
        assert "stats" in new_result
        assert new_result["stats"]["workflow_name"] == "test_workflow"

    def test_add_stats_to_string(self):
        """Test adding stats to string result (wraps in dict)."""
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        result = "simple_string"
        new_result = _add_stats_to_result(result, stats)
        assert isinstance(new_result, dict)
        assert new_result["result"] == "simple_string"
        assert "stats" in new_result

    def test_add_stats_to_dataclass(self):
        """Test adding stats to dataclass result."""
        @dataclass
        class TestResult:
            value: str
        
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        result = TestResult(value="test")
        new_result = _add_stats_to_result(result, stats)
        # Should try to add stats as attribute
        assert hasattr(new_result, "stats") or isinstance(new_result, dict)

    def test_add_stats_to_pydantic_v2_model(self):
        """Test adding stats to Pydantic v2 model."""
        class TestResult(BaseModel):
            value: str
            model_config = {"extra": "allow"}

        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        result = TestResult(value="test")
        new_result = _add_stats_to_result(result, stats)
        # Should have stats attribute or be wrapped
        assert hasattr(new_result, "stats") or isinstance(new_result, dict)

    def test_add_stats_handles_serialization_error(self):
        """Test that serialization errors don't break the function."""
        # Create a stats object that might fail serialization
        stats = WorkflowExecutionStats(workflow_name="test_workflow")
        result = {"key": "value"}
        
        # Mock to_dict to raise an error
        original_to_dict = stats.to_dict
        stats.to_dict = lambda: (_ for _ in ()).throw(Exception("Serialization error"))
        
        # Should return original result
        new_result = _add_stats_to_result(result, stats)
        assert new_result == result
        
        # Restore
        stats.to_dict = original_to_dict


class TestExtractWorkspaceCodegenUsage:
    """extract_workspace_codegen_usage aggregates nested workspace query usage."""

    def test_sums_tokens_turns_and_cache_buckets(self) -> None:
        wrapped = {
            "result": {},
            "stats": {
                "workspaces": {
                    "ws-a": {
                        "queries": [
                            {
                                "num_turns": 2,
                                "usage": {
                                    "input_tokens": 100,
                                    "output_tokens": 40,
                                    "cache_creation_input_tokens": 7,
                                    "cache_read_input_tokens": 3,
                                },
                            },
                            {
                                "num_turns": 1,
                                "usage": {
                                    "input_tokens": 10,
                                    "output_tokens": 5,
                                    "cache_creation_input_tokens": 0,
                                    "cache_read_input_tokens": 0,
                                },
                            },
                        ]
                    }
                }
            },
        }
        out = extract_workspace_codegen_usage(wrapped)
        assert set(out.keys()) == {"ws-a"}
        mu = out["ws-a"]
        assert mu.num_turns == 3
        assert mu.input_tokens == 110
        assert mu.output_tokens == 45
        assert mu.cache_write_tokens == 7
        assert mu.cache_read_tokens == 3
        assert mu.total_tokens == mu.input_tokens + mu.output_tokens + mu.cache_write_tokens + mu.cache_read_tokens
