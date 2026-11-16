"""Gap runs: the run steps, the run store and resume, modes, limits, metrics and cost (docs/graph/gap/api.md "Gap
runs", architecture.md "Run steps", detection.md "Run modes and scope", "Limits", "Run metrics"; FR-GRUN-02 to
FR-GRUN-08)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from gap_acceptance_support import Planted, RecordedGapGateway, assert_all_recorded
from gap_detection_support import PROJECT, MiniGraph
from gap_port_fakes import MemoryPlanView, ScriptedInvestigator
from gap_run_support import CountingInvestigator, GapWorld, corpus_world, executor_for
from graph.core import load_ontology
from graph.core.errors import ConfigurationError, GraphError, InfrastructureUnavailableError
from graph.gap import RUN_STEPS, MemoryGapRunStore, estimate_gap, run_gap
from graph.gap import run as run_module
from graph.gap.models import BuildFacts, GapChangeReport, InvestigationResult, SiteBudget, SitePack, TaskRef
from graph.gap.ports import SiteReads
from graph.gap.rules import load_gap_rules
from graph.gap.run import new_run_id, prompt_versions, relevance_from, seed_from, seed_payload
from graph.gap.run_store import GapRunRecord
from graph.gap.settings import GapSettings
from graph.gap.steps.investigate import SEED_STUB_VERSION, SeedInvestigator


def no_coverage_rules() -> Any:
    """The shipped rules without MS-COVERAGE (project-wide seeds every small graph would get), and without the
    isolated-nodes tracking signal (every node of a small graph is isolated)."""

    loaded = load_gap_rules(ontology=load_ontology())
    return replace(
        loaded,
        detectors={code: rule for code, rule in loaded.detectors.items() if code != "MS-COVERAGE"},
        relevance=replace(loaded.relevance, isolated_share=2.0),
    )


def interfaces(count: int) -> MiniGraph:
    """``count`` interfaces without a provider: one strong MS-PROVIDER site each."""

    graph = MiniGraph()
    lines = [f"Interface number {index} carries events." for index in range(count)]
    graph.section("Interfaces", *lines)
    for index, line in enumerate(lines):
        graph.node(f"IFC::N{index:02d}", "Interface", f"Interface {index}", quote=line)
    return graph


async def mini_world(graph: MiniGraph) -> GapWorld:
    ontology = load_ontology()
    reader = graph.reader()
    return GapWorld(
        reader=reader,
        executor=await executor_for(reader),
        ontology=ontology,
        rules=no_coverage_rules(),
        gateway=RecordedGapGateway(recordings={}),
    )


class FailingInvestigator:
    """An ``Investigator`` that raises a ``GraphError`` (a model failure the run must absorb)."""

    prompt_version = "failing.1"

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult:
        del pack, reads, budget
        raise GraphError("llm_exhausted")


class StopAfter(MemoryGapRunStore):
    """A run store that fails when the step after ``step`` starts (a shutdown between two steps)."""

    def __init__(self, step: str) -> None:
        super().__init__()
        self.stop = RUN_STEPS[RUN_STEPS.index(cast(Any, step)) + 1]
        self.armed = True

    async def put_progress(self, project_id: str, run_id: str, step: str, state: Any) -> None:
        if self.armed and step == self.stop and state == "running":
            self.armed = False
            raise InfrastructureUnavailableError("store_stopped", step=step)
        await super().put_progress(project_id, run_id, step, state)


# Full and update runs


async def test_a_full_run_records_a_gap_per_site_and_the_run_metrics() -> None:
    world = await corpus_world()

    result = await run_gap(world.context(), mode="full")

    assert result.status == "completed" and result.mode == "full"
    sites, outcomes = result.counts["sites"], result.counts["outcomes"]
    assert sites["investigated"] == sites["formed"] > 0 and sites["deferred"] == 0
    gaps = world.gap_nodes()
    assert len(gaps) == outcomes["gap"] == result.counts["gaps_new"]["total"]
    assert all(props["status"] == "open" and props["origin"] == "analysis" for props in gaps.values())
    assert all(props["route"] == "ask" and props["run_id"] == result.run_id for props in gaps.values())
    assert result.counts["routes"] == {"ask": len(gaps), "ai": 0}
    question = next(gap for gap, props in gaps.items() if props["kind"] == "open_question")
    assert world.about(question) == {Planted.STATED_QUESTION, Planted.PLACE_ORDER}
    assert gaps[question]["question"] == Planted.STATED_QUESTION_LINE
    cites = [key for key in world.executor.edge_keys("CITES") if key.startswith(question)]
    assert len(cites) == 1
    cite = world.executor.edge_props[("CITES", cites[0])]
    assert cite["quote"] == Planted.STATED_QUESTION_LINE and cite["line_start"] == cite["line_end"]
    rows = {row.site_id: row for row in world.records.sites[PROJECT]}
    assert {row.gap_id for row in rows.values() if row.outcome == "gap"} == set(gaps)
    assert len(world.records.verdicts[PROJECT]) == sites["investigated"]
    created = [event for event in world.records.event_rows[PROJECT] if event.event == "created"]
    assert {event.gap_id for event in created} == set(gaps)
    (run,) = world.records.runs[PROJECT].values()
    assert run.status == "completed" and run.counts["gaps_new"]["total"] == len(gaps)
    assert run.prompt_versions == prompt_versions(world.context())
    record = await world.store.get_run(PROJECT, result.run_id)
    assert record is not None and record.finished and set(record.progress.values()) == {"done"}
    assert_all_recorded(world.gateway)


async def test_fr_grun_03_a_second_update_run_on_an_unchanged_graph_investigates_nothing() -> None:
    world = await corpus_world()
    first = await run_gap(world.context())
    calls = len(world.gateway.calls)
    investigated = len(world.investigator.sites)

    second = await run_gap(world.context())

    assert first.mode == "full" and second.mode == "update"
    assert second.counts["sites"]["investigated"] == 0
    assert second.counts["sites"]["skipped"] == first.counts["sites"]["formed"]
    assert len(world.gateway.calls) == calls and len(world.investigator.sites) == investigated
    assert second.cost["gap_relevance"] == {"calls": 0, "tokens": 0}
    assert second.counts["gaps_new"]["total"] == 0
    assert len(world.gap_nodes()) == first.counts["gaps_new"]["total"]


async def test_a_reinvestigated_site_rechecks_its_gap_and_never_duplicates_it() -> None:
    world = await corpus_world()
    first = await run_gap(world.context(), mode="full")

    again = await run_gap(world.context(settings=GapSettings(ignore_cache=True)), mode="full")

    assert again.counts["sites"]["investigated"] == first.counts["sites"]["investigated"]
    assert again.counts["gaps_new"]["total"] == 0
    assert again.counts["gaps_updated"]["total"] == first.counts["gaps_new"]["total"]
    assert len(world.gap_nodes()) == first.counts["gaps_new"]["total"]
    rechecked = [event for event in world.records.event_rows[PROJECT] if event.event == "rechecked"]
    assert len(rechecked) == first.counts["gaps_new"]["total"]
    assert all(event.detail["added"] == 0 for event in rechecked)


async def test_fr_grun_08_the_result_names_seeds_outcomes_cost_and_tracking_sections() -> None:
    world = await corpus_world()

    result = await run_gap(world.context(), mode="full")

    counts = result.counts
    assert {
        "sections_by_relevance",
        "seeds_by_detector",
        "detector_failures",
        "sites",
        "outcomes",
        "gaps_new",
        "gaps_updated",
        "routes",
        "notes",
        "model_calls",
        "tokens",
        "noise_nodes",
    } <= set(counts)
    assert counts["seeds_by_detector"]["OQ-STATED"]["strong"] >= 1
    assert set(counts["outcomes"]) == {"gap", "not_a_gap", "not_spec", "duplicate", "deferred"}
    assert set(result.cost) == {"gap_relevance", "gap_investigate", "gap_judge", "gap_edits"}
    assert result.cost["gap_relevance"]["calls"] == counts["model_calls"]["gap_relevance"] == 1
    assert result.cost["gap_relevance"]["tokens"] > 0
    tracked = {section.heading_path[-1] for section in result.noise.sections}
    assert {"Release plan", "Sprint scope"} <= tracked
    assert counts["noise_nodes"] >= 2
    assert counts["notes"] >= 1  # MS-COVERAGE facts name no node: info notes until the investigator picks nodes


async def test_update_without_a_finished_run_is_full_and_build_ids_come_from_the_reports() -> None:
    world = await corpus_world()
    change = GapChangeReport(build_id="build-7")

    result = await run_gap(world.context(changes=[change], build=BuildFacts()))

    assert result.mode == "full" and result.build_id == "build-7"
    (row,) = world.records.runs[PROJECT].values()
    assert row.mode == "full" and row.build_id == "build-7"


# Limits, deferral and failures


async def test_fr_grun_05_an_update_run_investigates_up_to_its_limit_and_defers_the_rest() -> None:
    world = await mini_world(MiniGraph())
    await run_gap(world.context())  # a finished run, so the next one is an update
    big = await mini_world(interfaces(40))
    big.records, big.store = world.records, world.store

    result = await run_gap(big.context())

    assert result.mode == "update"
    assert result.counts["sites"]["investigated"] == 30
    assert len(result.deferred_sites) == 10
    deferred = {row.site_id for row in big.records.sites[PROJECT] if row.outcome == "deferred"}
    assert deferred == set(result.deferred_sites)

    following = await run_gap(big.context())

    assert set(big.investigator.sites[-10:]) == deferred
    assert following.counts["sites"]["investigated"] == 10 and following.deferred_sites == ()


async def test_limits_take_the_projects_overrides() -> None:
    world = await mini_world(interfaces(5))

    result = await run_gap(world.context(settings=GapSettings(limits={"sites_full_run": 2})), mode="full")

    assert result.counts["sites"]["investigated"] == 2 and len(result.deferred_sites) == 3


async def test_a_failed_investigation_defers_the_site_and_a_run_of_failures_is_degraded() -> None:
    world = await mini_world(interfaces(2))
    world.investigator = CountingInvestigator(FailingInvestigator())

    result = await run_gap(world.context(), mode="full")

    assert result.status == "degraded"
    assert result.counts["outcomes"]["deferred"] == 2 and len(result.deferred_sites) == 2
    assert world.gap_nodes() == {} and world.records.verdicts[PROJECT] == {}
    assert "llm_exhausted" in result.warnings
    (row,) = world.records.runs[PROJECT].values()
    assert row.status == "degraded"

    world.investigator = CountingInvestigator()
    healed = await run_gap(world.context())

    assert healed.status == "completed" and healed.counts["sites"]["investigated"] == 2
    assert len(world.gap_nodes()) == 2


async def test_scripted_verdicts_become_site_outcomes() -> None:
    world = await mini_world(interfaces(1))
    await run_gap(world.context(), mode="full")
    site_id = next(iter(world.records.sites[PROJECT])).site_id
    scripted = ScriptedInvestigator({site_id: InvestigationResult(report=None, error="timeout")})
    world.investigator = CountingInvestigator(scripted)

    result = await run_gap(world.context(settings=GapSettings(ignore_cache=True)), mode="site", site_ids=[site_id])

    assert result.counts["outcomes"]["deferred"] == 1 and result.status == "degraded"


# Modes


async def test_a_site_run_skips_scope_and_close_and_investigates_only_the_named_sites() -> None:
    world = await mini_world(interfaces(3))
    await run_gap(world.context(), mode="full")
    gap_id = next(iter(world.gap_nodes()))

    result = await run_gap(world.context(settings=GapSettings(ignore_cache=True)), mode="site", site_ids=[gap_id])

    assert result.mode == "site"
    assert result.counts["sites"]["formed"] == result.counts["sites"]["investigated"] == 1
    assert result.counts["gaps_updated"]["total"] == 1
    scoped = await world.store.get_artifact(PROJECT, result.run_id, "scope")
    closed = await world.store.get_artifact(PROJECT, result.run_id, "close")
    assert scoped == {"skipped": True} and closed is not None and closed["skipped"]


async def test_modes_and_ports_are_checked() -> None:
    world = await mini_world(interfaces(1))
    bad: Any = "partial"

    with pytest.raises(ConfigurationError, match="invalid_gap_run_mode"):
        await run_gap(world.context(), mode=bad)
    with pytest.raises(ConfigurationError, match="gap_site_ids_missing"):
        await run_gap(world.context(), mode="site")
    with pytest.raises(ConfigurationError, match="invalid_gap_run_mode"):
        await estimate_gap(world.context(), mode=bad)
    with pytest.raises(ConfigurationError, match="gap_run_store_missing"):
        await run_gap(replace(world.context(), store=None))
    with pytest.raises(ConfigurationError, match="invalid_gap_settings"):
        await run_gap(world.context(settings=GapSettings(limits={"sites_full_run": 0})))


# The run store and resume


async def test_fr_grun_07_a_run_stopped_after_detect_resumes_without_detecting_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await corpus_world()
    world.store = StopAfter("detect")
    detections: list[int] = []
    real = run_module.detect

    def counting(*args: Any, **kwargs: Any) -> Any:
        detections.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_module, "detect", counting)

    with pytest.raises(InfrastructureUnavailableError, match="store_stopped"):
        await run_gap(world.context(), mode="full")
    (record,) = world.store.records.values()
    assert record.progress["detect"] == "done" and record.progress["sites"] == "pending"
    with pytest.raises(ConfigurationError, match="unfinished_gap_run"):
        await run_gap(world.context(), mode="full")

    result = await run_gap(world.context(run_id=record.run_id), mode="full")

    assert detections == [1]
    assert result.run_id == record.run_id and result.status == "completed"
    assert result.counts["gaps_new"]["total"] == len(world.gap_nodes()) > 0
    again = await run_gap(world.context(run_id=record.run_id))
    assert again == result


async def test_a_failing_step_is_marked_failed_and_resumes() -> None:
    world = await mini_world(interfaces(1))
    world.store = StopAfter("investigate")

    with pytest.raises(InfrastructureUnavailableError):
        await run_gap(world.context(), mode="full")
    (record,) = world.store.records.values()
    assert record.progress["check"] == "pending" and record.progress["investigate"] == "done"
    calls = len(world.investigator.sites)

    result = await run_gap(world.context(run_id=record.run_id))

    assert len(world.investigator.sites) == calls and result.counts["gaps_new"]["total"] == 1


async def test_a_new_run_with_a_given_id_and_progress_callbacks() -> None:
    world = await mini_world(interfaces(1))
    seen: list[tuple[str, int, int]] = []

    result = await run_gap(world.context(run_id="gaprun-given", on_progress=lambda *step: seen.append(step)))

    assert result.run_id == "gaprun-given"
    assert [step for step, _, _ in seen] == list(RUN_STEPS)
    assert seen[-1] == ("record", len(RUN_STEPS), len(RUN_STEPS))


async def test_a_finished_run_without_its_result_is_a_configuration_error() -> None:
    world = await mini_world(interfaces(1))
    result = await run_gap(world.context())
    del world.store.results[(PROJECT, result.run_id)]

    with pytest.raises(ConfigurationError, match="gap_run_result_missing"):
        await run_gap(world.context(run_id=result.run_id))


async def test_a_missing_artifact_fails_the_run_and_a_later_run_starts() -> None:
    world = await mini_world(interfaces(1))
    world.store = StopAfter("detect")
    with pytest.raises(InfrastructureUnavailableError):
        await run_gap(world.context())
    (record,) = world.store.records.values()
    del world.store.artifacts[(PROJECT, record.run_id, "detect")]

    with pytest.raises(ConfigurationError, match="gap_run_artifact_missing"):
        await run_gap(world.context(run_id=record.run_id))

    failed = await world.store.get_result(PROJECT, record.run_id)
    assert failed is not None and failed.status == "failed" and failed.warnings == ("gap_run_artifact_missing",)
    assert world.records.runs[PROJECT][record.run_id].status == "failed"
    later = await run_gap(world.context())
    assert later.status == "completed" and later.run_id != record.run_id


# Estimates


async def test_fr_grun_06_an_estimate_makes_no_call_and_writes_nothing() -> None:
    world = await corpus_world()
    world.gateway = RecordedGapGateway(fail_roles=("gap_relevance", "gap_investigate", "gap_judge", "gap_edits"))

    estimate = await estimate_gap(world.context(), mode="full")

    assert estimate.sites > 0 and estimate.cached == 0
    assert estimate.calls["gap_relevance"] == 1 and estimate.tokens["gap_relevance"] > 0
    assert estimate.calls["gap_investigate"] == estimate.sites * world.rules.limits.rounds_per_site
    assert world.gateway.calls == [] and world.investigator.sites == []
    assert world.records.sites[PROJECT] == [] and world.records.seeds[PROJECT] == []
    assert world.records.relevance_rows[PROJECT] == {} and world.records.runs[PROJECT] == {}
    assert world.store.records == {} and world.gap_nodes() == {}


async def test_an_estimate_after_a_run_counts_the_cached_sites() -> None:
    world = await corpus_world()
    first = await run_gap(world.context())

    estimate = await estimate_gap(world.context())

    assert estimate.sites == 0 and estimate.cached == first.counts["sites"]["formed"]
    assert estimate.calls["gap_relevance"] == 0
    no_store = await estimate_gap(replace(world.context(), store=None), mode="full")
    assert no_store.cached == first.counts["sites"]["formed"]


# Artifacts and helpers


def test_run_ids_sort_by_time_and_artifacts_round_trip() -> None:
    early = new_run_id(datetime(2026, 1, 1, tzinfo=UTC))
    late = new_run_id(datetime(2026, 1, 2, tzinfo=UTC))
    assert early < late and early.startswith("gaprun-")
    record = GapRunRecord(project_id=PROJECT, run_id=early, mode="full", started_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert not record.finished


async def test_seed_and_relevance_artifacts_round_trip() -> None:
    world = await corpus_world()
    result = await run_gap(world.context(), mode="full")
    detected = await world.store.get_artifact(PROJECT, result.run_id, "detect")
    relevance = await world.store.get_artifact(PROJECT, result.run_id, "relevance")
    assert detected is not None and relevance is not None

    seeds = [seed_from(item) for item in detected["seeds"]]
    snapshot = await run_module.read_snapshot(world.reader, PROJECT, world.ontology)
    restored = relevance_from(relevance, snapshot, world.context())

    assert [seed_payload(seed) for seed in seeds] == detected["seeds"]
    assert len(restored.sections) == len(relevance["sections"])
    assert restored.counts() == result.counts["sections_by_relevance"]


def test_the_prompt_versions_name_the_investigator() -> None:
    class Bare:
        async def investigate(self, *_: Any) -> InvestigationResult:
            return InvestigationResult()

    empty: Any = None
    world_ctx = replace(
        GapWorld(reader=empty, executor=empty, ontology=load_ontology(), rules=no_coverage_rules()).context(),
        investigator=Bare(),
    )
    assert prompt_versions(world_ctx)["gap_investigate"] == SEED_STUB_VERSION
    assert SeedInvestigator().prompt_version == SEED_STUB_VERSION


async def test_a_plan_lowers_nothing_and_reach_counts_tasks() -> None:
    plan = MemoryPlanView({Planted.PLACE_ORDER: [TaskRef(task_id="TASK::1", story_id="STORY::1", status="pending")]})
    world = await corpus_world(plan=plan)

    result = await run_gap(world.context(), mode="full")

    assert result.status == "completed" and result.counts["gaps_new"]["total"] > 0
