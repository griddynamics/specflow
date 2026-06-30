"""Aggregated tool / MCP usage per agent_query (Claude Code SDK message stream)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class ToolUsage:
    """Per-tool aggregate within an AgentUsage bucket."""

    name: str
    count: int
    tokens: Optional[int] = None


@dataclass
class AgentUsage:
    """Tool call counts and optional token sums for one scope (main vs subagents, tools vs MCP)."""

    tools_usage_count: List[ToolUsage]
    tools_tokens: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable shape for logs and telemetry."""
        return {
            "tools_usage_count": [
                {"name": t.name, "count": t.count, "tokens": t.tokens} for t in self.tools_usage_count
            ],
            "tools_tokens": self.tools_tokens,
        }
