"""A small in-memory findings port for plan tests (test support, not shipped code).

``graph.plan`` reads open findings through its structural ``FindingsReads`` port (``graph.plan.findings_port``) and
never imports ``graph.gap``; the host fills the port with ``GapService``. ``MemoryFindings`` answers the port's two
reads over a list: ``findings`` (the open findings about any asked id, blocking first, then newest first) and
``open_counts`` (open findings per asked id). Every read is recorded in ``calls``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, Final

START: Final = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


@dataclass(frozen=True)
class FakeFinding:
    """The attributes of a finding the plan shows (``FindingLike``) and its status."""

    finding_id: str
    kind: str
    text: str
    blocking: bool
    about_spec_ids: tuple[str, ...]
    created_by: str
    created_at: datetime
    story_id: str | None = None
    orphaned: bool = False
    status: str = "open"


class MemoryFindings:
    """``FindingsReads`` over a list of findings; ``add`` stamps each one a second after the previous one."""

    def __init__(self) -> None:
        self.items: list[FakeFinding] = []
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def add(
        self,
        kind: str,
        text: str,
        about: Sequence[str],
        *,
        created_by: str = "u1",
        story_id: str | None = None,
        blocking: bool = False,
    ) -> FakeFinding:
        item = FakeFinding(
            finding_id=f"GAP::{len(self.items) + 1:012X}",
            kind=kind,
            text=text,
            blocking=blocking,
            about_spec_ids=tuple(sorted(about)),
            created_by=created_by,
            created_at=START + timedelta(seconds=len(self.items) + 1),
            story_id=story_id,
        )
        self.items.append(item)
        return item

    def close(self, finding_id: str) -> None:
        self.items = [
            replace(item, status="resolved") if item.finding_id == finding_id else item for item in self.items
        ]

    def _open_about(self, spec_ids: Sequence[str]) -> list[FakeFinding]:
        wanted = set(spec_ids)
        return [item for item in self.items if item.status == "open" and wanted & set(item.about_spec_ids)]

    async def findings(self, _project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50) -> Any:
        self.calls.append(("findings", tuple(spec_ids or ())))
        found = sorted(self._open_about(spec_ids or ()), key=lambda item: item.created_at, reverse=True)
        found.sort(key=lambda item: not item.blocking)
        return SimpleNamespace(findings=found[:limit], next=None)

    async def open_counts(self, _project_id: str, spec_ids: Sequence[str]) -> Mapping[str, int]:
        self.calls.append(("open_counts", tuple(spec_ids)))
        return {spec_id: len(self._open_about([spec_id])) for spec_id in dict.fromkeys(spec_ids)}
