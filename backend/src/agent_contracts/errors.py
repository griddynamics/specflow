"""The tool error codes, shared by the API's agent routes and the MCP role (tools.md Error codes)."""

from __future__ import annotations

from typing import Final, Literal

ErrorCode = Literal[
    "project_not_found",
    "invalid_cursor",
    "no_plan",
    "task_not_found",
    "feature_not_found",
    "graph_unavailable",
    "database_unavailable",
    "internal_error",
]

PROJECT_NOT_FOUND: Final = "project_not_found"
INVALID_CURSOR: Final = "invalid_cursor"
NO_PLAN: Final = "no_plan"
TASK_NOT_FOUND: Final = "task_not_found"
FEATURE_NOT_FOUND: Final = "feature_not_found"
GRAPH_UNAVAILABLE: Final = "graph_unavailable"
DATABASE_UNAVAILABLE: Final = "database_unavailable"
INTERNAL_ERROR: Final = "internal_error"
