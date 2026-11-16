"""The relevance step of graph.gap: rules, cache, noise rules, the batched model call, the noise report and the
not-spec feedback (docs/graph/gap/detection.md "Relevance filter"; FR-GREL-01, FR-GREL-02, FR-GREL-06, FR-GREL-07)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from gap_acceptance_support import RecordedGapGateway, assert_all_recorded
from gap_detection_support import MiniGraph
from gap_port_fakes import MemoryGapRecords
from graph.core import load_ontology
from graph.core.errors import LLMError
from graph.gap.models import NoiseRuleRow, RelevanceRow
from graph.gap.names import NoiseScope, Relevance
from graph.gap.prompts import prompt_text
from graph.gap.rules import load_gap_rules
from graph.gap.steps.relevance import (
    BATCH_SECTIONS,
    GATEWAY_PROMPT,
    SECTION_CHARS,
    RelevanceAnswer,
    RelevanceOutcome,
    SectionAnswer,
    SectionRelevance,
    classify_sections,
    cue_pattern,
    has_cue,
    mark_not_spec,
    noise_report,
    noise_rule_matches,
    relevance_messages,
    section_value,
)
from graph.gap.steps.snapshot import GraphSnapshot
from graph_support import ScriptedRequest

PROJECT = "shop"


def rules(**relevance: Any) -> Any:
    loaded = load_gap_rules(ontology=load_ontology())
    return replace(loaded, relevance=replace(loaded.relevance, **relevance)) if relevance else loaded


def corpus() -> GraphSnapshot:
    graph = MiniGraph()
    graph.section("Orders", "Customers must place orders.")
    graph.node("REQ::FR::PLACE", "FunctionalRequirement", "Place", quote="Customers must place orders.")
    graph.node("MOD::ORDERS", "Module", "Orders")
    graph.edge("REQ::FR::PLACE", "PART_OF", "MOD::ORDERS")
    graph.section("Release plan", "R2 must support refunds.", document="roadmap.md", title="Roadmap")
    graph.node("REQ::FR::R2", "FunctionalRequirement", "R2 refunds", quote="R2 must support refunds.")
    graph.section("Sprint scope", "SHOP-1 saved carts.", document="roadmap.md", title="Roadmap")
    graph.node("REQ::FR::SHOP_1", "FunctionalRequirement", "SHOP-1 saved carts", quote="SHOP-1 saved carts.")
    return graph.snapshot()


def section(heading: str) -> Any:
    return next(item for item in corpus().sections if item.heading_path[-1] == heading)


def answer(*relevance: Relevance) -> Any:
    def handler(request: ScriptedRequest) -> RelevanceAnswer:
        del request
        return RelevanceAnswer(
            sections=[
                SectionAnswer(section=f"S{n}", relevance=value, signal="x") for n, value in enumerate(relevance, 1)
            ]
        )

    return handler


def gateway(*relevance: Relevance) -> RecordedGapGateway:
    return RecordedGapGateway(recordings={}).handle(GATEWAY_PROMPT, answer(*relevance))


def by_heading(outcome: Any) -> dict[str, tuple[str, str]]:
    return {key[1][-1]: (item.relevance, item.by) for key, item in outcome.sections.items()}


async def test_rules_decide_spec_and_tracking_and_the_model_settles_uncertain_sections() -> None:
    records, scripted = MemoryGapRecords(), gateway("mixed")

    outcome = await classify_sections(corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=scripted)

    assert by_heading(outcome) == {
        "Orders": ("spec", "rules"),
        "Release plan": ("mixed", "model"),
        "Sprint scope": ("tracking", "rules"),
    }
    assert outcome.model_calls == 1
    assert len(scripted.calls_of("gap_relevance")) == 1
    assert {row.relevance for row in records.relevance_rows[PROJECT].values()} == {"spec", "mixed", "tracking"}
    assert outcome.counts() == {"mixed": {"model": 1}, "spec": {"rules": 1}, "tracking": {"rules": 1}}
    assert_all_recorded(scripted)


async def test_fr_grel_02_a_second_run_reads_the_cache_and_makes_no_call() -> None:
    records = MemoryGapRecords()
    await classify_sections(corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=gateway("mixed"))
    second = gateway("spec")

    outcome = await classify_sections(corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=second)

    assert second.calls_of("gap_relevance") == []
    assert by_heading(outcome)["Release plan"] == ("mixed", "model")


async def test_changed_text_misses_the_cache() -> None:
    records = MemoryGapRecords()
    await classify_sections(corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=gateway("mixed"))
    graph = MiniGraph()
    graph.section("Release plan", "R2 must support refunds and returns.", document="roadmap.md", title="Roadmap")
    graph.node("REQ::FR::R2", "FunctionalRequirement", "R2 refunds", quote="R2 must support refunds and returns.")
    second = gateway("tracking")

    outcome = await classify_sections(
        graph.snapshot(), project_id=PROJECT, rules=rules(), records=records, gateway=second
    )

    assert len(second.calls_of("gap_relevance")) == 1
    assert by_heading(outcome)["Release plan"] == ("tracking", "model")


async def test_a_failed_call_leaves_sections_mixed_and_uncached() -> None:
    records = MemoryGapRecords()
    failing = RecordedGapGateway(recordings={}, fail_roles=("gap_relevance",))

    outcome = await classify_sections(corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=failing)

    assert by_heading(outcome)["Release plan"] == ("mixed", "rules")
    assert outcome.failed_calls == 1
    assert (
        "model_failed"
        in next(item for item in outcome.sections.values() if item.by == "rules" and item.relevance == "mixed").signals
    )
    assert all(row.relevance != "mixed" for row in records.relevance_rows[PROJECT].values())


async def test_a_section_the_model_skips_stays_mixed() -> None:
    records = MemoryGapRecords()

    outcome = await classify_sections(corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=gateway())

    assert by_heading(outcome)["Release plan"] == ("mixed", "rules")


async def test_without_model_classification_mixed_is_the_cached_rules_answer() -> None:
    records = MemoryGapRecords()

    outcome = await classify_sections(
        corpus(), project_id=PROJECT, rules=rules(classify_with_model=False), records=records, gateway=gateway("spec")
    )

    assert by_heading(outcome)["Release plan"] == ("mixed", "rules")
    assert any(row.relevance == "mixed" for row in records.relevance_rows[PROJECT].values())


async def test_an_estimate_makes_no_call_and_writes_nothing() -> None:
    records, scripted = MemoryGapRecords(), gateway("spec")

    outcome = await classify_sections(
        corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=scripted, use_model=False, persist=False
    )

    assert by_heading(outcome)["Release plan"] == ("mixed", "rules")
    assert scripted.calls == []
    assert records.relevance_rows[PROJECT] == {}


async def test_uncertain_sections_go_in_batches_of_forty() -> None:
    graph = MiniGraph()
    for number in range(BATCH_SECTIONS + 5):
        graph.section(f"Release {number}", "R2 must support refunds.", document="roadmap.md", title="Roadmap")
    scripted = gateway(*(["spec"] * BATCH_SECTIONS))

    outcome = await classify_sections(
        graph.snapshot(), project_id=PROJECT, rules=rules(), records=MemoryGapRecords(), gateway=scripted
    )

    assert outcome.model_calls == 2
    assert len(scripted.calls_of("gap_relevance")) == 2


def test_the_messages_quote_sections_as_untrusted_blocks_of_at_most_1500_characters() -> None:
    graph = MiniGraph()
    graph.section("Release plan", "x" * 2000, document="roadmap.md", title="Roadmap")
    section = graph.snapshot().sections[0]

    messages = relevance_messages([section])

    assert messages[0] == {"role": "system", "content": prompt_text("relevance")}
    user = messages[1]["content"]
    assert '"id": "S1"' in user
    assert '"heading_path": ["Roadmap", "Roadmap", "Release plan"]' in user
    assert "x" * (SECTION_CHARS - len("## Release plan\n")) in user
    assert "x" * (SECTION_CHARS - len("## Release plan\n") + 1) not in user


async def test_fr_grel_06_noise_rules_decide_tracking_by_person_and_are_never_cached() -> None:
    records = MemoryGapRecords()
    at = datetime(2026, 11, 1, tzinfo=UTC)
    records.noise[PROJECT].append(
        NoiseRuleRow(
            rule_id="N1",
            scope="section",
            value="upload:spec \u203a Shop specification \u203a Orders",
            reason="",
            created_by="u",
            created_at=at,
        )
    )

    outcome = await classify_sections(
        corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=gateway("mixed")
    )

    assert by_heading(outcome)["Orders"] == ("tracking", "person")
    assert all(row.lineage != "upload:spec" for row in records.relevance_rows[PROJECT].values())
    report = noise_report(outcome, {})
    assert ("Orders", "person", ("noise_rule:N1",)) in {
        (item.heading_path[-1], item.by, item.signals) for item in report.sections
    }


def test_noise_rules_match_documents_sections_and_patterns() -> None:
    orders, plan = section("Orders"), section("Release plan")
    at = datetime(2026, 11, 1, tzinfo=UTC)

    def rule(scope: NoiseScope, value: str) -> NoiseRuleRow:
        return NoiseRuleRow(rule_id="N", scope=scope, value=value, reason="", created_by="u", created_at=at)

    assert noise_rule_matches(rule("document", "roadmap.md"), plan)
    assert noise_rule_matches(rule("document", "upload:roadmap"), plan)
    assert not noise_rule_matches(rule("document", "roadmap.md"), orders)
    assert noise_rule_matches(rule("section", "upload:roadmap \u203a Roadmap"), plan)
    assert noise_rule_matches(rule("section", section_value(plan)), plan)
    assert not noise_rule_matches(rule("section", "upload:roadmap \u203a Road"), plan)
    assert noise_rule_matches(rule("pattern", r"release\s+plan"), plan)
    assert not noise_rule_matches(rule("pattern", "("), plan)


async def test_a_persons_cached_mark_wins_over_the_rules() -> None:
    records = MemoryGapRecords()
    plan = section("Release plan")
    await records.put_relevance(
        PROJECT,
        [
            RelevanceRow(
                lineage=plan.lineage,
                heading_path=plan.heading_path,
                text_hash=plan.text_hash,
                relevance="spec",
                by="person",
            )
        ],
    )

    outcome = await classify_sections(corpus(), project_id=PROJECT, rules=rules(), records=records, gateway=gateway())

    assert by_heading(outcome)["Release plan"] == ("spec", "person")


async def test_fr_grel_07_not_spec_feedback_marks_spec_sections_mixed() -> None:
    records = MemoryGapRecords()
    snapshot = corpus()
    outcome = await classify_sections(
        snapshot, project_id=PROJECT, rules=rules(), records=records, gateway=gateway("mixed")
    )

    updated = await mark_not_spec(
        snapshot, outcome, ["REQ::FR::PLACE", "REQ::FR::R2"], project_id=PROJECT, records=records
    )

    assert by_heading(updated)["Orders"] == ("mixed", "model")
    assert by_heading(updated)["Release plan"] == ("mixed", "model")
    assert "not_spec" in next(
        item.signals for item in updated.sections.values() if item.section.heading_path[-1] == "Orders"
    )
    assert not records.noise[PROJECT]
    second = await classify_sections(snapshot, project_id=PROJECT, rules=rules(), records=records, gateway=gateway())
    assert by_heading(second)["Orders"] == ("mixed", "model")


def test_the_noise_report_lists_tracking_and_mixed_sections_with_removed_nodes() -> None:
    snapshot = corpus()
    orders, plan, sprint = section("Orders"), section("Release plan"), section("Sprint scope")
    outcome = RelevanceOutcome(
        {
            orders.ident: SectionRelevance(orders, "spec", "rules"),
            plan.ident: SectionRelevance(plan, "mixed", "model", ("title:release plan",)),
            sprint.ident: SectionRelevance(sprint, "tracking", "rules"),
        }
    )

    report = noise_report(outcome, {sprint.ident: {"REQ::FR::SHOP_1"}})

    assert [(item.heading_path[-1], item.relevance, item.removed) for item in report.sections] == [
        ("Release plan", "mixed", ()),
        ("Sprint scope", "tracking", ("REQ::FR::SHOP_1",)),
    ]
    assert outcome.of_node(snapshot, "REQ::FR::SHOP_1") == "tracking"
    assert outcome.of_node(snapshot, "REQ::FR::R2") == "mixed"
    assert outcome.of_node(snapshot, "MOD::ORDERS") == "spec"


@pytest.mark.parametrize(
    ("text", "found"),
    [("Customers can export.", True), ("A canister.", False), ("It must work.", True), ("Commonly used.", False)],
)
def test_cues_match_at_word_starts(text: str, *, found: bool) -> None:
    pattern = cue_pattern(["can ", "must", "only"])

    assert has_cue(pattern, text) is found
    assert cue_pattern([]) is None
    assert not has_cue(None, text)


async def test_a_gateway_error_other_than_a_recording_is_caught() -> None:
    class Failing:
        async def structured(self, *_: Any, **__: Any) -> Any:
            raise LLMError("timeout")

    outcome = await classify_sections(
        corpus(), project_id=PROJECT, rules=rules(), records=MemoryGapRecords(), gateway=Failing()
    )

    assert outcome.failed_calls == 1
