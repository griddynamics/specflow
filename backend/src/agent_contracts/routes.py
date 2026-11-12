"""Where the API serves each tool (architecture.md, API agent routes)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

AGENT_PREFIX: Final = "/api/v1/agent"
AGENT_ROUTES: Final[Mapping[str, str]] = {
    "list_projects": f"{AGENT_PREFIX}/projects",
    "list_features": f"{AGENT_PREFIX}/features",
    "list_tasks": f"{AGENT_PREFIX}/tasks",
    "list_feature_tasks": f"{AGENT_PREFIX}/feature-tasks",
    "get_task_path": f"{AGENT_PREFIX}/task-path",
    "get_task_brief": f"{AGENT_PREFIX}/task-brief",
    "get_story_brief": f"{AGENT_PREFIX}/story-brief",
    "trace": f"{AGENT_PREFIX}/trace",
    "get_node": f"{AGENT_PREFIX}/node",
    "search_spec": f"{AGENT_PREFIX}/spec-search",
    "similar": f"{AGENT_PREFIX}/similar",
    "get_evidence": f"{AGENT_PREFIX}/evidence",
    "path": f"{AGENT_PREFIX}/node-path",
    "expand_brief": f"{AGENT_PREFIX}/brief-expansion",
}
USER_ID_HEADER: Final = "X-SpecFlow-User-Id"
