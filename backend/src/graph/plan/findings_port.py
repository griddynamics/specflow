"""The plan's view of findings: a port the host fills with ``graph.gap.Findings`` ([gap.md](gap.md)).

``graph.plan`` never imports ``graph.gap`` (the architecture's import contract). It asks for open findings and open
counts through the structural ``FindingsReads`` below, which ``Findings`` satisfies as it is, and shows them as
``OpenFinding``. A service built without a port shows no findings: lists stay empty and counts are 0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final, Protocol

from pydantic import AwareDatetime

from graph.plan.models import PlanModel

FINDINGS_PAGE: Final = 200
"""The most open findings one brief or preview shows."""


class OpenFinding(PlanModel):
    """An open finding about nodes of a plan answer. ``text`` is the agent's own words, never logged."""

    finding_id: str
    kind: str
    text: str
    blocking: bool
    about_spec_ids: tuple[str, ...]
    story_id: str | None = None
    created_by: str
    created_at: AwareDatetime
    orphaned: bool = False


class FindingLike(Protocol):
    """The read-only attributes of a finding the plan shows."""

    @property
    def finding_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def blocking(self) -> bool: ...

    @property
    def about_spec_ids(self) -> Sequence[str]: ...

    @property
    def story_id(self) -> str | None: ...

    @property
    def created_by(self) -> str: ...

    @property
    def created_at(self) -> AwareDatetime: ...

    @property
    def orphaned(self) -> bool: ...


class FindingPageLike(Protocol):
    @property
    def findings(self) -> Sequence[FindingLike]: ...


class FindingsReads(Protocol):
    """What the plan reads of findings: open counts per id and the open findings about ids (aliases resolved)."""

    async def open_counts(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, int]: ...

    async def findings(
        self, project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50
    ) -> FindingPageLike: ...


def open_finding(finding: FindingLike) -> OpenFinding:
    return OpenFinding(
        finding_id=finding.finding_id,
        kind=finding.kind,
        text=finding.text,
        blocking=finding.blocking,
        about_spec_ids=tuple(finding.about_spec_ids),
        story_id=finding.story_id,
        created_by=finding.created_by,
        created_at=finding.created_at,
        orphaned=finding.orphaned,
    )


async def open_findings_about(
    port: FindingsReads | None, project_id: str, spec_ids: Sequence[str]
) -> tuple[OpenFinding, ...]:
    """The open findings about any of ``spec_ids`` (blocking first, newest first), at most 200; none without a port."""

    ids = tuple(dict.fromkeys(spec_ids))
    if port is None or not ids:
        return ()
    found: dict[str, FindingLike] = {}
    for start in range(0, len(ids), FINDINGS_PAGE):  # a filter takes at most 200 ids
        page = await port.findings(project_id, spec_ids=ids[start : start + FINDINGS_PAGE], limit=FINDINGS_PAGE)
        found.update((item.finding_id, item) for item in page.findings)
    newest = sorted(found.values(), key=lambda item: item.created_at, reverse=True)
    newest.sort(key=lambda item: not item.blocking)
    return tuple(open_finding(item) for item in newest[:FINDINGS_PAGE])


async def open_counts_of(port: FindingsReads | None, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, int]:
    """Open findings per id; 0 for each without a port."""

    ids = tuple(dict.fromkeys(spec_ids))
    if port is None or not ids:
        return dict.fromkeys(ids, 0)
    return await port.open_counts(project_id, ids)
