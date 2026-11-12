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
    "area_not_found",
    "story_not_found",
    "node_not_found",
    "story_claimed",
    "not_available",
    "partly_claimed",
    "plan_applying",
    "claim_mismatch",
    "invalid_commit",
    "invalid_arguments",
    "unknown_relation",
    "finding_limit",
]

PROJECT_NOT_FOUND: Final = "project_not_found"
INVALID_CURSOR: Final = "invalid_cursor"
NO_PLAN: Final = "no_plan"
TASK_NOT_FOUND: Final = "task_not_found"
FEATURE_NOT_FOUND: Final = "feature_not_found"
GRAPH_UNAVAILABLE: Final = "graph_unavailable"
DATABASE_UNAVAILABLE: Final = "database_unavailable"
INTERNAL_ERROR: Final = "internal_error"
AREA_NOT_FOUND: Final = "area_not_found"
STORY_NOT_FOUND: Final = "story_not_found"
NODE_NOT_FOUND: Final = "node_not_found"
STORY_CLAIMED: Final = "story_claimed"
NOT_AVAILABLE: Final = "not_available"
PARTLY_CLAIMED: Final = "partly_claimed"
PLAN_APPLYING: Final = "plan_applying"
CLAIM_MISMATCH: Final = "claim_mismatch"
INVALID_COMMIT: Final = "invalid_commit"
INVALID_ARGUMENTS: Final = "invalid_arguments"
UNKNOWN_RELATION: Final = "unknown_relation"
FINDING_LIMIT: Final = "finding_limit"
