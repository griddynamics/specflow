"""The models of the Jira sync (docs/jira-sync/api.md "Shapes"): the arguments and results of ``sync_to_jira`` and
``jira_sync_status``, and the sync status the settings answer carries. Lists only, no dict fields."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from agent_contracts.models import DEFAULT_LIMIT, Limit, ProjectArguments

JiraSyncState = Literal["idle", "running", "succeeded", "partial", "failed", "skipped"]
JiraItemKind = Literal["epic", "story", "subtask"]


class JiraSyncArgs(ProjectArguments):
    """``sync_to_jira`` takes the project and nothing else."""


class JiraSyncStatusArgs(ProjectArguments):
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class JiraSyncCounts(BaseModel):
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    links_added: int = 0
    links_removed: int = 0
    ranked: int = 0
    unchanged: int = 0
    failed: int = 0


class JiraSyncProgress(BaseModel):
    """Where a running pass is: ``done`` of ``total`` units of the current ``step`` are handled."""

    step: Literal["issues", "links", "order"] = Field(
        description="What the pass is doing: creating and updating issues, adding links, or ranking them."
    )
    done: int = Field(ge=0, description="Units of this step handled so far.")
    total: int = Field(
        ge=0,
        description=(
            "Units of this step the pass will handle; for issues it can grow once, when the updates are counted."
        ),
    )


class JiraSyncStatus(BaseModel):
    project_id: str
    enabled: bool
    status: JiraSyncState
    queued: bool
    trigger: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    counts: JiraSyncCounts | None
    jira_project: str
    epic_key: str | None
    progress: JiraSyncProgress | None = Field(
        default=None,
        description="Set only while the pass runs and has begun its work; null when queued or finished.",
    )


class JiraSyncItem(BaseModel):
    node_id: str
    kind: JiraItemKind
    jira_key: str | None
    url: str | None
    removed: bool
    error_code: str | None


class JiraSyncReport(JiraSyncStatus):
    items: list[JiraSyncItem]
    next_cursor: str | None
