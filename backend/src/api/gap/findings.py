"""The plan's findings port in the host: open gaps as the plan's open findings (FR-GPLN-02).

``graph.plan`` reads open findings through its structural ``FindingsReads`` (``open_counts``, ``findings``) and never
imports ``graph.gap`` (docs/graph/gap/lifecycle.md "Plan link"). ``OpenGaps`` fills that port for the agent reads with
``GapService.open_counts`` and ``GapService.findings``: every gap with status ``open``, ``answered`` or ``applying``,
mapped to the plan's ``FindingLike`` by ``graph.gap`` (development gaps reported over ``add_finding`` included: the
first-slice findings store is gone).

The gap service comes from the host's gap runner (``Runtime.gaps``) on each call; without a runner (gap runs not
configured) no finding is read, and a gap service that cannot be built (bad rules or ontology: ``ConfigurationError``)
is logged once per call and left out, so a brief never fails because the gap configuration is broken. Store outages
pass through as for every other read.

``severities`` gives the severity of the gaps among shown findings, so briefs, previews and ``get_node`` show it
(FR-GWEB-03). ``finding`` reads one gap by id or alias (a task's ``blocked_by``) for a claim refusal. Logs carry codes
only, never a question (NFR-GSEC-02).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from graph.core import ConfigurationError
from graph.gap import GapNotFoundError, GapService, GapView
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("api.gap.findings")

PAGE: Final = 200  # the most items one findings read asks either source for (the port's own maximum)


class GapRunnerService(Protocol):
    """The call of ``api.gap.runner.GapRunner`` the port uses."""

    async def service(self) -> GapService: ...


@dataclass(frozen=True)
class OpenFindingsPage:
    """The merged page: items satisfy the plan's ``FindingLike``."""

    findings: tuple[Any, ...]
    next: str | None = None


class OpenGaps:
    """The plan's ``FindingsReads`` over open gaps (see the module docstring)."""

    def __init__(self, gaps: Callable[[], GapRunnerService | None]) -> None:
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
        service = await self.service() if ids else None
        if service is None:
            return counts
        for spec_id, count in (await service.open_counts(project_id, ids)).items():
            if spec_id in counts:
                counts[spec_id] += count
        return counts

    async def findings(
        self, project_id: str, *, spec_ids: Sequence[str] | None = None, limit: int = 50
    ) -> OpenFindingsPage:
        service = await self.service()
        if service is None:
            return OpenFindingsPage(findings=())
        page = await service.findings(project_id, spec_ids=spec_ids, limit=PAGE)
        return OpenFindingsPage(findings=tuple(ordered(page.findings)[:limit]))

    async def severities(self, project_id: str, findings: Iterable[Any]) -> dict[str, str]:
        """The severity of each open gap among ``findings`` (by finding id)."""

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

    async def finding(self, project_id: str, finding_id: str) -> GapView | None:
        """The gap of ``finding_id`` (an id or an alias such as a migrated ``FND::`` id), None when it is gone."""

        service = await self.service()
        if service is None:
            return None
        try:
            return await service.get_gap(project_id, finding_id)
        except GapNotFoundError:
            return None

    async def hold_blockers(self, project_id: str, spec_ids: Sequence[str]) -> None:
        """Hold the tasks of the open blocker gaps about ``spec_ids`` before a claim (decision GD2); nothing without
        gaps."""

        service = await self.service()
        if service is not None and spec_ids:
            await service.hold_blockers(project_id, spec_ids)

    async def blocking_ids(self, project_id: str, created_by: str) -> tuple[str, ...]:
        """The ids (and aliases) of the open development gaps ``created_by`` reported that block a story."""

        service = await self.service()
        if service is None:
            return ()
        found: list[str] = []
        cursor: str | None = None
        while True:  # every page: the caller's gaps may sit anywhere in the project's order
            page = await service.list_gaps(project_id, origin="development", limit=PAGE, cursor=cursor)
            found += [
                ident
                for gap in page.gaps
                if gap.blocking and gap.created_by == created_by
                for ident in (gap.gap_id, *gap.aliases)
            ]
            if page.next is None:
                return tuple(found)
            cursor = page.next


def ordered(items: Iterable[Any]) -> list[Any]:
    """Blocking first, then newest first, then by id: the port's order."""

    return sorted(items, key=lambda item: (not item.blocking, -item.created_at.timestamp(), item.finding_id))
