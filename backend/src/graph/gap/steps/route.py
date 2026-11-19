"""The route of a site, a phase of the step ``investigate``: severity and who answers (docs/graph/gap/answers.md
"Severity", "Routing", "Gates", "The valve"; FR-GRTE-01 to FR-GRTE-05).

Routing is code: the model only proposes a severity and the evidence (FR-GRTE-01). The severity rules, in order, on the
severity the report proposes (``severity_of``):

1. a report of severity ``info`` stays ``info``;
2. a gap ``ABOUT`` only out-of-scope nodes (the ontology's spine ``exclude``) becomes ``info``;
3. a ``contradiction`` is at least ``major``;
4. a ``blocker`` becomes ``major`` when the run has a plan and no task implements an ``ABOUT`` or ``IMPACTS`` node;
5. no proposal reads as ``major`` (a person sees it).

The route (``route_report``): ``info`` is a ``gap_note`` (no route, no node, FR-GRTE-04); a confirmed pointer (the
second check found that a stated line answers the gap) is ``ai`` with mode ``pointer`` for every kind but
``contradiction`` when AI answers are on; ``blocker`` and ``major`` are ``ask`` (FR-GRTE-02). A ``minor`` gap is
``ai`` with mode ``ai`` only when it is a new gap of origin ``analysis``, its kind is in ``routing.ai_kinds``
(never ``contradiction``, ``open_question`` or ``assumption``), AI answers are on and every gate holds (FR-GRTE-03):

- entailed: the recommended option has a basis quote (the site's check kept only quotes found in their fragment) and the
  second check (``gap_judge``, prompt ``AI_ANSWER_PROMPT``) found its edits entailed;
- local: the ``ABOUT`` and ``IMPACTS`` nodes number at most ``gates.local_max_nodes`` and reach at most
  ``gates.local_max_areas`` spine owners (work areas or owning modules);
- consistent: the second check found no line that says otherwise;
- confident: the second check's confidence is at least ``gates.min_confidence``.

``ai_answerable`` from the report is advisory and never decides. The reasons of each route and every gate result are
kept for the gap's ``routed`` event.

The valve (``Valve``, FR-GRTE-05) counts over the run as its sites are routed one by one, in the order they are
investigated: before each ``ai`` gap, when the run already has ``limits.ai_answers_per_run`` AI answers or the nodes
its AI answers touch (``ABOUT`` and ``IMPACTS``) are already ``limits.ai_answers_max_node_share`` of the project's spec
nodes, the gap is routed ``ask`` with the reason ``ai_limit``. Pointers decide nothing new and are not counted.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal

from graph.gap.models import GapReport, ReportOption
from graph.gap.names import GapRoute, ReportSeverity
from graph.gap.rules import Gates
from graph.gap.steps.snapshot import GraphSnapshot

INFO: Final = "info"
ASK: Final[GapRoute] = "ask"
AI: Final[GapRoute] = "ai"
POINTER: Final = "pointer"
AI_MODE: Final = "ai"
ANALYSIS: Final = "analysis"
AI_LIMIT: Final = "ai_limit"
"""The reason of a gap the valve routed ``ask`` ("AI answer limit reached")."""
GATES: Final = ("entailed", "local", "consistent", "confident")
_RANK: Final[dict[str, int]] = {"info": 0, "minor": 1, "major": 2, "blocker": 3}


@dataclass(frozen=True)
class Routed:
    """A checked gap's severity, route and why; ``route`` is None for an ``info`` note, ``mode`` ``pointer`` for a
    pointer answer and ``ai`` for an AI answer; ``gates`` holds each gate's result when the gates were looked at."""

    severity: ReportSeverity
    route: GapRoute | None
    mode: Literal["pointer", "ai"] | None = None
    reasons: tuple[str, ...] = ()
    gates: Mapping[str, bool] = field(default_factory=dict)
    confidence: float | None = None


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


def recommended(report: GapReport) -> ReportOption | None:
    """The report's recommended option, or None."""

    return next((option for option in report.options if option.recommended), None)


def touched(report: GapReport) -> frozenset[str]:
    """The spec ids an answer to the gap touches: its ``ABOUT`` and ``IMPACTS`` nodes."""

    return frozenset({*report.about, *report.impacts})


def local(report: GapReport, snapshot: GraphSnapshot, gates: Gates) -> bool:
    """The local gate: at most ``local_max_nodes`` nodes and at most ``local_max_areas`` spine owners among them."""

    nodes = touched(report)
    if len(nodes) > gates.local_max_nodes:
        return False
    areas = {owner for spec_id in nodes for owner in snapshot.spine_owners(spec_id)}
    return len(areas) <= gates.local_max_areas


def ai_candidate(
    report: GapReport,
    snapshot: GraphSnapshot,
    *,
    gates: Gates,
    ai_kinds: Collection[str],
    ai_answers: bool,
    new: bool,
    seed_kinds: Collection[str] = (),
) -> bool:
    """Whether the second check of an AI answer is worth a call: a new ``minor`` gap whose kind and whose seeds' kinds
    are all AI kinds (a report cannot relabel a stated question, an assumption or a contradiction), AI answers on, a
    recommended option with edits and a basis quote, and the local gate holding. Code routes it afterwards
    (``route_report``)."""

    option = recommended(report)
    return (
        ai_answers
        and new
        and report.severity == "minor"
        and ai_kind(report, ai_kinds, seed_kinds)
        and option is not None
        and bool(option.basis)
        and bool(option.edits)
        and local(report, snapshot, gates)
    )


def ai_kind(report: GapReport, ai_kinds: Collection[str], seed_kinds: Collection[str] = ()) -> bool:
    """The report's kind and every seed kind of its site are AI kinds."""

    return report.kind in ai_kinds and all(kind in ai_kinds for kind in seed_kinds)


def gate_results(
    report: GapReport, snapshot: GraphSnapshot, gates: Gates, judge: Mapping[str, Any] | None
) -> dict[str, bool]:
    """Each gate of answers.md "Gates" for the report and the second check's answer (all false without one)."""

    option = recommended(report)
    found = judge or {}
    confidence = found.get("confidence")
    return {
        "entailed": option is not None and bool(option.basis) and bool(option.edits) and found.get("entailed") is True,
        "local": local(report, snapshot, gates),
        "consistent": found.get("consistent") is True,
        "confident": isinstance(confidence, int | float) and float(confidence) >= gates.min_confidence,
    }


@dataclass(frozen=True)
class RouteInputs:
    """What decides the route besides the report: the plan, the pointer check, the switches and the AI second check."""

    planned: Collection[str] | None = None
    pointer: bool = False
    ai_answers: bool = True
    ai_kinds: Collection[str] = ()
    gates: Gates | None = None
    judge: Mapping[str, Any] | None = None
    """The second check of the AI answer (``JudgeAnswer`` as JSON), None when it was not asked."""
    judge_failed: bool = False
    origin: str = ANALYSIS
    new: bool = True
    seed_kinds: Collection[str] = ()
    """The kinds of the site's seeds: a report that relabels a seed of another kind is not answered by AI."""


def route_report(
    report: GapReport,
    snapshot: GraphSnapshot,
    inputs: RouteInputs | None = None,
    **fields: Any,
) -> Routed:
    """The severity and route of a checked gap report (module docstring); ``fields`` set ``RouteInputs`` fields."""

    given = replace(inputs or RouteInputs(), **fields)
    severity = severity_of(report, snapshot, planned=given.planned)
    if severity == INFO:
        return Routed(severity, None, reasons=("info",))
    contradiction = report.kind == "contradiction" or "contradiction" in given.seed_kinds
    if given.pointer and not contradiction and given.ai_answers:
        return Routed(severity, AI, POINTER, ("pointer_confirmed",))
    reasons = _ask_reasons(report, severity, given)
    if given.gates is None:
        return Routed(severity, ASK, reasons=(*reasons, "gates_off"))
    gates = gate_results(report, snapshot, given.gates, given.judge)
    confidence = (given.judge or {}).get("confidence")
    score = float(confidence) if isinstance(confidence, int | float) else None
    if given.judge_failed:
        reasons.append("ai_judge_failed")
    reasons += [f"gate_{name}" for name in GATES if not gates[name]]
    if reasons:
        return Routed(severity, ASK, reasons=tuple(reasons), gates=gates, confidence=score)
    return Routed(severity, AI, AI_MODE, ("gates_hold",), gates=gates, confidence=score)


def _ask_reasons(report: GapReport, severity: ReportSeverity, given: RouteInputs) -> list[str]:
    """The rules of answers.md "Routing" before the gates that send a gap to a person."""

    checks = (
        (severity in ("blocker", "major"), "severity"),
        (not ai_kind(report, given.ai_kinds, given.seed_kinds), "kind"),
        (given.origin != ANALYSIS, "origin"),
        (not given.new, "existing"),
        (not given.ai_answers, "ai_answers_off"),
    )
    return [reason for failed, reason in checks if failed]


@dataclass
class Valve:
    """The valve's running counters over one run: the AI answers it let through and the nodes they touch."""

    limit: int
    max_share: float
    spec_nodes: int
    answered: int = 0
    nodes: set[str] = field(default_factory=set)

    def admit(self, routed: Routed, nodes: Collection[str]) -> Routed:
        """``routed``, or an ``ai`` answer over the valve routed ``ask`` (reason ``ai_limit``); an answer let through
        counts (module docstring)."""

        if routed.route != AI or routed.mode != AI_MODE:
            return routed
        share = len(self.nodes) / self.spec_nodes if self.spec_nodes else 1.0
        if self.answered >= self.limit or share >= self.max_share:
            return replace(routed, route=ASK, mode=None, reasons=(AI_LIMIT,))
        self.answered += 1
        self.nodes |= set(nodes)
        return routed


def routed_payload(routed: Routed) -> Mapping[str, object]:
    return {
        "severity": routed.severity,
        "route": routed.route,
        "mode": routed.mode,
        "reasons": list(routed.reasons),
        "gates": dict(routed.gates),
        "confidence": routed.confidence,
    }
