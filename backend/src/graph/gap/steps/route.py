"""Step ``route``: severity and who answers (docs/graph/gap/answers.md "Severity", "Routing").

SLICE-1 BEHAVIOUR. Every recorded gap is routed ``ask`` and has no options until session 06 adds the gates, the valve
and the AI route. The severity rules that need no model are applied now, in this order, to the severity the report
proposes (``severity_of``):

1. a report of severity ``info`` stays ``info``;
2. a gap ``ABOUT`` only out-of-scope nodes (the ontology's spine ``exclude``) becomes ``info``;
3. a ``contradiction`` is at least ``major``;
4. a ``blocker`` becomes ``major`` when the run has a plan and no task implements an ``ABOUT`` or ``IMPACTS`` node;
5. no proposal reads as ``major`` (a person sees it).

An ``info`` gap is written as a ``gap_note`` row only: no node, no route.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from graph.gap.models import GapReport
from graph.gap.names import GapRoute, ReportSeverity
from graph.gap.steps.snapshot import GraphSnapshot

INFO: Final = "info"
ASK: Final[GapRoute] = "ask"
_RANK: Final[dict[str, int]] = {"info": 0, "minor": 1, "major": 2, "blocker": 3}


@dataclass(frozen=True)
class Routed:
    """A checked gap's severity and route; ``route`` is None for an ``info`` note."""

    severity: ReportSeverity
    route: GapRoute | None


def severity_of(report: GapReport, snapshot: GraphSnapshot, *, planned: Collection[str] | None) -> ReportSeverity:
    """The severity after the code rules of answers.md "Severity" (module docstring); ``planned`` holds the spec ids
    a plan task implements, None when the run has no plan."""

    proposed: ReportSeverity = report.severity or "major"
    if proposed == INFO:
        return "info"
    nodes = [snapshot.nodes[spec_id] for spec_id in report.about if spec_id in snapshot.nodes]
    if nodes and all(snapshot.out_of_spine(node) for node in nodes):
        return "info"
    if report.kind == "contradiction" and _RANK[proposed] < _RANK["major"]:
        return "major"
    if proposed == "blocker" and planned is not None and not set(planned) & {*report.about, *report.impacts}:
        return "major"
    return proposed


def route_report(report: GapReport, snapshot: GraphSnapshot, *, planned: Collection[str] | None = None) -> Routed:
    """The slice-1 route of a checked gap report: ``ask`` for every severity but ``info``."""

    severity = severity_of(report, snapshot, planned=planned)
    return Routed(severity, None if severity == INFO else ASK)
