"""The shapes of the Linear plan routes (``/projects/{pid}/linear-plan``): the target form's answer, its input, the
team and project lists and the access check. The sync status is the agent contract's ``LinearSyncStatus``, so the web
form and the MCP tools read one shape."""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from agent_contracts.linear_models import LinearSyncStatus

ID_MAX: Final = 100

LinearAccessReason = Literal[
    "linear_not_connected",
    "linear_reconnect_required",
    "linear_unreachable",
    "linear_forbidden",
    "linear_rate_limited",
    "linear_team_not_found",
    "linear_project_not_found",
    "linear_invalid",
]
LinearAccessField = Literal["team", "project"]


class LinearPlanAccessIn(BaseModel):
    """The typed destination before it is saved: a team of the member and one of its projects, or none to let
    Graphico create the project (the check then only proves the member sees the team)."""

    model_config = ConfigDict(extra="forbid")

    team_id: str = Field(min_length=1, max_length=ID_MAX)
    project_id: str | None = Field(default=None, min_length=1, max_length=ID_MAX)

    @field_validator("project_id", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value


class LinearPlanIn(LinearPlanAccessIn):
    enabled: StrictBool


class LinearTeamChoice(BaseModel):
    id: str
    key: str
    name: str


class LinearProjectChoice(BaseModel):
    id: str
    name: str
    url: str


class LinearTeamsOut(BaseModel):
    """The teams the acting member sees, and the workspace they belong to."""

    workspace: str
    teams: list[LinearTeamChoice]


class LinearProjectsOut(BaseModel):
    projects: list[LinearProjectChoice]


class LinearPlanOut(BaseModel):
    """The Linear plan destination of a project: null fields while there is none. ``available`` is false when the Linear
    integration is off on this server."""

    available: bool
    team_id: str | None
    team_key: str | None
    project_id: str | None
    project_name: str | None
    project_url: str | None
    created_by_graphico: bool
    enabled: bool
    sync: LinearSyncStatus | None
    workspace: str | None = Field(description="The Linear workspace the export acts in, or null.")
    connected_by_me: bool


class LinearPlanAccessOk(BaseModel):
    result: Literal["ok"] = "ok"
    team_key: str
    project_name: str | None


class LinearPlanAccessFailed(BaseModel):
    result: Literal["failed"] = "failed"
    reason: LinearAccessReason
    field: LinearAccessField | None


LinearPlanAccessResult = Annotated[LinearPlanAccessOk | LinearPlanAccessFailed, Field(discriminator="result")]
