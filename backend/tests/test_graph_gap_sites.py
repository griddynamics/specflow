"""The sites step of graph.gap: suppression, corroboration, grouping, ranking, the verdict cache and the run limits
(docs/graph/gap/detection.md "Seeds and corroboration", "Sites"; FR-GDET-06 to FR-GDET-09)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from gap_detection_support import PROJECT, MiniGraph
from gap_port_fakes import MemoryGapRecords, MemoryPlanView
from graph.core import load_ontology
from graph.gap.models import SuppressionRow, TaskRef, VerdictRow
from graph.gap.rules import load_gap_rules
from graph.gap.settings import GapLimits
from graph.gap.steps.detect import Seed, detect
from graph.gap.steps.relevance import RelevanceOutcome
from graph.gap.steps.sites import (
    MAX_SEEDS,
    ExistingGap,
    SiteRun,
    SitesOutcome,
    SystemParts,
    corroboration,
    form_sites,
    group_seeds,
    node_fingerprint,
    site_id,
)

AT = datetime(2026, 11, 1, tzinfo=UTC)


def rules() -> Any:
    """The shipped rules without MS-COVERAGE, whose project-wide seeds every small graph would get."""

    loaded = load_gap_rules(ontology=load_ontology())
    return replace(loaded, detectors={code: rule for code, rule in loaded.detectors.items() if code != "MS-COVERAGE"})


async def sites(graph: MiniGraph, *, records: MemoryGapRecords | None = None, **run: Any) -> SitesOutcome:
    snapshot = graph.snapshot()
    loaded = run.pop("rules", None) or rules()
    detection = detect(snapshot, rules=loaded, relevance=run.pop("relevance", RelevanceOutcome({})))
    return await form_sites(
        snapshot,
        detection,
        SiteRun(
            project_id=PROJECT,
            run_id=run.pop("run_id", "run-1"),
            mode=run.pop("mode", "full"),
            rules=loaded,
            limits=run.pop("limits", loaded.limits),
            records=records or MemoryGapRecords(),
            reader=graph.reader(),
            **run,
        ),
    )


def account() -> MiniGraph:
    """Two ownerless requirements: one with obligation wording that names a module, one with neither."""

    graph = MiniGraph()
    graph.section("Account", "Customers can export their orders.", "Newsletter preferences page.")
    graph.node("MOD::ORDERS", "Module", "Order")
    graph.node("REQ::FR::EXPORT", "FunctionalRequirement", "Export", quote="Customers can export their orders.")
    graph.node("REQ::FR::NEWS", "FunctionalRequirement", "News", quote="Newsletter preferences page.")
    return graph


def in_sites(outcome: SitesOutcome, spec_id: str) -> bool:
    return any(spec_id in site.about for site in outcome.sites)


async def test_fr_gdet_06_a_weak_seed_needs_a_corroboration_signal() -> None:
    records = MemoryGapRecords()

    outcome = await sites(account(), records=records)

    assert in_sites(outcome, "REQ::FR::EXPORT")
    assert not in_sites(outcome, "REQ::FR::NEWS")
    news = [row for row in records.seeds[PROJECT] if row.spec_ids == ("REQ::FR::NEWS",)]
    assert news and all(not row.corroborated and row.site_id is None for row in news)


async def test_a_planned_dependency_corroborates_a_weak_seed() -> None:
    plan = MemoryPlanView({"REQ::FR::NEWS": [TaskRef(task_id="TASK::1", story_id="STORY::1", status="todo")]})

    outcome = await sites(account(), plan=plan)

    assert in_sites(outcome, "REQ::FR::NEWS")


async def test_a_weak_seed_of_a_mixed_section_needs_obligation_wording_and_one_more_signal() -> None:
    graph = account()
    snapshot = graph.snapshot()
    loaded = rules()
    detection = detect(snapshot, rules=loaded, relevance=RelevanceOutcome({}))
    export = next(
        seed for seed in detection.seeds if seed.about == ("REQ::FR::EXPORT",) and seed.detector == "MS-OWNER"
    )
    parts = SystemParts.of(snapshot, loaded)

    assert corroboration(snapshot, export, rules=loaded, parts=parts) == ("obligation_wording", "names_system_part")
    alone = replace(loaded, system_parts=())
    assert corroboration(snapshot, export, rules=alone) == ("obligation_wording",)
    mixed = replace(export, relevance="mixed")
    run = SiteRun(PROJECT, "run-1", "full", alone, loaded.limits, MemoryGapRecords(), graph.reader())
    outcome = await form_sites(snapshot, replace(detection, seeds=(mixed,)), run)
    assert outcome.sites == ()


async def test_fr_gdet_07_a_suppressed_signature_is_dropped_and_counted() -> None:
    records = MemoryGapRecords()
    first = await sites(account(), records=records)
    site = next(site for site in first.sites if "REQ::FR::EXPORT" in site.about)
    for seed in site.seeds:
        await records.suppress(
            PROJECT,
            SuppressionRow(signature=seed.signature, question="q", reason="no", dismissed_by="u", dismissed_at=AT),
        )

    second = await sites(account(), records=records, run_id="run-2")

    assert not in_sites(second, "REQ::FR::EXPORT")
    assert sum(item.get("suppressed", 0) for item in second.counts["seeds_by_detector"].values()) == len(site.seeds)


async def test_fr_gdet_08_seeds_sharing_a_node_or_an_owner_and_section_merge() -> None:
    graph = MiniGraph()
    graph.section(
        "Place order", "Can a customer cancel?", "Can a customer pay twice?", "Orders are fast.", "Orders are simple."
    )
    graph.node("MOD::ORDERS", "Module", "Orders")
    graph.node("REQ::FR::PLACE", "FunctionalRequirement", "Place", acceptance_criteria=["x"])
    graph.edge("REQ::FR::PLACE", "PART_OF", "MOD::ORDERS")
    for spec_id, quote in (("QST::CANCEL", "Can a customer cancel?"), ("QST::TWICE", "Can a customer pay twice?")):
        graph.node(spec_id, "OpenQuestion", quote, quote=quote)
        graph.edge(spec_id, "RELATES_TO", "REQ::FR::PLACE")
    for spec_id, quote in (("REQ::NFR::FAST", "Orders are fast."), ("REQ::NFR::SIMPLE", "Orders are simple.")):
        graph.node(spec_id, "QualityRequirement", quote, quote=quote)
        graph.edge(spec_id, "PART_OF", "MOD::ORDERS")

    outcome = await sites(graph)

    questions = [site for site in outcome.sites if "QST::CANCEL" in site.about]
    assert len(questions) == 1 and "QST::TWICE" in questions[0].about
    vague = [site for site in outcome.sites if "REQ::NFR::FAST" in site.about]
    assert len(vague) == 1 and "REQ::NFR::SIMPLE" in vague[0].about


async def test_fr_gdet_08_reach_through_plan_tasks_ranks_a_site_higher() -> None:
    graph = MiniGraph()
    graph.section("Interfaces", "The Orders API.", "The Stock API.")
    graph.node("IFC::ORDERS", "Interface", "Orders API", quote="The Orders API.")
    graph.node("IFC::STOCK", "Interface", "Stock API", quote="The Stock API.")
    plan = MemoryPlanView(
        {
            "IFC::ORDERS": [
                TaskRef(task_id="TASK::A", story_id="STORY::A", status="todo"),
                TaskRef(task_id="TASK::B", story_id="STORY::B", status="todo"),
            ]
        }
    )

    outcome = await sites(graph, plan=plan)

    ranked = [site.about for site in outcome.sites if site.state == "investigate"]
    assert ranked.index(("IFC::ORDERS",)) < ranked.index(("IFC::STOCK",))
    orders = next(site for site in outcome.sites if site.about == ("IFC::ORDERS",))
    assert orders.reach == 1 + 2 * 2
    assert orders.rank_score == 2 * 2 * orders.reach


async def test_fr_gdet_09_the_same_question_in_two_documents_forms_one_site() -> None:
    graph = MiniGraph()
    graph.section("Orders", "Can a customer cancel a paid order?")
    graph.node("QST::CANCEL_A", "OpenQuestion", "Cancel?", quote="Can a customer cancel a paid order?")
    graph.section("Payments", "Can a customer cancel a paid order ?", document="payments.md")
    graph.node("QST::CANCEL_B", "OpenQuestion", "Cancel?", quote="Can a customer cancel a paid order ?")

    outcome = await sites(graph)

    assert [site.about for site in outcome.sites] == [("QST::CANCEL_A", "QST::CANCEL_B")]


async def test_a_seed_with_an_existing_gaps_signature_joins_its_site() -> None:
    graph = account()
    first = await sites(graph)
    seed = next(seed for site in first.sites for seed in site.seeds if "REQ::FR::EXPORT" in seed.about)

    outcome = await sites(graph, existing=[ExistingGap("GAP::1", seed.signature, "open", seed.about)])

    site = next(site for site in outcome.sites if "REQ::FR::EXPORT" in site.about)
    assert site.gap_id == "GAP::1"
    other = next(site for site in first.sites if "REQ::FR::EXPORT" in site.about)
    assert site.input_hash != other.input_hash


async def test_a_cached_verdict_skips_the_site_and_limits_defer_the_rest() -> None:
    graph = MiniGraph()
    graph.section("Interfaces", "A.", "B.", "C.")
    for name in "ABC":
        graph.node(f"IFC::{name}", "Interface", name, quote=f"{name}.")
    records = MemoryGapRecords()
    first = await sites(graph, records=records)
    cached = next(site for site in first.sites if site.about == ("IFC::A",))
    await records.put_verdict(
        PROJECT, VerdictRow(input_hash=cached.input_hash, report={}, model="m", prompt_version="1", created_at=AT)
    )
    limits = GapLimits.model_validate({**rules().limits.model_dump(), "sites_update_run": 1})

    second = await sites(graph, records=records, mode="update", limits=limits, run_id="run-2")

    states = {site.about[0]: site.state for site in second.sites}
    assert states["IFC::A"] == "skipped"
    assert sorted(states.values()) == ["deferred", "investigate", "skipped"]
    written = {row.site_id: row.outcome for row in records.sites[PROJECT] if row.run_id == "run-2"}
    assert sorted(written.values()) == ["deferred", "skipped"]
    deferred = second.deferred[0]

    third = await sites(
        graph, records=records, mode="update", limits=limits, run_id="run-3", deferred_first=[deferred.site_id]
    )

    assert third.investigate[0].site_id == deferred.site_id
    no_cache = await sites(graph, records=records, run_id="run-4", use_cache=False)
    assert len(no_cache.investigate) == 3


async def test_a_site_run_keeps_only_the_named_sites() -> None:
    graph = MiniGraph()
    graph.section("Interfaces", "A.", "B.")
    for name in "AB":
        graph.node(f"IFC::{name}", "Interface", name, quote=f"{name}.")
    first = await sites(graph)
    wanted = first.sites[0].site_id

    outcome = await sites(graph, mode="site", site_ids=[wanted])

    assert [site.site_id for site in outcome.sites] == [wanted]
    assert outcome.investigate[0].site_id == wanted


async def test_a_large_group_splits_into_sites_of_at_most_twelve_seeds() -> None:
    graph = MiniGraph()
    graph.node("MOD::HUB", "Module", "Hub")
    words = [chr(ord("a") + number) * 3 for number in range(MAX_SEEDS)]
    for section in ("alpha", "beta"):
        lines = [f"Item {section} {word} is fast." for word in words]
        graph.section(f"Part {section}", *lines)
        for word, line in zip(words, lines, strict=True):
            spec_id = f"REQ::NFR::ITEM_{section}_{word}".upper()
            graph.node(spec_id, "QualityRequirement", line, quote=line)
            graph.edge(spec_id, "PART_OF", "MOD::HUB")
    snapshot = graph.snapshot()
    seeds = [
        seed
        for seed in detect(snapshot, rules=rules(), relevance=RelevanceOutcome({})).seeds
        if seed.detector == "AM-VAGUE"
    ]
    joined = [replace(seed, about=(*seed.about, "MOD::HUB")) for seed in seeds]

    groups = group_seeds(snapshot, joined)

    assert sorted(len(members) for members, _ in groups) == [MAX_SEEDS, MAX_SEEDS]


async def test_notes_and_seeds_are_written_with_the_run_id() -> None:
    graph = MiniGraph()
    graph.section("Later", "Crypto later.", "Which coins?")
    graph.node("REQ::FR::CRYPTO", "FunctionalRequirement", "Crypto", quote="Crypto later.", in_scope="no")
    graph.node("QST::COINS", "OpenQuestion", "Coins?", quote="Which coins?")
    graph.edge("QST::COINS", "RELATES_TO", "REQ::FR::CRYPTO")
    records = MemoryGapRecords()

    outcome = await sites(graph, records=records, run_id="run-7")

    assert [(note.run_id, note.detector) for note in records.notes[PROJECT]] == [("run-7", "OQ-STATED")]
    assert outcome.counts["notes"] == 1


async def test_nothing_is_written_without_persist() -> None:
    records = MemoryGapRecords()

    outcome = await sites(account(), records=records, persist=False)

    assert outcome.seed_rows
    assert records.seeds[PROJECT] == [] and records.sites[PROJECT] == []


def test_site_ids_and_fingerprints_are_stable() -> None:
    graph = account()
    snapshot = graph.snapshot()
    seeds: list[Seed] = list(detect(snapshot, rules=rules(), relevance=RelevanceOutcome({})).seeds)

    assert site_id(seeds) == site_id(list(reversed(seeds)))
    assert site_id(seeds).startswith("SITE::") and len(site_id(seeds)) == len("SITE::") + 12
    node = snapshot.nodes["REQ::FR::EXPORT"]
    assert node_fingerprint(node) == node_fingerprint(node.model_copy())
    assert node_fingerprint(node) != node_fingerprint(node.model_copy(update={"name": "Other"}))
