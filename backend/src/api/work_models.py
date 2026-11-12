"""The web models of the findings list and the claims board: what the people routes answer and take.

Holders and authors are names (``Holder``): no user id, email or claim token leaves the API (A90).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from agent_contracts import Holder, RefersTo
from api.agent_views import Holders
from graph.gap import Finding
from graph.gap.names import NOTE_LIMIT
from graph.plan import ClaimRow

FindingStatusQuery = Literal["open", "resolved", "dismissed", "all"]


class FindingOut(BaseModel):
    finding_id: str
    kind: Literal["gap", "conflict", "question", "note"]
    text: str
    status: Literal["open", "resolved", "dismissed"]
    blocking: bool
    about: list[str]
    refers_to: list[RefersTo]
    story_id: str | None
    story_title: str | None = None
    author: Holder
    created_at: AwareDatetime
    resolved_by: Holder | None = None
    resolved_at: AwareDatetime | None = None
    resolution_note: str | None = None
    orphaned: bool = False


class FindingsOut(BaseModel):
    """One page, blocking first, then newest first; ``next`` is the opaque ``after`` of the next page."""

    findings: list[FindingOut]
    next: str | None = None


class CloseFindingBody(BaseModel):
    """Resolve or dismiss: an optional note of at most 500 characters."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=NOTE_LIMIT)


class ClosedFindingOut(BaseModel):
    """The closed finding and the number of tasks that went back to ``pending`` (none: nothing was blocked, or a plan
    run had already reset the tasks)."""

    finding: FindingOut
    unblocked: int


class ClaimOut(BaseModel):
    kind: Literal["story", "task"]
    story_id: str
    title: str
    task_id: str | None
    holder: Holder
    claimed_at: AwareDatetime
    branch: str | None
    tasks: int


class ClaimsOut(BaseModel):
    """The claims, oldest first."""

    claims: list[ClaimOut]


class ReleasedClaimOut(BaseModel):
    """The tasks a forced release returned to ``pending``; none when the claim was already gone."""

    released: int


def finding_out(finding: Finding, holders: Holders, titles: Mapping[str, str] | None = None) -> FindingOut:
    return FindingOut(
        finding_id=finding.finding_id,
        kind=finding.kind,
        text=finding.text,
        status=finding.status,
        blocking=finding.blocking,
        about=list(finding.about_spec_ids),
        refers_to=[RefersTo(type=ref.type, value=ref.value) for ref in finding.refers_to],
        story_id=finding.story_id,
        story_title=None if finding.story_id is None or titles is None else titles.get(finding.story_id),
        author=holders.of(finding.created_by) or Holder(name=None, you=False),
        created_at=finding.created_at,
        resolved_by=holders.of(finding.resolved_by),
        resolved_at=finding.resolved_at,
        resolution_note=finding.resolution_note,
        orphaned=finding.orphaned,
    )


def claim_out(row: ClaimRow, holders: Holders) -> ClaimOut:
    return ClaimOut(
        kind=row.kind,
        story_id=row.story_id,
        title=row.title,
        task_id=row.task_id,
        holder=holders.of(row.claimed_by, row.agent_label) or Holder(name=None, you=False),
        claimed_at=row.claimed_at,
        branch=row.branch,
        tasks=row.tasks,
    )


def user_ids(findings: Iterable[Finding]) -> list[str | None]:
    return [who for finding in findings for who in (finding.created_by, finding.resolved_by)]
