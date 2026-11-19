"""Detectors of graph.gap: one test each that it fires, does not fire and skips tracking nodes (docs/graph/gap/
detection.md "Detector catalogue"; FR-GDET-01 to FR-GDET-05, FR-GDET-10, FR-GREL-03 to FR-GREL-05)."""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache

import pytest

from gap_detection_support import MiniGraph
from graph.core import load_ontology
from graph.gap.models import BuildFacts, CutEdge, RefusedPair, Witness
from graph.gap.rules import GapRules, load_gap_rules
from graph.gap.steps.detect import (
    NOT_SHIPPED,
    RULES,
    DetectOutcome,
    Seed,
    detect,
    facts_of,
    question_text,
    same_question,
    seeds_by_section,
    witness_holds,
    witness_section,
)
from graph.gap.steps.relevance import RelevanceOutcome, SectionRelevance
from graph.gap.steps.snapshot import GraphSnapshot


@cache
def shipped_rules() -> GapRules:
    return load_gap_rules(ontology=load_ontology())


def run(
    graph: MiniGraph,
    *,
    tracking: Iterable[str] = (),
    mixed: Iterable[str] = (),
    build: BuildFacts | None = None,
    registry: dict | None = None,
) -> tuple[DetectOutcome, GraphSnapshot]:
    """Detect on ``graph``; sections whose heading is in ``tracking``/``mixed`` are judged so, the rest spec."""

    snapshot = graph.snapshot()
    judged = {
        section.ident: SectionRelevance(section, "tracking", "rules")
        for section in snapshot.sections
        if section.heading_path[-1] in set(tracking)
    } | {
        section.ident: SectionRelevance(section, "mixed", "model")
        for section in snapshot.sections
        if section.heading_path[-1] in set(mixed)
    }
    kwargs = {} if registry is None else {"registry": registry}
    outcome = detect(snapshot, rules=shipped_rules(), relevance=RelevanceOutcome(judged), build=build, **kwargs)
    return outcome, snapshot


def by(outcome: DetectOutcome, code: str) -> list[Seed]:
    return [seed for seed in outcome.seeds if seed.detector == code]


def owned_module(graph: MiniGraph, spec_id: str = "MOD::ORDERS", **attributes: str) -> None:
    graph.node(spec_id, "Module", spec_id.split("::")[1].title(), **attributes)


# MS-OWNER


def test_ms_owner_fires_on_a_requirement_reaching_no_internal_module() -> None:
    graph = MiniGraph()
    graph.section("Export", "Customers can export orders.", "Admins can export customers.", "Staff see it.")
    owned_module(graph)
    graph.node("MOD::STRIPE", "Module", "Stripe", external="yes")
    graph.node("REQ::FR::EXPORT", "FunctionalRequirement", "Export", quote="Customers can export orders.")
    graph.node("REQ::FR::ADMIN", "FunctionalRequirement", "Admin export", quote="Admins can export customers.")
    graph.node("REQ::FR::STAFF", "FunctionalRequirement", "Staff", quote="Staff see it.")
    graph.edge("REQ::FR::ADMIN", "PART_OF", "MOD::ORDERS")
    graph.edge("REQ::FR::STAFF", "RELATES_TO", "MOD::STRIPE")

    outcome, _ = run(graph)

    seeds = by(outcome, "MS-OWNER")
    assert [seed.about for seed in seeds] == [("REQ::FR::EXPORT",), ("REQ::FR::STAFF",)]
    assert seeds[0].strength == "weak"
    assert seeds[0].witness[0].fact == "REQ::FR::EXPORT reaches no owner along the spine"
    assert seeds[0].witness[1].quote == "Customers can export orders."


def test_ms_owner_skips_out_of_scope_and_tracking_nodes() -> None:
    graph = MiniGraph()
    graph.section("Later", "Customers can trade in old cards.")
    graph.node(
        "REQ::FR::TRADE", "FunctionalRequirement", "Trade", quote="Customers can trade in old cards.", in_scope="no"
    )
    graph.section("Sprint scope", "SHOP-1 saved carts.")
    graph.node("REQ::FR::SAVED", "FunctionalRequirement", "Saved carts", quote="SHOP-1 saved carts.")

    outcome, snapshot = run(graph, tracking=["Sprint scope"])

    assert by(outcome, "MS-OWNER") == []
    assert outcome.by_detector["MS-OWNER"]["skipped_tracking"] == 1
    sprint = next(section for section in snapshot.sections if section.heading_path[-1] == "Sprint scope")
    assert outcome.removed[sprint.ident] == frozenset({"REQ::FR::SAVED"})


def test_a_structural_seed_in_a_mixed_section_needs_obligation_wording_and_becomes_weak() -> None:
    graph = MiniGraph()
    graph.section("Release plan", "R2 must support refunds.", "R3 hardening.")
    graph.node("IFC::REFUNDS", "Interface", "Refunds API", quote="R2 must support refunds.")
    graph.node("IFC::HARDENING", "Interface", "Hardening API", quote="R3 hardening.")

    outcome, _ = run(graph, mixed=["Release plan"])

    seeds = by(outcome, "MS-PROVIDER")
    assert [(seed.about, seed.strength, seed.relevance) for seed in seeds] == [(("IFC::REFUNDS",), "weak", "mixed")]
    assert outcome.by_detector["MS-PROVIDER"]["skipped_tracking"] == 1


# MS-PROVIDER, MS-STORE


def test_ms_provider_and_ms_store_fire_on_a_missing_relation_only() -> None:
    graph = MiniGraph()
    graph.section("Interfaces", "The Orders API.", "The Refund API.", "Orders are stored.", "Refunds exist.")
    owned_module(graph)
    graph.node("IFC::ORDERS_API", "Interface", "Orders API", quote="The Orders API.")
    graph.node("IFC::REFUND_API", "Interface", "Refund API", quote="The Refund API.")
    graph.node("ENT::ORDER", "DataEntity", "Order", quote="Orders are stored.")
    graph.node("ENT::REFUND", "DataEntity", "Refund", quote="Refunds exist.")
    graph.edge("IFC::ORDERS_API", "PROVIDED_BY", "MOD::ORDERS")
    graph.edge("ENT::ORDER", "STORED_IN", "MOD::ORDERS")

    outcome, _ = run(graph)

    assert [seed.about for seed in by(outcome, "MS-PROVIDER")] == [("IFC::REFUND_API",)]
    assert [seed.about for seed in by(outcome, "MS-STORE")] == [("ENT::REFUND",)]
    assert by(outcome, "MS-STORE")[0].witness[0].fact == "ENT::REFUND has no outgoing STORED_IN relation"


def test_ms_provider_skips_a_tracking_interface() -> None:
    graph = MiniGraph()
    graph.section("Sprint scope", "SHOP-2 the Report API.")
    graph.node("IFC::REPORT_API", "Interface", "Report API", quote="SHOP-2 the Report API.")

    outcome, _ = run(graph, tracking=["Sprint scope"])

    assert by(outcome, "MS-PROVIDER") == []


# MS-CONTRACT


def test_ms_contract_fires_on_modules_joined_without_an_interface_between_them() -> None:
    graph = MiniGraph()
    graph.section("Integration", "Orders calls Billing to charge.", "Orders calls Stock.")
    owned_module(graph)
    owned_module(graph, "MOD::BILLING")
    owned_module(graph, "MOD::STOCK")
    graph.node("MOD::STRIPE", "Module", "Stripe", external="yes")
    graph.node("REQ::INT::CHARGE", "IntegrationRequirement", "Charge", quote="Orders calls Billing to charge.")
    graph.edge("REQ::INT::CHARGE", "PART_OF", "MOD::ORDERS")
    graph.edge("REQ::INT::CHARGE", "RELATES_TO", "MOD::BILLING")
    graph.edge("MOD::ORDERS", "INTEGRATES_WITH", "MOD::STOCK")
    graph.edge("MOD::ORDERS", "INTEGRATES_WITH", "MOD::STRIPE")
    graph.node("IFC::STOCK_API", "Interface", "Stock API")
    graph.edge("IFC::STOCK_API", "PROVIDED_BY", "MOD::STOCK")
    graph.edge("IFC::STOCK_API", "CONSUMED_BY", "MOD::ORDERS")

    outcome, _ = run(graph)

    seeds = by(outcome, "MS-CONTRACT")
    assert [(seed.about, seed.context) for seed in seeds] == [(("MOD::BILLING", "MOD::ORDERS"), ("REQ::INT::CHARGE",))]
    assert seeds[0].witness[1].quote == "Orders calls Billing to charge."


def test_ms_contract_reads_a_direct_integration_with_its_fact_only() -> None:
    graph = MiniGraph()
    owned_module(graph)
    owned_module(graph, "MOD::STOCK")
    graph.edge("MOD::ORDERS", "INTEGRATES_WITH", "MOD::STOCK")

    outcome, _ = run(graph)

    seeds = by(outcome, "MS-CONTRACT")
    assert [(seed.about, len(seed.witness), seed.context) for seed in seeds] == [(("MOD::ORDERS", "MOD::STOCK"), 1, ())]


# MS-CRITERIA, AM-IMPLIED, AS-FLAG


def test_ms_criteria_am_implied_and_as_flag_read_their_attributes() -> None:
    graph = MiniGraph()
    graph.section("Rules", "Customers can pay.", "Customers can refund.", "Every customer has an email.")
    graph.node("REQ::FR::PAY", "FunctionalRequirement", "Pay", quote="Customers can pay.")
    graph.node(
        "REQ::FR::REFUND",
        "FunctionalRequirement",
        "Refund",
        quote="Customers can refund.",
        acceptance_criteria=["Refunded within a day"],
        source_status="implied",
    )
    graph.node("REQ::CON::EMAIL", "Constraint", "Email", quote="Every customer has an email.", is_assumption="yes")

    outcome, _ = run(graph)

    assert [seed.about for seed in by(outcome, "MS-CRITERIA")] == [("REQ::FR::PAY",)]
    assert [seed.about for seed in by(outcome, "AM-IMPLIED")] == [("REQ::FR::REFUND",)]
    flag = by(outcome, "AS-FLAG")
    assert [(seed.about, seed.witness[0].quote) for seed in flag] == [
        (("REQ::CON::EMAIL",), "Every customer has an email.")
    ]


def test_attribute_detectors_fall_back_to_a_fact_and_skip_tracking() -> None:
    graph = MiniGraph()
    graph.node("REQ::CON::NO_LINE", "Constraint", "No line", is_assumption="yes")
    graph.section("Retrospective", "We assume the team stays.")
    graph.node("REQ::CON::TEAM", "Constraint", "Team", quote="We assume the team stays.", is_assumption="yes")

    outcome, _ = run(graph, tracking=["Retrospective"])

    assert [seed.witness[0].fact for seed in by(outcome, "AS-FLAG")] == ["REQ::CON::NO_LINE is_assumption yes"]


# MS-COVERAGE


def test_ms_coverage_seeds_each_uncovered_topic_about_the_project() -> None:
    graph = MiniGraph()
    graph.section("Qualities", "Response time under 2 s.", "Uptime is 99.9 %.")
    graph.node("REQ::NFR::LATENCY", "QualityRequirement", "Latency", quote="Response time under 2 s.")
    graph.section("Sprint scope", "Add access control.")
    graph.node("REQ::NFR::ACCESS", "QualityRequirement", "Access", quote="Add access control.")
    graph.section("Ops", "Uptime is 99.9 %.")
    graph.node("REQ::CON::UPTIME", "Constraint", "Uptime", quote="Uptime is 99.9 %.")

    outcome, _ = run(graph, tracking=["Sprint scope"])

    topics = sorted(seed.witness[0].fact.rsplit(" ", 1)[-1] for seed in by(outcome, "MS-COVERAGE"))
    assert topics == ["accessibility", "data_protection", "observability", "security"]
    assert all(seed.about == () for seed in by(outcome, "MS-COVERAGE"))


# MS-REF


def test_ms_ref_fires_on_references_that_resolve_to_nothing() -> None:
    graph = MiniGraph()
    graph.section("1.2 Orders", "FR-12 Customers can order.", "See section 1.2 and section 4.1.", "See FR-99.")
    graph.section("Links", "See billing-spec.md and spec.md.", "Release 2.3 is planned.")
    graph.node(
        "REQ::FR::FR_12_ORDER",
        "FunctionalRequirement",
        "Order",
        quote="FR-12 Customers can order.",
        fragment=graph.fragments[0].fragment_id,
    )

    outcome, _ = run(graph)

    quotes = sorted(seed.witness[0].quote for seed in by(outcome, "MS-REF"))
    assert quotes == ["See FR-99.", "See billing-spec.md and spec.md.", "See section 1.2 and section 4.1."]


def test_ms_ref_names_the_nodes_on_the_citing_line_and_is_a_text_detector() -> None:
    graph = MiniGraph()
    graph.section("Sprint scope", "Customers can order, see FR-7.")
    graph.node("REQ::FR::ORDER", "FunctionalRequirement", "Order", quote="Customers can order, see FR-7.")

    outcome, _ = run(graph, tracking=["Sprint scope"])

    seeds = by(outcome, "MS-REF")
    assert [(seed.about, seed.relevance) for seed in seeds] == [(("REQ::FR::ORDER",), "tracking")]


# AM-VAGUE, AM-MEASURE


def test_am_vague_fires_on_a_vague_term_without_a_measure_in_its_sentence() -> None:
    graph = MiniGraph()
    graph.section("Qualities", "The admin panel should be fast.", "Search is fast: under 2 s.", "Orders are kept.")
    graph.node("REQ::NFR::FAST", "QualityRequirement", "Fast", quote="The admin panel should be fast.")
    graph.node("REQ::NFR::SEARCH", "QualityRequirement", "Search", quote="Search is fast: under 2 s.")
    graph.node("REQ::FR::KEEP", "FunctionalRequirement", "Keep", quote="Orders are kept.")

    outcome, _ = run(graph, tracking=["Qualities"])

    assert [seed.about for seed in by(outcome, "AM-VAGUE")] == [("REQ::NFR::FAST",)]
    assert by(outcome, "AM-VAGUE")[0].relevance == "tracking"
    assert [seed.about for seed in by(outcome, "AM-MEASURE")] == [("REQ::NFR::FAST",)]


def test_am_measure_needs_text_and_ignores_numbers() -> None:
    graph = MiniGraph()
    graph.node("REQ::NFR::BLANK", "QualityRequirement", "Blank")
    graph.section("Qualities", "Pages load in 2 s.")
    graph.node("REQ::NFR::LOAD", "QualityRequirement", "Load", quote="Pages load in 2 s.")

    outcome, _ = run(graph)

    assert by(outcome, "AM-MEASURE") == []


# CT-VALUE


def test_ct_value_seeds_a_refused_pair_with_both_lines() -> None:
    graph = MiniGraph()
    graph.section("Review", "Orders over 1,000 EUR are reviewed.")
    graph.node("REQ::BR::ORDERS", "BusinessRule", "Orders", quote="Orders over 1,000 EUR are reviewed.")
    graph.section("Payments", "Payments over 500 EUR are held.")
    graph.node("REQ::BR::PAYMENTS", "BusinessRule", "Payments", quote="Payments over 500 EUR are held.")
    graph.node("MOD::ORDERS", "Module", "Orders")
    build = BuildFacts(
        refused_pairs=(
            RefusedPair(
                first_spec_id="REQ::BR::ORDERS",
                second_spec_id="REQ::BR::PAYMENTS",
                label="BusinessRule",
                guard="numbers",
            ),
            RefusedPair(
                first_spec_id="REQ::BR::ORDERS", second_spec_id="REQ::BR::GONE", label="BusinessRule", guard="numbers"
            ),
            RefusedPair(first_spec_id="REQ::BR::ORDERS", second_spec_id="MOD::ORDERS", label="Module", guard="numbers"),
        )
    )

    outcome, _ = run(graph, tracking=["Payments"], build=build)

    seeds = by(outcome, "CT-VALUE")
    assert [seed.about for seed in seeds] == [("REQ::BR::ORDERS", "REQ::BR::PAYMENTS")]
    assert [item.quote for item in seeds[0].witness] == [
        "Orders over 1,000 EUR are reviewed.",
        "Payments over 500 EUR are held.",
    ]


# IN-SCOPE


def test_in_scope_fires_when_an_in_scope_node_uses_an_out_of_scope_one() -> None:
    graph = MiniGraph()
    graph.node("REQ::FR::PAY", "FunctionalRequirement", "Pay")
    graph.node("IFC::CRYPTO", "Interface", "Crypto API", in_scope="no")
    graph.node("IFC::CARDS", "Interface", "Cards API")
    graph.node("REQ::FR::LATER", "FunctionalRequirement", "Later", in_scope="no")
    graph.edge("REQ::FR::PAY", "RELATES_TO", "IFC::CRYPTO")
    graph.edge("REQ::FR::PAY", "RELATES_TO", "IFC::CARDS")
    graph.edge("REQ::FR::LATER", "RELATES_TO", "IFC::CRYPTO")
    graph.node("MOD::SHOP", "Module", "Shop")
    graph.edge("IFC::CARDS", "CONSUMED_BY", "MOD::SHOP")

    outcome, _ = run(graph)

    seeds = by(outcome, "IN-SCOPE")
    assert [(seed.about, seed.context) for seed in seeds] == [(("REQ::FR::PAY",), ("IFC::CRYPTO",))]


# IN-CYCLE


def test_in_cycle_finds_cycles_including_a_cut_edge() -> None:
    graph = MiniGraph()
    graph.section("Deps", "Checkout needs payment.", "Payment needs checkout.")
    fragment = graph.fragments[-1].fragment_id
    for name in ("CHECKOUT", "PAYMENT", "STOCK", "SHIPPING", "SELF"):
        graph.node(f"FEAT::{name}", "Feature", name.title())
    graph.edge("FEAT::CHECKOUT", "DEPENDS_ON", "FEAT::PAYMENT", evidence=[(fragment, "Checkout needs payment.")])
    graph.edge("FEAT::PAYMENT", "DEPENDS_ON", "FEAT::CHECKOUT", evidence=[(fragment, "Payment needs checkout.")])
    graph.edge("FEAT::STOCK", "DEPENDS_ON", "FEAT::SHIPPING")
    graph.edge("FEAT::SELF", "DEPENDS_ON", "FEAT::SELF")
    build = BuildFacts(
        cut_edges=(CutEdge(relation="DEPENDS_ON", source_spec_id="FEAT::SHIPPING", target_spec_id="FEAT::STOCK"),)
    )

    outcome, _ = run(graph, build=build)

    seeds = sorted(by(outcome, "IN-CYCLE"), key=lambda seed: seed.about)
    assert [seed.about for seed in seeds] == [
        ("FEAT::CHECKOUT", "FEAT::PAYMENT"),
        ("FEAT::SELF",),
        ("FEAT::SHIPPING", "FEAT::STOCK"),
    ]
    assert [item.quote for item in seeds[0].witness[1:]] == ["Checkout needs payment.", "Payment needs checkout."]


def test_in_cycle_does_not_fire_on_a_chain() -> None:
    graph = MiniGraph()
    for name in ("A", "B", "C"):
        graph.node(f"FEAT::{name}", "Feature", name)
    graph.edge("FEAT::A", "DEPENDS_ON", "FEAT::B")
    graph.edge("FEAT::B", "DEPENDS_ON", "FEAT::C")

    outcome, _ = run(graph)

    assert by(outcome, "IN-CYCLE") == []


# OQ-STATED, OQ-TBD


def test_oq_stated_seeds_every_question_about_itself_and_what_it_relates_to() -> None:
    graph = MiniGraph()
    graph.section("Place order", "Customers place orders.", "Can a customer cancel a paid order?")
    graph.node("REQ::FR::PLACE", "FunctionalRequirement", "Place", quote="Customers place orders.")
    graph.node("QST::CANCEL", "OpenQuestion", "Cancel?", quote="Can a customer cancel a paid order?")
    graph.edge("QST::CANCEL", "RELATES_TO", "REQ::FR::PLACE")
    graph.section("Sprint scope", "Who owns refunds?")
    graph.node("QST::OWNER", "OpenQuestion", "Owner?", quote="Who owns refunds?")

    outcome, snapshot = run(graph, tracking=["Sprint scope"])

    seeds = by(outcome, "OQ-STATED")
    assert [(seed.about, seed.strength) for seed in seeds] == [
        (("QST::CANCEL", "REQ::FR::PLACE"), "strong"),
        (("QST::OWNER",), "strong"),
    ]
    assert question_text(snapshot, seeds[0]) == "can a customer cancel a paid order?"


def test_a_stated_question_about_out_of_scope_nodes_only_is_an_info_note() -> None:
    graph = MiniGraph()
    graph.section("Later", "Crypto later.", "Which coins?")
    graph.node("REQ::FR::CRYPTO", "FunctionalRequirement", "Crypto", quote="Crypto later.", in_scope="no")
    graph.node("QST::COINS", "OpenQuestion", "Coins?", quote="Which coins?")
    graph.edge("QST::COINS", "RELATES_TO", "REQ::FR::CRYPTO")

    outcome, _ = run(graph)

    assert by(outcome, "OQ-STATED") == []
    assert [(note.detector, note.spec_ids) for note in outcome.notes] == [
        ("OQ-STATED", ("QST::COINS", "REQ::FR::CRYPTO"))
    ]
    assert outcome.by_detector["OQ-STATED"]["notes"] == 1


def test_oq_tbd_fires_on_a_placeholder_no_question_grounds() -> None:
    graph = MiniGraph()
    graph.section("Limits", "The refund limit is TBD.", "Shipping cost: to be decided?", "Nothing open here.")
    graph.node("QST::SHIPPING", "OpenQuestion", "Shipping cost?", quote="Shipping cost: to be decided?")

    outcome, _ = run(graph)

    assert [seed.witness[0].quote for seed in by(outcome, "OQ-TBD")] == ["The refund limit is TBD."]


# The driver


def test_fr_gdet_10_a_failing_detector_is_named_and_the_others_still_seed() -> None:
    graph = MiniGraph()
    graph.section("Rules", "Every customer has an email.")
    graph.node("REQ::CON::EMAIL", "Constraint", "Email", quote="Every customer has an email.", is_assumption="yes")

    def broken(*_: object) -> list[Seed]:
        raise RuntimeError("boom")

    outcome, _ = run(graph, registry={**RULES, "missing_relation": broken})

    assert outcome.failures == {"MS-PROVIDER": "RuntimeError", "MS-STORE": "RuntimeError"}
    assert by(outcome, "AS-FLAG")


def test_a_rule_without_a_function_is_reported_as_not_shipped() -> None:
    rules = shipped_rules()
    snapshot = MiniGraph().snapshot()

    outcome = detect(snapshot, rules=rules, relevance=RelevanceOutcome({}), slice_=2)

    assert {code for code, reason in outcome.failures.items() if reason == NOT_SHIPPED} == {
        rule.code for rule in rules.detectors.values() if rule.slice == 2 and rule.rule not in RULES
    }


def test_witnesses_resolve_against_the_snapshot() -> None:
    graph = MiniGraph()
    fragment = graph.section("Rules", "Every “customer” has   an email.")
    graph.node("REQ::CON::EMAIL", "Constraint", "Email", quote="Every “customer” has   an email.", is_assumption="yes")
    graph.node("IFC::X", "Interface", "X")
    outcome, snapshot = run(graph)

    assert witness_holds(snapshot, Witness(fragment_id=fragment, quote='Every "customer" has an email.'))
    assert not witness_holds(snapshot, Witness(fragment_id=fragment, quote="Every customer has a phone."))
    assert not witness_holds(snapshot, Witness(fragment_id="missing", quote="x"))
    facts = facts_of(outcome.seeds)
    assert witness_holds(snapshot, Witness(fact="IFC::X has no outgoing PROVIDED_BY relation"), facts)
    assert not witness_holds(snapshot, Witness(fact="IFC::Y has no outgoing PROVIDED_BY relation"), facts)


def test_seed_sections_and_signatures() -> None:
    graph = MiniGraph()
    graph.section("Rules", "Every customer has an email.")
    graph.node("REQ::CON::EMAIL", "Constraint", "Email", quote="Every customer has an email.", is_assumption="yes")
    graph.node("IFC::X", "Interface", "X")
    outcome, snapshot = run(graph)

    flag, provider = by(outcome, "AS-FLAG")[0], by(outcome, "MS-PROVIDER")[0]
    assert witness_section(snapshot, flag) == snapshot.sections[0].ident
    assert witness_section(snapshot, provider) is None
    grouped = seeds_by_section(snapshot, [flag, provider])
    assert set(grouped) == {snapshot.sections[0].ident, None}
    assert len(flag.signature) == 64
    assert flag.signature != provider.signature
    assert same_question("can a customer cancel a paid order?", "can a customer cancel a paid order ?")
    assert not same_question("who owns refunds?", "can a customer cancel a paid order?")


@pytest.mark.parametrize("code", sorted({rule.code for rule in shipped_rules().detectors_of_slice(1)}))
def test_every_slice_one_detector_has_a_function(code: str) -> None:
    assert shipped_rules().detectors[code].rule in RULES
