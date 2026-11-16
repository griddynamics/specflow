"""Acceptance tests of graph.gap: Severity and routing (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from typing import Any

from gap_acceptance_support import Planted, assert_all_recorded, gap_corpus_graph, pending
from gap_detection_support import PROJECT, TracingReader
from gap_investigation_support import loop_world
from gap_port_fakes import SeedInvestigator
from graph.core import load_ontology
from graph.gap import GapService, run_gap
from graph.gap.models import GapReport, WitnessRef
from graph.gap.settings import GapSettings
from graph.gap.steps.route import route_report
from graph.gap.steps.snapshot import GraphSnapshot, read_snapshot
from test_graph_gap_run import interfaces, mini_world


async def corpus_snapshot() -> GraphSnapshot:
    ontology = load_ontology()
    return await read_snapshot(TracingReader(gap_corpus_graph().reader, ontology), PROJECT, ontology)


def report(**fields: Any) -> GapReport:
    base: dict[str, Any] = {
        "verdict": "gap",
        "kind": "missing",
        "question": "What is missing here?",
        "about": (Planted.PLACE_ORDER,),
        "witness": (WitnessRef(fact=0),),
    }
    return GapReport.model_validate({**base, **fields})


async def test_fr_grte_01_code_decide_route_kind_severity_gates_model() -> None:
    """Inspection and test: a report with `ai_answerable: true` on a contradiction is routed `ask`."""

    snapshot = await corpus_snapshot()
    ai_kinds = ("missing", "ambiguous", "inconsistency")
    contradiction = report(
        kind="contradiction",
        severity="minor",
        ai_answerable=True,
        about=(Planted.REVIEW_LIMIT_ORDERS, Planted.REVIEW_LIMIT_PAYMENTS),
    )

    routed = route_report(contradiction, snapshot, ai_kinds=ai_kinds)
    pointed = route_report(contradiction, snapshot, ai_kinds=ai_kinds, pointer=True)

    assert routed.route == "ask" and routed.severity == "major" and "kind" in routed.reasons
    assert pointed.route == "ask"
    minor = report(severity="minor", ai_answerable=True)
    assert route_report(minor, snapshot, ai_kinds=ai_kinds).route == "ask"  # the AI branch comes with session 11
    world = await loop_world()
    await run_gap(world.context(), mode="full")
    contradictions = [props for props in world.gap_nodes().values() if props["kind"] == "contradiction"]
    assert [props["route"] for props in contradictions] == ["ask"]
    assert_all_recorded(world.gateway)


async def test_fr_grte_02_blocker_major_gap_always_routed_ask() -> None:
    """Given reports of each severity, then blockers and majors are `ask`."""

    snapshot = await corpus_snapshot()
    kinds = ("missing", "ambiguous", "inconsistency")
    planned = {Planted.PLACE_ORDER}
    for severity in ("blocker", "major"):
        routed = route_report(report(severity=severity, ai_answerable=True), snapshot, planned=planned, ai_kinds=kinds)
        assert (routed.severity, routed.route) == (severity, "ask")
        assert "severity" in routed.reasons
        pointed = route_report(report(severity=severity), snapshot, planned=planned, ai_kinds=kinds, pointer=True)
        assert pointed.route == "ai" and pointed.mode == "pointer"  # nothing new is decided (answers.md "Routing")
    assert route_report(report(severity="minor"), snapshot, ai_kinds=kinds).route == "ask"
    assert route_report(report(severity="info"), snapshot, ai_kinds=kinds).route is None
    off = route_report(report(severity="major"), snapshot, pointer=True, ai_answers=False)
    assert off.route == "ask" and "ai_answers_off" in off.reasons


@pending("11")
def test_fr_grte_03_minor_gap_routed_ai_kind_ai_kinds() -> None:
    """Given a minor missing-information gap with confidence 0.84, then it is `ask`; with 0.9 and all gates, then
    `ai`.
    """


async def test_fr_grte_04_info_report_become_run_note_graph_node() -> None:
    """Given an info report, then a `gap_note` row exists and no `Gap` node."""

    world = await loop_world()

    result = await run_gap(world.context(), mode="full")

    titles = {"No security requirement in the spec", "No data protection requirement in the spec"}
    notes = [note for note in world.records.notes[PROJECT] if note.run_id == result.run_id and note.text in titles]
    assert len(notes) == 2 and all(note.detector == "MS-COVERAGE" for note in notes)
    assert not any(props["title"] in titles for props in world.gap_nodes().values())
    assert result.counts["notes"] >= 2
    assert_all_recorded(world.gateway)


@pending("11", "limits: 08")
def test_fr_grte_05_ai_answers_stop_at_run_limit_or_node_share() -> None:
    """Given 25 AI-eligible gaps, then 20 are answered by AI and 5 are `ask`."""


async def test_fr_grte_06_run_show_at_most_configured_number_new() -> None:
    """Given 30 new `ask` gaps, then the default inbox lists 25."""

    world = await mini_world(interfaces(30))
    world.investigator = SeedInvestigator()
    service = GapService(world.executor, world.reader, world.ontology, world.rules, world.records, world.amendments)

    result = await run_gap(world.context(), mode="full")

    assert result.counts["gaps_new"]["total"] == 30 and result.counts["held_back"] == 5
    inbox = await service.list_gaps(PROJECT, held_back=False)
    assert len(inbox.gaps) == 25 and all(not gap.held_back for gap in inbox.gaps)
    assert len((await service.list_gaps(PROJECT, held_back=True)).gaps) == 5
    assert len((await service.list_gaps(PROJECT)).gaps) == 30
    assert all(gap.status == "open" for gap in (await service.list_gaps(PROJECT)).gaps)

    await run_gap(world.context(settings=GapSettings()), mode="update")

    assert len((await service.list_gaps(PROJECT, held_back=False)).gaps) == 30
