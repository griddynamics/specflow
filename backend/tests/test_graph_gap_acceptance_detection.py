"""Acceptance tests of graph.gap: Detection and sites (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from gap_acceptance_support import Planted, assert_all_recorded
from gap_detection_support import PROJECT, DetectionSetup, MiniGraph, corpus_reader, fragment_of, run_detection
from gap_port_fakes import MemoryGapRecords, MemoryPlanView
from graph.gap import run_gap
from graph.gap.models import SuppressionRow, TaskRef
from graph.gap.steps.detect import RULES, facts_of, witness_holds
from test_graph_gap_run import mini_world

AT = datetime(2026, 11, 1, tzinfo=UTC)


async def test_fr_gdet_01_seed_carry_spec_ids_witness_quoted_line() -> None:
    """Given a full run, then every `gap_seed` row has a witness that the check of its site resolves."""

    found = await run_detection()

    rows = found.records.seeds[PROJECT]
    facts = facts_of(found.detection.seeds)
    assert rows and len(rows) == len(found.detection.seeds)
    for row in rows:
        assert row.witness and row.strength in ("strong", "weak")
        assert all(witness_holds(found.snapshot, item, facts) for item in row.witness), row.detector
        assert row.spec_ids or row.detector == "MS-COVERAGE"
    assert found.gateway is not None
    assert_all_recorded(found.gateway)


async def test_fr_gdet_02_oq_stated_seed_openquestion_node_still_graph() -> None:
    """Given the shop corpus, then a seed names `QST::CANCEL_PAID_ORDER` and `REQ::FR::PLACE_ORDER`."""

    found = await run_detection()

    seeds = [seed for seed in found.seeds_naming(Planted.STATED_QUESTION) if seed.detector == "OQ-STATED"]
    assert [seed.about for seed in seeds] == [(Planted.STATED_QUESTION, Planted.PLACE_ORDER)]
    assert [item.quote for item in seeds[0].witness] == [Planted.STATED_QUESTION_LINE]
    assert seeds[0].strength == "strong"


async def test_fr_gdet_03_ct_value_seed_near_duplicate_pair_build() -> None:
    """Given the shop corpus, then one seed names both review-limit rules with both lines as witness."""

    found = await run_detection()

    seeds = [seed for seed in found.detection.seeds if seed.detector == "CT-VALUE"]
    assert len(seeds) == 1
    assert set(seeds[0].about) == {Planted.REVIEW_LIMIT_ORDERS, Planted.REVIEW_LIMIT_PAYMENTS}
    assert {item.quote for item in seeds[0].witness} == {
        Planted.REVIEW_LIMIT_ORDERS_LINE,
        Planted.REVIEW_LIMIT_PAYMENTS_LINE,
    }


async def test_fr_gdet_04_ms_provider_ms_store_flag_seed_graph() -> None:
    """Given the shop corpus, then the interface without a provider and the assumption are seeded."""

    found = await run_detection()

    def codes(spec_id: str) -> set[str]:
        return {seed.detector for seed in found.seeds_naming(spec_id)}

    assert "MS-PROVIDER" in codes(Planted.INTERFACE_WITHOUT_PROVIDER)
    assert "MS-STORE" in codes(Planted.ENTITY_WITHOUT_STORE)
    assert "AS-FLAG" in codes(Planted.ASSUMPTION)
    flag = next(seed for seed in found.seeds_naming(Planted.ASSUMPTION) if seed.detector == "AS-FLAG")
    assert flag.witness[0].quote == Planted.ASSUMPTION_LINE


async def test_fr_gdet_05_am_vague_seed_requirement_whose_text_holds() -> None:
    """Given the shop corpus, then "The admin panel should be fast." is seeded."""

    found = await run_detection()

    seeds = [seed for seed in found.seeds_naming(Planted.VAGUE_REQUIREMENT) if seed.detector == "AM-VAGUE"]
    assert [item.quote for seed in seeds for item in seed.witness] == [Planted.VAGUE_REQUIREMENT_LINE]


async def test_fr_gdet_06_weak_seed_become_part_site_corroboration_signal() -> None:
    """Given an ownerless node without obligation wording, a named system part or a planned dependency, then it is
    not in any site; given the CSV export requirement, then it is.
    """

    found = await run_detection()

    assert found.seeds_naming(Planted.OWNERLESS_UNCORROBORATED)
    assert found.site_of(Planted.OWNERLESS_UNCORROBORATED) is None
    rows = [row for row in found.records.seeds[PROJECT] if Planted.OWNERLESS_UNCORROBORATED in row.spec_ids]
    assert rows and not any(row.corroborated for row in rows)
    csv = found.site_of(Planted.OWNERLESS_CSV_EXPORT)
    assert csv is not None and "MS-OWNER" in {seed.detector for seed in csv.seeds}


async def test_fr_gdet_07_seed_whose_signature_matches_suppression_dropped() -> None:
    """Given a dismissed gap, when its nodes and witness are unchanged, then no seed reaches a site; when its witness
    line changes, then it does.
    """

    records = MemoryGapRecords()
    first = await run_detection(setup=DetectionSetup(records=records))
    site = first.site_of(Planted.ASSUMPTION)
    for seed in site.seeds:
        await records.suppress(
            PROJECT,
            SuppressionRow(
                signature=seed.signature, gap_id="GAP::1", question="q", reason="no", dismissed_by="u", dismissed_at=AT
            ),
        )

    unchanged = await run_detection(setup=DetectionSetup(records=records), run_id="run-2")
    changed_line = "We assume every customer has one email address."
    reader = corpus_reader(replace={Planted.ASSUMPTION_LINE: changed_line})
    changed = await run_detection(reader, DetectionSetup(records=records), run_id="run-3")

    assert unchanged.site_of(Planted.ASSUMPTION) is None
    assert unchanged.sites.counts["seeds_by_detector"]["AS-FLAG"]["suppressed"] == 1
    again = changed.site_of(Planted.ASSUMPTION)
    assert again is not None and again.seeds[0].witness[0].quote == changed_line


async def test_fr_gdet_08_seeds_sharing_about_node_owner_merge_into() -> None:
    """Given two stated questions about the same requirement, then they form one site; given a site reaching two
    stories and one reaching none, then the first ranks higher.
    """

    place = fragment_of("orders-spec.md", "Place order")
    line = "Can a customer change a paid order?"
    second_question: dict[str, Any] = {
        "spec_id": "QST::CHANGE_PAID_ORDER",
        "uuid": "gap-node-9001",
        "label": "OpenQuestion",
        "name": line,
        "provenance": [
            {
                "document_id": "upload:orders-spec",
                "path": "orders-spec.md",
                "fragment_id": place["fragment_id"],
                "line_start": place["line_end"] + 1,
                "line_end": place["line_end"] + 1,
                "quote": line,
            }
        ],
    }
    edge = {
        "source": "QST::CHANGE_PAID_ORDER",
        "relation": "RELATES_TO",
        "target": Planted.PLACE_ORDER,
        "basis": "stated",
    }
    reader = corpus_reader(append={("orders-spec.md", "Place order"): line}, nodes=[second_question], edges=[edge])
    plan = MemoryPlanView(
        {
            Planted.PLACE_ORDER: [
                TaskRef(task_id="TASK::ORDERS::PLACE", story_id="STORY::ORDERS::PLACE_ORDER", status="todo"),
                TaskRef(task_id="TASK::ORDERS::CANCEL", story_id="STORY::ORDERS::CANCEL", status="todo"),
            ]
        }
    )

    without_plan = await run_detection(reader)
    with_plan = await run_detection(reader, DetectionSetup(plan=plan))

    site = with_plan.site_of(Planted.STATED_QUESTION)
    assert {"QST::CANCEL_PAID_ORDER", "QST::CHANGE_PAID_ORDER", Planted.PLACE_ORDER} <= set(site.about)
    assert [seed.detector for seed in site.seeds] == ["OQ-STATED", "OQ-STATED"]
    provider = with_plan.site_of(Planted.INTERFACE_WITHOUT_PROVIDER)
    assert provider.reach == without_plan.site_of(Planted.INTERFACE_WITHOUT_PROVIDER).reach
    assert site.reach == without_plan.site_of(Planted.STATED_QUESTION).reach + 2 * 2
    assert without_plan.site_of(Planted.STATED_QUESTION).rank_score < provider.rank_score < site.rank_score
    order = [item.site_id for item in with_plan.sites.investigate]
    assert order.index(site.site_id) < order.index(provider.site_id)


async def test_fr_gdet_09_two_openquestion_nodes_asking_question_two_documents() -> None:
    """Given the same question in two documents, then one `Gap` has `ABOUT` to both question nodes."""

    graph = MiniGraph()
    graph.section("Orders", "Can a customer cancel a paid order?")
    graph.node("QST::CANCEL_A", "OpenQuestion", "Cancel?", quote="Can a customer cancel a paid order?")
    graph.section("Payments", "Can a customer cancel a paid order ?", document="payments.md")
    graph.node("QST::CANCEL_B", "OpenQuestion", "Cancel?", quote="Can a customer cancel a paid order ?")
    world = await mini_world(graph)

    await run_gap(world.context(), mode="full")

    (gap_id,) = world.gap_nodes()
    assert world.about(gap_id) == {"QST::CANCEL_A", "QST::CANCEL_B"}


async def test_fr_gdet_10_failing_detector_stop_run_reported_run_metrics() -> None:
    """Given a detector that raises, then the other detectors' seeds are recorded and `detector_failures` names it."""

    def broken(*_: object) -> list[Any]:
        raise ValueError("query failed")

    found = await run_detection(setup=DetectionSetup(registry={**RULES, "refused_merge": broken}))

    assert found.sites.counts["detector_failures"] == {"CT-VALUE": "ValueError"}
    detectors = {row.detector for row in found.records.seeds[PROJECT]}
    assert "CT-VALUE" not in detectors
    assert {"OQ-STATED", "MS-PROVIDER", "AS-FLAG", "AM-VAGUE"} <= detectors
