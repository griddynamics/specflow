"""Acceptance tests of graph.gap: Relevance filter (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from gap_acceptance_support import Planted, RecordedGapGateway, assert_all_recorded, pending
from gap_detection_support import PROJECT, DetectionSetup, run_detection
from gap_port_fakes import MemoryGapRecords
from graph.gap.models import NoiseRuleRow
from graph.gap.names import NoiseScope
from graph.gap.steps.relevance import noise_report

AT = datetime(2026, 11, 1, tzinfo=UTC)
SPEC_DOCUMENTS = ("upload:orders-spec", "upload:payments-spec", "upload:account-spec", "upload:admin-spec")


def noise_rule(scope: NoiseScope, value: str) -> NoiseRuleRow:
    return NoiseRuleRow(rule_id="NR-1", scope=scope, value=value, reason="not spec", created_by="u-1", created_at=AT)


async def test_fr_grel_01_relevance_step_classify_section_spec_tracking_mixed() -> None:
    """Given the shop corpus, then `roadmap.md > Release plan` and `sprint-14.md > Sprint scope` are `tracking` or
    `mixed`, the spec documents' sections are `spec`, and at most one model call per uncertain batch is made.
    """

    found = await run_detection()

    for document, heading in Planted.TRACKING_SECTIONS:
        assert found.relevance_of(document, heading) in ("tracking", "mixed")
    spec_sections = [item for (lineage, _), item in found.relevance.sections.items() if lineage in SPEC_DOCUMENTS]
    assert spec_sections and all(item.relevance == "spec" for item in spec_sections)
    uncertain = sum(1 for item in found.relevance.sections.values() if "obligation" in item.signals)
    assert uncertain == 2
    assert found.gateway is not None and len(found.gateway.calls_of("gap_relevance")) == 1
    assert_all_recorded(found.gateway)


async def test_fr_grel_02_section_relevance_cached_section_text_hash() -> None:
    """Given an unchanged corpus, then the second run makes no `gap_relevance` call."""

    records = MemoryGapRecords()
    await run_detection(setup=DetectionSetup(records=records))
    second = RecordedGapGateway()

    found = await run_detection(setup=DetectionSetup(records=records, gateway=second), run_id="run-2")

    assert second.calls_of("gap_relevance") == []
    assert found.relevance_of("roadmap.md", "Release plan") == "mixed"
    assert_all_recorded(second)


async def test_fr_grel_03_structural_detectors_produce_no_seed_node_whose() -> None:
    """Given the shop corpus, then no seed names `REQ::FR::RELEASE_2_3_HARDENING` or the SHOP-412 item."""

    found = await run_detection()

    assert found.seeds_naming(Planted.RELEASE_HARDENING) == []
    assert found.seeds_naming(Planted.SPRINT_ITEM) == []
    removed = {
        spec_id for item in noise_report(found.relevance, found.detection.removed).sections for spec_id in item.removed
    }
    assert {Planted.RELEASE_HARDENING, Planted.SPRINT_ITEM} <= removed


async def test_fr_grel_04_text_detectors_ignore_relevance_filter() -> None:
    """Given a contradiction whose one side is in a `tracking` section, then CT-VALUE still seeds it."""

    records = MemoryGapRecords()
    records.noise[PROJECT].append(noise_rule("pattern", "Payment review"))

    found = await run_detection(setup=DetectionSetup(records=records))

    assert found.relevance_of("payments-spec.md", "Payment review") == "tracking"
    seeds = [seed for seed in found.detection.seeds if seed.detector == "CT-VALUE"]
    assert [seed.about for seed in seeds] == [(Planted.REVIEW_LIMIT_PAYMENTS, Planted.REVIEW_LIMIT_ORDERS)]
    assert found.site_of(Planted.REVIEW_LIMIT_PAYMENTS) is not None


async def test_fr_grel_05_mixed_section_node_obligation_wording_stay_eligible() -> None:
    """Given the shop corpus, then the "R2 must support refunds through Payments" requirement is a weak seed."""

    found = await run_detection()

    seeds = found.seeds_naming(Planted.RELEASE_PLAN_RULE)
    assert found.relevance_of("roadmap.md", "Release plan") == "mixed"
    assert seeds and all(seed.strength == "weak" and seed.relevance == "mixed" for seed in seeds)
    assert {seed.witness[-1].quote for seed in seeds if seed.detector == "MS-OWNER"} == {Planted.RELEASE_PLAN_RULE_LINE}


async def test_fr_grel_06_noise_rule_added_person_mark_scope_tracking() -> None:
    """Given a noise rule on `roadmap.md > Release plan`, then the next run gives no structural seed from it and the
    noise report says `person`.
    """

    records = MemoryGapRecords()
    records.noise[PROJECT].append(noise_rule("section", "upload:roadmap \u203a Shop roadmap \u203a Release plan"))
    gateway = RecordedGapGateway()

    found = await run_detection(setup=DetectionSetup(records=records, gateway=gateway))

    assert found.seeds_naming(Planted.RELEASE_PLAN_RULE) == []
    report = noise_report(found.relevance, found.detection.removed)
    plan = next(item for item in report.sections if item.heading_path[-1] == "Release plan")
    assert (plan.relevance, plan.by) == ("tracking", "person")
    assert Planted.RELEASE_PLAN_RULE in plan.removed
    assert all("Release plan" not in str(call.messages) for call in gateway.calls_of("gap_relevance"))
    assert_all_recorded(gateway)


@pending("06", "steps.relevance.mark_not_spec turns a not_spec report into mixed sections; investigation calls it")
def test_fr_grel_07_not_spec_report_records_the_site_not_spec() -> None:
    """Given a fake report `not_spec`, then no gap is written and the noise report names the nodes."""
