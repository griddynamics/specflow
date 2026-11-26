"""The models of the Linear plan export: the arguments and results of ``sync_to_linear`` and ``linear_sync_status``, and
the sync status the plan form's answer carries. Same shape as ``agent_contracts.jira_models``. Lists only, no dict
fields."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from agent_contracts.models import DEFAULT_LIMIT, Limit, ProjectArguments

LinearSyncState = Literal["idle", "running", "succeeded", "partial", "failed", "skipped"]
LinearItemKind = Literal["story", "subtask"]


class LinearSyncArgs(ProjectArguments):
    """``sync_to_linear`` takes the project and nothing else."""


class LinearSyncStatusArgs(ProjectArguments):
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class LinearSyncCounts(BaseModel):
    created: int = 0
    updated: int = 0
    commented: int = 0
    removed: int = 0
    relations_added: int = 0
    estimate_refused: int = 0
    unchanged: int = 0
    failed: int = 0


class LinearSyncProgress(BaseModel):
    """Where a running pass is: ``done`` of ``total`` units of the current ``step`` are handled."""

    step: Literal["issues", "relations"] = Field(
        description="What the pass is doing: creating and updating issues, or adding the blocked-by relations."
    )
    done: int = Field(ge=0, description="Units of this step handled so far.")
    total: int = Field(
        ge=0,
        description=(
            "Units of this step the pass will handle; for issues it can grow once, when the updates are counted."
        ),
    )


class LinearSyncStatus(BaseModel):
    project_id: str
    enabled: bool
    status: LinearSyncState
    queued: bool
    trigger: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    counts: LinearSyncCounts | None
    team_key: str
    linear_project: str
    linear_project_url: str | None
    progress: LinearSyncProgress | None = Field(
        default=None,
        description="Set only while the pass runs and has begun its work; null when queued or finished.",
    )


class LinearSyncItem(BaseModel):
    node_id: str
    kind: LinearItemKind
    identifier: str | None
    url: str | None
    removed: bool
    error_code: str | None


class LinearSyncReport(LinearSyncStatus):
    items: list[LinearSyncItem]
    next_cursor: str | None
