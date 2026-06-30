"""Data models for workflow execution metrics collection."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class AgentQueryMetrics:
    """Metrics from a single agent_query call."""

    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: Optional[str]
    total_cost_usd: float
    usage: dict
    timestamp: str  # ISO format
    tool_usage_breakdown: Optional[dict[str, Any]] = None


@dataclass
class WorkflowExecutionStats:
    """Accumulated stats for a workflow execution."""
    
    workflow_name: str
    agent_queries: List[AgentQueryMetrics] = field(default_factory=list)
    workspace_stats: Dict[str, 'WorkflowExecutionStats'] = field(default_factory=dict)
    workspace_name: Optional[str] = None  # None for parent, workspace name for child
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None
    
    def add_query(self, metrics: AgentQueryMetrics) -> None:
        """Add an agent query metrics to this workflow's stats."""
        self.agent_queries.append(metrics)
    
    def add_workspace_stats(self, workspace_name: str, stats: 'WorkflowExecutionStats') -> None:
        """Merge workspace stats into parent workflow stats."""
        self.workspace_stats[workspace_name] = stats
    
    def mark_completed(self) -> None:
        """Mark this workflow as completed with current timestamp."""
        self.completed_at = datetime.now(timezone.utc).isoformat()
    
    @property
    def total_cost_usd(self) -> float:
        """Calculate total cost including own queries and workspace queries."""
        own_cost = sum(q.total_cost_usd for q in self.agent_queries)
        workspace_cost = sum(ws.total_cost_usd for ws in self.workspace_stats.values())
        return own_cost + workspace_cost
    
    @property
    def total_duration_ms(self) -> int:
        """Calculate total duration including own queries and workspace queries."""
        own_duration = sum(q.duration_ms for q in self.agent_queries)
        workspace_duration = sum(ws.total_duration_ms for ws in self.workspace_stats.values())
        return own_duration + workspace_duration
    
    @property
    def total_turns(self) -> int:
        """Calculate total turns including own queries and workspace queries."""
        own_turns = sum(q.num_turns for q in self.agent_queries)
        workspace_turns = sum(ws.total_turns for ws in self.workspace_stats.values())
        return own_turns + workspace_turns
    
    def to_dict(self) -> dict:
        """Serialize stats to dictionary for JSON serialization."""
        return {
            "workflow_name": self.workflow_name,
            "workspace_name": self.workspace_name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_cost_usd": self.total_cost_usd,
            "total_duration_ms": self.total_duration_ms,
            "total_turns": self.total_turns,
            "num_queries": len(self.agent_queries),
            "queries": [
                {
                    "duration_ms": q.duration_ms,
                    "duration_api_ms": q.duration_api_ms,
                    "is_error": q.is_error,
                    "num_turns": q.num_turns,
                    "session_id": q.session_id,
                    "total_cost_usd": q.total_cost_usd,
                    "usage": q.usage,
                    "timestamp": q.timestamp,
                    "tool_usage_breakdown": q.tool_usage_breakdown,
                }
                for q in self.agent_queries
            ],
            "workspaces": {
                name: ws.to_dict()
                for name, ws in self.workspace_stats.items()
            },
        }
