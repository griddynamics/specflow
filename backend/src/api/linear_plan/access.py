"""The access check and the destination lookup of the Linear plan form: what the member the export acts as can reach
before a destination is saved. Results are closed reasons, never Linear text. Permission to create issues cannot be
tried without writing, so a missing write scope shows on the first pass (``linear_forbidden``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from api.linear_plan.models import (
    LinearAccessField,
    LinearAccessReason,
    LinearPlanAccessFailed,
    LinearPlanAccessOk,
)
from linear.errors import LinearCode, LinearError
from linear.gateway import LinearGateway
from linear.models import Project, Team

_REASONS: Final[dict[LinearCode, LinearAccessReason]] = {
    LinearCode.UNAUTHORIZED: "linear_reconnect_required",
    LinearCode.FORBIDDEN: "linear_forbidden",
    LinearCode.RATE_LIMITED: "linear_rate_limited",
    LinearCode.UNAVAILABLE: "linear_unreachable",
}


@dataclass(frozen=True)
class Resolved:
    """The team of a destination and, when one was named, its Linear project."""

    team: Team
    project: Project | None


class AccessRefusedError(Exception):
    """The destination cannot be used; ``reason`` is the closed code."""

    def __init__(self, reason: LinearAccessReason, cause: LinearError | None = None) -> None:
        super().__init__(reason)
        self.reason: LinearAccessReason = reason
        self.cause = cause  # the Linear failure behind it, if any


def reason_of(error: LinearError, *, missing: LinearAccessReason) -> LinearAccessReason:
    """The closed reason of a Linear failure; ``missing`` is the answer of "not found"."""

    if error.code is LinearCode.NOT_FOUND:
        return missing
    return _REASONS.get(error.code, "linear_invalid")


async def resolve(gateway: LinearGateway, team_id: str, project_id: str | None) -> Resolved:
    """The team the member sees and, with ``project_id``, that team's project."""

    try:
        team = next((found for found in await gateway.teams() if found.id == team_id), None)
        if team is None:
            raise AccessRefusedError("linear_team_not_found")
        if project_id is None:
            return Resolved(team, None)
        project = await gateway.project(project_id)
    except LinearError as error:
        raise AccessRefusedError(reason_of(error, missing="linear_project_not_found"), error) from None
    if team_id not in project.team_ids:
        raise AccessRefusedError("linear_project_not_found")
    return Resolved(team, project)


def field_of(reason: LinearAccessReason) -> LinearAccessField | None:
    if reason == "linear_team_not_found":
        return "team"
    return "project" if reason == "linear_project_not_found" else None


async def check_access(
    gateway: LinearGateway, team_id: str, project_id: str | None
) -> LinearPlanAccessOk | LinearPlanAccessFailed:
    try:
        found = await resolve(gateway, team_id, project_id)
    except AccessRefusedError as refused:
        return LinearPlanAccessFailed(reason=refused.reason, field=field_of(refused.reason))
    name = None if found.project is None else found.project.name
    return LinearPlanAccessOk(team_key=found.team.key, project_name=name)
