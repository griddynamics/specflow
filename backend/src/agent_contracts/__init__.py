"""The contract between the API's agent routes and the MCP role: pure pydantic models, error codes and route paths.

Imports only the standard library, pydantic and the public models of ``graph.plan``.
"""

from __future__ import annotations

from agent_contracts.errors import (
    DATABASE_UNAVAILABLE,
    FEATURE_NOT_FOUND,
    GRAPH_UNAVAILABLE,
    INTERNAL_ERROR,
    INVALID_CURSOR,
    NO_PLAN,
    PROJECT_NOT_FOUND,
    TASK_NOT_FOUND,
    ErrorCode,
)
from agent_contracts.models import (
    DEFAULT_LIMIT,
    MAX_CURSOR_CHARS,
    MAX_LIMIT,
    MAX_NEIGHBOURS,
    FeatureItem,
    FeatureList,
    FeatureTaskList,
    GetTaskBriefArgs,
    GetTaskPathArgs,
    Limit,
    ListFeaturesArgs,
    ListFeatureTasksArgs,
    ListProjectsArgs,
    ListTasksArgs,
    ProjectArguments,
    ProjectItem,
    ProjectList,
    TaskBriefResult,
    TaskList,
    TaskPath,
    TaskStatusFilter,
)
from agent_contracts.routes import AGENT_PREFIX, AGENT_ROUTES, USER_ID_HEADER
from graph.plan.models import FeatureRef, TaskBrief, TaskItem

__all__ = [
    "AGENT_PREFIX",
    "AGENT_ROUTES",
    "DATABASE_UNAVAILABLE",
    "DEFAULT_LIMIT",
    "FEATURE_NOT_FOUND",
    "GRAPH_UNAVAILABLE",
    "INTERNAL_ERROR",
    "INVALID_CURSOR",
    "MAX_CURSOR_CHARS",
    "MAX_LIMIT",
    "MAX_NEIGHBOURS",
    "NO_PLAN",
    "PROJECT_NOT_FOUND",
    "TASK_NOT_FOUND",
    "USER_ID_HEADER",
    "ErrorCode",
    "FeatureItem",
    "FeatureList",
    "FeatureRef",
    "FeatureTaskList",
    "GetTaskBriefArgs",
    "GetTaskPathArgs",
    "Limit",
    "ListFeatureTasksArgs",
    "ListFeaturesArgs",
    "ListProjectsArgs",
    "ListTasksArgs",
    "ProjectArguments",
    "ProjectItem",
    "ProjectList",
    "TaskBrief",
    "TaskBriefResult",
    "TaskItem",
    "TaskList",
    "TaskPath",
    "TaskStatusFilter",
]
