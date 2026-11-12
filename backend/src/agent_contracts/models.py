"""The argument and result models of the six tools, moved verbatim from the MCP tool modules."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, Field

from graph.plan.models import FeatureRef, TaskBrief, TaskItem

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 100
MAX_CURSOR_CHARS: Final = 512  # FR-MCP-TOOL-07
MAX_NEIGHBOURS: Final = 100

Limit = Annotated[int, Field(ge=1, le=MAX_LIMIT)]  # the input schema bounds (FR-MCP-TOOL-01, TOOL-03)

TaskStatusFilter = Literal["pending", "in_progress", "completed", "blocked", "failed"]


class ProjectArguments(BaseModel):
    """The arguments of a project-scoped tool start with the project id, a plain string (FR-MCP-ACC-03)."""

    project_id: str = Field(
        description=(
            "The id of the SpecFlow project: the bound project's project_id from .specflow/config.json, "
            "or one from list_projects."
        )
    )


class ListProjectsArgs(BaseModel):
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class ProjectItem(BaseModel):
    project_id: str
    name: str
    created_at: datetime


class ProjectList(BaseModel):
    items: list[ProjectItem]
    next_cursor: str | None


class ListFeaturesArgs(ProjectArguments):
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class FeatureItem(BaseModel):
    spec_id: str
    name: str
    # Stored feature attributes are text (tools.md). An ontology whose feature type stores a non-text
    # attribute fails here and the tool answers internal_error (handoff Open issues, R-04).
    attributes: dict[str, str]


class FeatureList(BaseModel):
    project_id: str
    total_count: int
    items: list[FeatureItem]
    next_cursor: str | None


class ListTasksArgs(ProjectArguments):
    ready_only: bool = Field(
        default=True,
        description="Only tasks that can be started now (pending, every prerequisite completed).",
    )
    status: TaskStatusFilter | None = Field(default=None, description="Only tasks of this status.")
    area_id: str | None = Field(default=None, description="Only tasks of this work area (an area_id of a task).")
    story_id: str | None = Field(default=None, description="Only tasks of this story (a story_id of a task).")
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class TaskList(BaseModel):
    project_id: str
    total_count: int
    items: list[TaskItem]
    next_cursor: str | None


class ListFeatureTasksArgs(ProjectArguments):
    feature_id: str = Field(description="The spec_id of a feature from list_features.")
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class FeatureTaskList(BaseModel):
    project_id: str
    feature: FeatureRef
    total_count: int
    items: list[TaskItem]
    next_cursor: str | None


class GetTaskPathArgs(ProjectArguments):
    task_id: str = Field(description="The task_id of a task from list_tasks or list_feature_tasks.")
    limit: Limit = DEFAULT_LIMIT
    cursor: str | None = None


class TaskPath(BaseModel):
    project_id: str
    task_id: str
    total_count: int
    completed: int
    remaining: int
    items: list[TaskItem]
    next_cursor: str | None


class GetTaskBriefArgs(ProjectArguments):
    task_id: str = Field(description="The task_id of a task from list_tasks.")
    neighbours: int = Field(
        default=30,
        ge=0,
        le=MAX_NEIGHBOURS,
        description="How many neighbouring specification nodes to include.",
    )


class TaskBriefResult(BaseModel):
    project_id: str
    brief: TaskBrief
