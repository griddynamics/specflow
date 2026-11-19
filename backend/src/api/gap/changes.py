"""The host's conversion of build results to the gap-side models (``GapContext.changes`` and ``GapContext.build``).

``graph.gap`` never imports ``graph.build``, so the gap runner reads the build chain (``api.graph_build.reports``) and
converts each link here (docs/graph/gap/api.md "Models"):

- ``gap_change_reports``: one ``GapChangeReport`` per link, from the classes of its changed nodes. ``added``,
  ``removed``, ``changed_meaning``, ``changed_links`` and ``changed_evidence`` list the node's ``spec_id``; ``merged``
  and ``rekeyed`` map each of the node's ``previous_spec_ids`` to its ``spec_id``. ``carried_over`` nodes and the
  edge list are not copied (an edge change already marks its ends ``changed_links``). The build's change report
  names no fragments or document revisions, so ``fragments_changed`` and ``documents`` stay empty.
- ``build_facts``: the dedup refusals (``RefusedPair``) and the ``cycle_broken`` quarantine (``CutEdge``) of a build
  result. A refusal or cut edge with an end that was never persisted (``None``), and a refusal whose ends collapsed
  into one node, are left out: the detectors read pairs of two distinct spec nodes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from api.graph_build.reports import ChainLink
from graph.build import BuildResult
from graph.build.models import ChangedNode
from graph.gap import BuildFacts, CutEdge, GapChangeReport, RefusedPair

CYCLE_BROKEN: Final = "cycle_broken"
LISTED_CLASSES: Final = ("added", "removed", "changed_meaning", "changed_links", "changed_evidence")


def gap_change_reports(links: Iterable[ChainLink]) -> tuple[GapChangeReport, ...]:
    """The links as gap-side reports, in the same (build) order."""

    return tuple(_report(link) for link in links)


def _report(link: ChainLink) -> GapChangeReport:
    listed: dict[str, list[str]] = {name: [] for name in LISTED_CLASSES}
    merged: dict[str, str] = {}
    rekeyed: dict[str, str] = {}
    for node in link.report.nodes:
        for name in LISTED_CLASSES:
            if name in node.classes and node.spec_id not in listed[name]:
                listed[name].append(node.spec_id)
        if "merged" in node.classes:
            merged.update(_replaced(node))
        if "rekeyed" in node.classes:
            rekeyed.update(_replaced(node))
    return GapChangeReport(
        build_id=link.build_id,
        added=tuple(listed["added"]),
        removed=tuple(listed["removed"]),
        merged=merged,
        rekeyed=rekeyed,
        changed_meaning=tuple(listed["changed_meaning"]),
        changed_links=tuple(listed["changed_links"]),
        changed_evidence=tuple(listed["changed_evidence"]),
    )


def _replaced(node: ChangedNode) -> dict[str, str]:
    return {old: node.spec_id for old in node.previous_spec_ids if old != node.spec_id}


def build_facts(result: BuildResult) -> BuildFacts:
    """The dedup refusals and cycle cuts of ``result``, ends by ``spec_id``."""

    refused = tuple(
        RefusedPair(
            first_spec_id=pair.first_spec_id,
            second_spec_id=pair.second_spec_id,
            label=pair.label,
            guard=pair.guard,
        )
        for pair in result.dedup_refused
        if pair.first_spec_id is not None
        and pair.second_spec_id is not None
        and pair.first_spec_id != pair.second_spec_id
    )
    cut = tuple(
        CutEdge(
            relation=edge.relation,
            source_spec_id=edge.source_spec_id,
            target_spec_id=edge.target_spec_id,
            fragment_ids=edge.fragment_ids,
        )
        for edge in result.quarantine
        if edge.reason == CYCLE_BROKEN and edge.source_spec_id is not None and edge.target_spec_id is not None
    )
    return BuildFacts(build_id=result.build_id, refused_pairs=refused, cut_edges=cut)
