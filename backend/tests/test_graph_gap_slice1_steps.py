"""The slice-1 investigate, check and route steps of a gap run (docs/graph/gap/api.md "Gap runs"; answers.md
"Severity"; FR-GRUN-04 deferral of failed sites)."""

from __future__ import annotations

from typing import Any

from gap_detection_support import PROJECT, MiniGraph
from graph.gap.models import GapReport, InvestigationResult, SiteBudget, SitePack, WitnessRef
from graph.gap.steps.check import check_result
from graph.gap.steps.investigate import NO_READS, SeedInvestigator
from graph.gap.steps.route import route_report

BUDGET = SiteBudget(rounds=1, reads_per_round=1, context_tokens=100)


def graph() -> tuple[MiniGraph, str]:
    mini = MiniGraph()
    fragment = mini.section("Orders", "Orders over 1,000 EUR are reviewed.")
    mini.node("REQ::BR::REVIEW", "BusinessRule", "Review", quote="Orders over 1,000 EUR are reviewed.")
    mini.node("MOD::LEGACY", "Module", "Legacy", in_scope="no")
    return mini, fragment


async def test_the_read_menu_of_slice_1_answers_every_read_empty() -> None:
    results = [
        await NO_READS.node("A"),
        await NO_READS.evidence("A"),
        await NO_READS.neighbours("A"),
        await NO_READS.trace("A", "affects", 2),
        await NO_READS.paths("A", "B"),
        await NO_READS.similar("q", 3),
        await NO_READS.find_nodes("w"),
        await NO_READS.find_fragments("w"),
    ]

    assert all(result.items == () and result.reason == "reads_unavailable" for result in results)
    empty = SitePack(project_id=PROJECT, site_id="SITE::0", input_hash="h", mode="full", seeds=())
    assert (await SeedInvestigator().investigate(empty, NO_READS, BUDGET)).error == "site_without_seeds"


def test_check_turns_verdicts_into_outcomes_and_cleans_a_gap() -> None:
    mini, fragment = graph()
    snapshot = mini.snapshot()
    gap = GapReport(
        verdict="gap",
        kind="contradiction",
        question="  Which limit holds?  ",
        title="t" * 300,
        about=("REQ::BR::REVIEW", "REQ::BR::GONE"),
        impacts=("REQ::BR::REVIEW", "MOD::LEGACY"),
        witness=(
            WitnessRef(fragment_id=fragment, quote="Orders over 1,000 EUR are reviewed."),
            WitnessRef(fragment_id=fragment, quote="Not in the text."),
            WitnessRef(fact=0),
            WitnessRef(fact=3),
        ),
    )

    checked = check_result(InvestigationResult(report=gap), snapshot, seeds=1)

    assert checked.outcome == "gap" and checked.report is not None
    assert checked.report.about == ("REQ::BR::REVIEW",) and checked.report.impacts == ("MOD::LEGACY",)
    assert len(checked.report.witness) == 2 and len(checked.report.title) == 160
    assert checked.report.question == "Which limit holds?"
    assert set(checked.warnings) == {"about_dropped", "witness_dropped"}
    for verdict, outcome in (("not_a_gap", "not_a_gap"), ("not_spec", "not_spec"), ("duplicate_of", "duplicate")):
        other = GapReport(verdict=verdict)
        assert check_result(InvestigationResult(report=other), snapshot, seeds=1).outcome == outcome
    incomplete = GapReport(verdict="gap", kind="missing", question=" ")
    assert check_result(InvestigationResult(report=incomplete), snapshot, seeds=1).warnings == ("report_incomplete",)
    assert check_result(InvestigationResult(error="timeout"), snapshot, seeds=1).outcome == "deferred"
    loose = GapReport(verdict="gap", kind="missing", question="?", witness=(WitnessRef(),))
    assert check_result(InvestigationResult(report=loose), snapshot, seeds=1).outcome == "deferred"


def test_route_applies_the_severity_rules_that_need_no_model() -> None:
    mini, _ = graph()
    snapshot = mini.snapshot()

    def routed(**fields: Any) -> tuple[str, str | None]:
        decision = route_report(GapReport.model_validate({"verdict": "gap", "question": "?", **fields}), snapshot)
        return decision.severity, decision.route

    assert routed(kind="contradiction", severity="minor", about=("REQ::BR::REVIEW",)) == ("major", "ask")
    assert routed(kind="missing", severity="major", about=("MOD::LEGACY",)) == ("info", None)
    assert routed(kind="missing", severity="info", about=("REQ::BR::REVIEW",)) == ("info", None)
    assert routed(kind="missing", about=("REQ::BR::REVIEW",)) == ("major", "ask")
    blocker = GapReport(verdict="gap", question="?", kind="missing", severity="blocker", about=("REQ::BR::REVIEW",))
    assert route_report(blocker, snapshot, planned=set()).severity == "major"
    assert route_report(blocker, snapshot, planned={"REQ::BR::REVIEW"}).severity == "blocker"
    assert route_report(blocker, snapshot).severity == "blocker"
