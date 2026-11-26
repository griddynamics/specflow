"""The bodies of the Linear gap routes (docs/linear/gaps.md "Routes")."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

TEAM_KEY_PATTERN = r"^[A-Z][A-Z0-9]{0,14}$"
REF_MAX = 300
Grouping = Literal["one", "area"]


class LinearGapAccessCheckIn(BaseModel):
    """The typed team and optional Linear project of the gap channel before it is saved: the team key is trimmed and
    upper-cased, a blank project is null. The project is its name or a link to it."""

    model_config = ConfigDict(extra="forbid")

    team_key: str = Field(pattern=TEAM_KEY_PATTERN)
    linear_project: str | None = Field(default=None, max_length=REF_MAX)

    @field_validator("team_key", mode="before")
    @classmethod
    def _normalise_team(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("linear_project", mode="before")
    @classmethod
    def _normalise_project(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None


class LinearGapsIn(LinearGapAccessCheckIn):
    grouping: Grouping
    enabled: StrictBool


class LinearGapsOut(BaseModel):
    """The Linear gap channel of a project: the target (null fields while there is none), the counts of its issues and
    the parent issue. ``available`` is false when the server has no Linear OAuth app or gap runs are off."""

    available: bool
    enabled: bool
    team_id: str | None
    team_key: str | None
    linear_project_id: str | None
    grouping: Grouping | None
    parent_identifier: str | None
    parent_url: str | None
    posted: int
    answered: int
    error_code: str | None = Field(description="The closed code of the last refused post or read, or null.")


class LinearAccessOk(BaseModel):
    result: Literal["ok"] = "ok"
    team_name: str
    linear_project_name: str | None


class LinearAccessFailed(BaseModel):
    result: Literal["failed"] = "failed"
    reason: str = Field(description="A closed Linear API code, for example linear_team_not_found.")
    field: Literal["team_key", "linear_project"] | None


LinearAccessResult = Annotated[LinearAccessOk | LinearAccessFailed, Field(discriminator="result")]


class WebhookAck(BaseModel):
    """The answer to a delivery: it was verified and handed on."""

    ok: bool
