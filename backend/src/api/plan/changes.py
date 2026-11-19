"""The host's conversion of build change reports to the plan-side ``BuildChangeReport`` (``PlanContext.changes``).

``graph.plan`` never imports ``graph.build``, so the plan runner reads the build chain (``api.graph_build.reports``)
and converts each link here: nodes by ``spec_id`` with their classes and previous ids, edges split from their
``edge_ref`` (``<source>|<RELATION>|<target>``). An edge ref that does not split in three makes the report unusable
(``None``); the runner then re-plans.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from api.graph_build.reports import ChainLink
from graph.build.models import ChangedEdge
from graph.plan.models import BuildChangeReport, ChangedSpecEdge, ChangedSpecNode

REF_PARTS: Final = 3


def plan_change_reports(links: Iterable[ChainLink]) -> tuple[BuildChangeReport, ...] | None:
    """The links as plan-side reports, in the same order; None when an edge ref is malformed."""

    reports: list[BuildChangeReport] = []
    for link in links:
        edges = [_edge(edge) for edge in link.report.edges]
        if any(edge is None for edge in edges):
            return None
        reports.append(
            BuildChangeReport(
                build_id=link.build_id,
                previous_build_id=link.previous_build_id,
                nodes=tuple(
                    ChangedSpecNode(
                        spec_id=node.spec_id, classes=node.classes, previous_spec_ids=node.previous_spec_ids
                    )
                    for node in link.report.nodes
                ),
                edges=tuple(edge for edge in edges if edge is not None),
            )
        )
    return tuple(reports)


def _edge(edge: ChangedEdge) -> ChangedSpecEdge | None:
    parts = edge.ref.split("|")
    if len(parts) != REF_PARTS or not all(parts):
        return None
    source, relation, target = parts
    return ChangedSpecEdge(source=source, relation=relation, target=target, change=edge.change, basis=edge.basis)
