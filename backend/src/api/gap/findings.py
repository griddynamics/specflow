"""The plan's findings port in the host: open gaps and first-slice findings as one source (FR-GPLN-02).

``graph.plan`` reads open findings through its structural ``FindingsReads`` (``open_counts``, ``findings``) and never
imports ``graph.gap`` (docs/graph/gap/lifecycle.md "Plan link"). ``OpenGaps`` fills that port for the agent reads:

- ``GapService.open_counts`` and ``GapService.findings`` (every gap with status ``open``, ``answered`` or ``applying``,
  mapped to the plan's ``FindingLike`` by ``graph.gap``);
- the first-slice ``Findings`` store, which keeps serving development findings until the first-slice migration
  (session 13 of the gap plan), when ``add_finding`` becomes ``raise_gap``.

Both sources are merged: counts add up, findings are united by id and ordered as the port promises (blocking first,
then newest first). The gap service comes from the host's gap runner (``Runtime.gaps``) on each call; without a runner
(gap runs not configured) only the first-slice findings are read, and a gap service that cannot be built (bad rules or
ontology: ``ConfigurationError``) is logged once per call and left out, so a brief never fails because the gap
configuration is broken. Store outages pass through as for every other read.

``severities`` gives the severity of the gaps among shown findings, so briefs, previews and ``get_node`` show it
(FR-GWEB-03); a first-slice finding has none. Logs carry codes only, never a question (NFR-GSEC-02).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from graph.core import ConfigurationError
from graph.gap import GapService
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.gap.findings")

PAGE: Final = 200  # the most items one findings read asks either source for (the port's own maximum)


class FirstSliceFindings(Protocol):
    """The reads of the first-slice ``graph.gap.Findings`` the port uses."""

    async def open_counts(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, int]: ...

    async def findings(self, project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50) -> Any: ...


class GapRunnerService(Protocol):
    """The call of ``api.gap.runner.GapRunner`` the port uses."""

    async def service(self) -> GapService: ...


@dataclass(frozen=True)
class OpenFindingsPage:
    """The merged page: items satisfy the plan's ``FindingLike``."""

    findings: tuple[Any, ...]
    next: str | None = None


class OpenGaps:
    """The plan's ``FindingsReads`` over open gaps and first-slice findings (see the module docstring)."""

    def __init__(self, findings: FirstSliceFindings | None, gaps: Callable[[], GapRunnerService | None]) -> None:
        self._first = findings
        self._gaps = gaps

    async def service(self) -> GapService | None:
        """The gap service of the host's gap runner, or None when gaps are not configured or cannot be read."""

        runner = self._gaps()
        if runner is None:
            return None
        try:
            return await runner.service()
        except ConfigurationError as error:
            log_event(logger, WARNING, "api.gap.findings_unavailable", code=error.code)
            return None

    async def open_counts(self, project_id: str, spec_ids: Sequence[str]) -> dict[str, int]:
        ids = tuple(dict.fromkeys(spec_ids))
        counts = dict.fromkeys(ids, 0)
        if not ids:
            return counts
        sources: list[Callable[[], Awaitable[Mapping[str, int]]]] = []
        if self._first is not None:
            first = self._first
            sources.append(lambda: first.open_counts(project_id, ids))
        service = await self.service()
        if service is not None:
            sources.append(lambda: service.open_counts(project_id, ids))
        for read in sources:
            for spec_id, count in (await read()).items():
                if spec_id in counts:
                    counts[spec_id] += count
        return counts

    async def findings(
        self, project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50
    ) -> OpenFindingsPage:
        found: dict[str, Any] = {}
        if self._first is not None:
            page = await self._first.findings(project_id, spec_ids=spec_ids, limit=PAGE)
            found.update((item.finding_id, item) for item in page.findings)
        service = await self.service()
        if service is not None:
            page = await service.findings(project_id, spec_ids=spec_ids, limit=PAGE)
            found.update((item.finding_id, item) for item in page.findings)
        return OpenFindingsPage(findings=tuple(ordered(found.values())[:limit]))

    async def severities(self, project_id: str, findings: Iterable[Any]) -> dict[str, str]:
        """The severity of each open gap among ``findings`` (by finding id); first-slice findings have none."""

        shown = list(findings)
        about = sorted({spec_id for item in shown for spec_id in item.about_spec_ids})
        if not about:
            return {}
        service = await self.service()
        if service is None:
            return {}
        wanted = {item.finding_id for item in shown}
        page = await service.list_gaps(project_id, about=about, limit=PAGE)
        return {gap.gap_id: gap.severity for gap in page.gaps if gap.gap_id in wanted}


def ordered(items: Iterable[Any]) -> list[Any]:
    """Blocking first, then newest first, then by id: the port's order."""

    return sorted(items, key=lambda item: (not item.blocking, -item.created_at.timestamp(), item.finding_id))
