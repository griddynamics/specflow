"""The build chain of change reports the host reads from the build store (``docs/graph/plan/api.md`` "Change reports").

Every build stores its ``ChangeReport`` in its ``BuildResult`` (file store, under the build id). The chain between
two builds is the project's builds in build order (``BuildStore.list_builds``: start time, then id), after the
first build up to and including the second. A failed or unfinished build is skipped unless its ``persist`` step
started (persist writes in batches, so the graph may then hold changes no report names); every build of the chain
needs its stored report. When a link is missing, the chain says why (``reason``) and names the build, so a caller
can fall back (the plan runner runs a ``replan``).
Readers: the plan runner (``api.plan.runner``); the gap runner reads the same chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from graph.build import BuildStore
from graph.build.models import ChangeReport

PERSIST_STEP: Final = "persist"

MissingReason = Literal[
    "since_build_unknown",  # the first build is not a finished build of the store (deleted run artifacts)
    "until_build_unknown",  # the last build is not a finished build of the store
    "build_order",  # the last build is not after the first in build order
    "unfinished_build_in_chain",  # a failed or interrupted build of the chain started persist
    "report_missing",  # a build of the chain has no stored result or no change report
]


@dataclass(frozen=True)
class ChainLink:
    """One build of the chain, the build before it in the chain and its stored change report."""

    build_id: str
    previous_build_id: str
    report: ChangeReport


@dataclass(frozen=True)
class ReportChain:
    """The links in build order; ``reason`` and ``missing_build_id`` when the chain is incomplete (no links)."""

    links: tuple[ChainLink, ...] = ()
    reason: MissingReason | None = None
    missing_build_id: str | None = None

    @property
    def complete(self) -> bool:
        return self.reason is None


async def reports_since(builds: BuildStore, project_id: str, since_build_id: str, until_build_id: str) -> ReportChain:
    """The change reports of every build after ``since_build_id`` up to ``until_build_id``, in build order.

    Empty and complete when both are the same build.
    """

    records = await builds.list_builds(project_id)
    order = {record.build_id: index for index, record in enumerate(records) if record.finished}
    bounds = _bounds(order, since_build_id, until_build_id)
    if isinstance(bounds, ReportChain):
        return bounds
    start, end = bounds
    links: list[ChainLink] = []
    previous = since_build_id
    for record in records[start + 1 : end + 1]:
        if not record.finished:
            if record.progress.get(PERSIST_STEP) is not None:
                return ReportChain(reason="unfinished_build_in_chain", missing_build_id=record.build_id)
            continue
        result = await builds.get_result(project_id, record.build_id)
        if result is None or result.change_report is None:
            return ReportChain(reason="report_missing", missing_build_id=record.build_id)
        links.append(ChainLink(build_id=record.build_id, previous_build_id=previous, report=result.change_report))
        previous = record.build_id
    return ReportChain(links=tuple(links))


def _bounds(order: dict[str, int], since_build_id: str, until_build_id: str) -> tuple[int, int] | ReportChain:
    """The positions of both builds among the finished builds, or the incomplete (or empty) chain."""

    if since_build_id not in order:
        return ReportChain(reason="since_build_unknown", missing_build_id=since_build_id)
    if until_build_id not in order:
        return ReportChain(reason="until_build_unknown", missing_build_id=until_build_id)
    if since_build_id == until_build_id:
        return ReportChain()
    if order[until_build_id] < order[since_build_id]:
        return ReportChain(reason="build_order", missing_build_id=until_build_id)
    return order[since_build_id], order[until_build_id]
