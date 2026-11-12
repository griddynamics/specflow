"""Models of the findings library: what it returns ([gap.md](gap.md) "Library: Findings")."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict

from graph.gap.names import FindingKind, FindingStatus, RefType


class GapModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FindingRef(GapModel):
    """A ``refers_to`` entry: where a finding comes from. Stored, never fetched."""

    type: RefType
    value: str


class Finding(GapModel):
    """One finding with its references; ``uuid`` and ``dedupe_key`` stay inside the store."""

    finding_id: str
    kind: FindingKind
    text: str
    origin: str
    status: FindingStatus
    blocking: bool
    about_spec_ids: tuple[str, ...]
    story_id: str | None = None
    created_by: str
    created_at: AwareDatetime
    resolved_by: str | None = None
    resolved_at: AwareDatetime | None = None
    resolution_note: str | None = None
    orphaned: bool = False
    updated_at: AwareDatetime | None = None
    refers_to: tuple[FindingRef, ...] = ()


class AddedFinding(GapModel):
    """The answer to ``add``: the finding and whether this call created it (false: an open one already matched)."""

    finding: Finding
    created: bool


class FindingPage(GapModel):
    """One page of findings; ``next`` is the opaque ``after`` of the following page, or none."""

    findings: tuple[Finding, ...]
    next: str | None = None


class FindingRefresh(GapModel):
    """What ``refresh`` did: open findings whose ids or edges it updated, and those it left orphaned."""

    refreshed: int
    orphaned: int
