"""Step ``route``: severity and who answers (docs/graph/gap/answers.md "Severity", "Routing", "The question card";
FR-GRTE-01, FR-GRTE-02, FR-GRTE-04, FR-GRTE-06).

Routing is code: the model only proposes a severity and the evidence (FR-GRTE-01). The severity rules, in order, on the
severity the report proposes (``severity_of``):

1. a report of severity ``info`` stays ``info``;
2. a gap ``ABOUT`` only out-of-scope nodes (the ontology's spine ``exclude``) becomes ``info``;
3. a ``contradiction`` is at least ``major``;
4. a ``blocker`` becomes ``major`` when the run has a plan and no task implements an ``ABOUT`` or ``IMPACTS`` node;
5. no proposal reads as ``major`` (a person sees it).

The route (``route_report``): ``info`` is a ``gap_note`` (no route, no node, FR-GRTE-04); a confirmed pointer (the
second check found that a stated line answers the gap) is ``ai`` with mode ``pointer`` for every kind but
``contradiction`` when AI answers are on; ``blocker`` and ``major`` are ``ask`` (FR-GRTE-02); so is every other gap
until the AI branch (gates, valve) is built. ``ai_answerable`` from the report is advisory and never decides. The
reasons of each route are kept for the gap's ``routed`` event.

The new ``ask`` gaps of a run beyond ``limits.new_questions_per_run`` are held back from the inbox's default view
until the next run (``hold_back``): ranked by severity, then reach (plan tasks behind the site), then the site's rank
score (FR-GRTE-06).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from graph.gap.models import GapReport
from graph.gap.names import GapRoute, ReportSeverity
from graph.gap.steps.snapshot import GraphSnapshot

INFO: Final = "info"
ASK: Final[GapRoute] = "ask"
AI: Final[GapRoute] = "ai"
POINTER: Final = "pointer"
_RANK: Final[dict[str, int]] = {"info": 0, "minor": 1, "major": 2, "blocker": 3}


@dataclass(frozen=True)
class Routed:
    """A checked gap's severity, route and why; ``route`` is None for an ``info`` note, ``mode`` ``pointer`` for a
    pointer answer."""

    severity: ReportSeverity
    route: GapRoute | None
    mode: Literal["pointer"] | None = None
    reasons: tuple[str, ...] = ()


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


def route_report(
    report: GapReport,
    snapshot: GraphSnapshot,
    *,
    planned: Collection[str] | None = None,
    pointer: bool = False,
    ai_answers: bool = True,
    ai_kinds: Collection[str] = (),
) -> Routed:
    """The severity and route of a checked gap report (module docstring)."""

    severity = severity_of(report, snapshot, planned=planned)
    if severity == INFO:
        return Routed(severity, None, reasons=("info",))
    if pointer and report.kind != "contradiction" and ai_answers:
        return Routed(severity, AI, POINTER, ("pointer_confirmed",))
    reasons: list[str] = []
    if severity in ("blocker", "major"):
        reasons.append("severity")
    if report.kind not in ai_kinds:
        reasons.append("kind")
    if not ai_answers:
        reasons.append("ai_answers_off")
    reasons.append("ai_branch_off")
    return Routed(severity, ASK, reasons=tuple(reasons))


@dataclass(frozen=True)
class Candidate:
    """A new ``ask`` gap of the run and what ranks it."""

    site_id: str
    severity: ReportSeverity
    reach: int
    rank_score: float


def hold_back(candidates: Collection[Candidate], limit: int) -> frozenset[str]:
    """The site ids of the new ``ask`` gaps beyond ``limit``, by severity, reach, rank score, then site id."""

    ordered = sorted(candidates, key=lambda item: (-_RANK[item.severity], -item.reach, -item.rank_score, item.site_id))
    return frozenset(item.site_id for item in ordered[limit:])


def routed_payload(routed: Routed, *, held_back: bool) -> Mapping[str, object]:
    return {
        "severity": routed.severity,
        "route": routed.route,
        "mode": routed.mode,
        "reasons": list(routed.reasons),
        "held_back": held_back,
    }
