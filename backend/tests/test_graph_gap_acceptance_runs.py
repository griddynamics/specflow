"""Acceptance tests of graph.gap: Gap runs (docs/graph/gap/requirements.md).

One test per requirement unit; its docstring starts with the unit's acceptance text. A test stays skipped
(``pending``) until the session named in its skip reason enables it.
"""

from __future__ import annotations

import asyncio

import pytest

from gap_acceptance_support import Planted, RecordedGapGateway, assert_all_recorded
from gap_detection_support import corpus_reader
from gap_host_support import PROJECT, StubGapRun, make_pipeline
from gap_investigation_support import loop_world, loop_world_of, recording_of, site_about
from gap_run_support import corpus_world
from graph.core.errors import InfrastructureUnavailableError
from graph.gap import estimate_gap, run_gap
from graph.gap import run as run_module
from graph.gap.models import GapChangeReport
from test_graph_gap_run import StopAfter, interfaces, mini_world


async def test_fr_grun_01_build_returns_auto_run_after_build_set(tmp_path, caplog) -> None:
    """Given an update build, then the run log shows the build, the gap run and the plan run in that order for the
    project.

    The host's build, gap and plan runners on stub run functions: the plan run is started while the gap run queued by
    the build still runs, and waits for it.
    """

    log: list[str] = []
    run = StubGapRun(block=True, log=log)
    pipeline = make_pipeline(tmp_path, log, run)
    with caplog.at_level("INFO"):
        await pipeline.build()
        await asyncio.wait_for(run.started.wait(), 5)
        await pipeline.plans.start(PROJECT)
        for _ in range(10):  # the plan task gets every chance to run before the gap run ends
            await asyncio.sleep(0)
        before = list(log)
        run.release.set()
        await asyncio.wait_for(pipeline.plans._runs[PROJECT].task, 5)
    await pipeline.aclose()

    assert before == ["build:build-1", "gap:build-1"]
    assert log == ["build:build-1", "gap:build-1", "plan:build-1"]
    order = ("api.graph_build.finished", "api.gap.queued", "api.gap.finished", "api.plan.started")
    seen = [getattr(record, "event", None) for record in caplog.records]
    assert [seen.index(name) for name in order] == sorted(seen.index(name) for name in order)
    assert [context.build.build_id for context in run.contexts] == ["build-1"] and run.modes == ["update"]


async def test_fr_grun_02_mode_update_run_gap_run_detector_over() -> None:
    """Given an update build that changed one fragment, then detectors report seeds for the whole graph and
    investigation runs only on sites touching that fragment's nodes.
    """

    world = await corpus_world()
    first = await run_gap(world.context())
    before = len(world.investigator.sites)
    world.reader = corpus_reader(replace={Planted.VAGUE_REQUIREMENT_LINE: "The admin panel should be fast for staff."})
    change = GapChangeReport(build_id="build-2", changed_evidence=(Planted.VAGUE_REQUIREMENT,))

    result = await run_gap(world.context(changes=[change]))

    assert result.mode == "update" and result.counts["seeds_by_detector"] == first.counts["seeds_by_detector"]
    sites = await world.store.get_artifact(world.project_id, result.run_id, "sites")
    assert sites is not None
    investigated = [item for item in sites["sites"] if item["site_id"] in world.investigator.sites[before:]]
    assert [item["about"] for item in investigated] == [[Planted.VAGUE_REQUIREMENT]]
    assert result.counts["sites"]["skipped"] == first.counts["sites"]["formed"] - 1


async def test_fr_grun_03_site_s_input_hash_equals_cached_verdict() -> None:
    """Given two consecutive update runs with no document change, then the second run makes no model call."""

    world = await corpus_world()
    await run_gap(world.context())
    calls, investigated = len(world.gateway.calls), len(world.investigator.sites)

    second = await run_gap(world.context())

    assert len(world.gateway.calls) == calls and len(world.investigator.sites) == investigated
    assert all(value["calls"] == 0 for value in second.cost.values())
    assert second.counts["sites"]["investigated"] == 0
    assert_all_recorded(world.gateway)


async def test_fr_grun_04_run_fail_because_model_failure_site_recorded() -> None:
    """Given a gateway fake that fails one site, then the run completes, the site is `deferred`, and the next run
    investigates it.
    """

    site_id = await site_about(Planted.STATED_QUESTION, Planted.PLACE_ORDER)
    failing = RecordedGapGateway().record("gap_investigate", recording_of(site_id), error="exhausted")
    world = await loop_world(gateway=failing)

    first = await run_gap(world.context(), mode="full")

    assert first.status == "completed" and first.deferred_sites == (site_id,)
    assert first.counts["sites"]["deferred"] == 1 and first.counts["outcomes"]["deferred"] == 1
    assert not any(Planted.STATED_QUESTION in world.about(gap) for gap in world.gap_nodes())
    world.gateway = RecordedGapGateway()
    loop_world_of(world)

    second = await run_gap(world.context())

    assert second.mode == "update" and world.investigator.sites == [site_id]
    assert second.counts["outcomes"]["gap"] == 1 and second.deferred_sites == ()
    assert any(world.about(gap) == {Planted.STATED_QUESTION, Planted.PLACE_ORDER} for gap in world.gap_nodes())
    assert_all_recorded(world.gateway)


async def test_fr_grun_05_run_respect_limits_rules_file_defer_lowest() -> None:
    """Given 40 eligible sites in an update run, then 30 are investigated and 10 are `deferred`."""

    empty = await mini_world(interfaces(0))
    await run_gap(empty.context())
    world = await mini_world(interfaces(40))
    world.records, world.store = empty.records, empty.store

    result = await run_gap(world.context())

    assert result.mode == "update" and result.counts["sites"]["formed"] == 40
    assert result.counts["sites"]["investigated"] == 30 and len(result.deferred_sites) == 10
    ranks = {row.site_id: row.rank_score for row in world.records.sites[world.project_id]}
    assert all(
        ranks[site] <= min(rank for name, rank in ranks.items() if name not in result.deferred_sites)
        for site in result.deferred_sites
    )


async def test_fr_grun_06_estimate_gap_make_no_model_call_write() -> None:
    """Given a gateway fake that fails every call, then `estimate_gap` returns site and call counts and the stores
    are unchanged.
    """

    world = await corpus_world()
    world.gateway = RecordedGapGateway(fail_roles=("gap_relevance", "gap_investigate", "gap_judge", "gap_edits"))
    nodes, edges = dict(world.executor.nodes), dict(world.executor.edges)

    estimate = await estimate_gap(world.context())

    assert estimate.sites > 0 and estimate.calls["gap_investigate"] > 0 and estimate.calls["gap_relevance"] == 1
    assert world.gateway.calls == []
    assert world.executor.nodes == nodes and world.executor.edges == edges
    assert (
        not world.records.runs[world.project_id]
        and not world.records.seeds[world.project_id]
        and not world.records.sites[world.project_id]
    )
    assert not world.records.relevance_rows[world.project_id] and not world.store.records


async def test_fr_grun_07_run_interrupted_after_step_resume_last_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given a run stopped after `detect`, when resumed, then seeds are not recomputed and the run completes."""

    world = await corpus_world()
    world.store = StopAfter("detect")
    detections: list[int] = []
    real = run_module.detect
    monkeypatch.setattr(run_module, "detect", lambda *args, **kwargs: detections.append(1) or real(*args, **kwargs))
    with pytest.raises(InfrastructureUnavailableError, match="store_stopped"):
        await run_gap(world.context())
    (record,) = world.store.records.values()

    result = await run_gap(world.context(run_id=record.run_id))

    assert detections == [1]
    assert result.status == "completed" and result.run_id == record.run_id
    assert result.counts["gaps_new"]["total"] > 0


async def test_fr_grun_08_run_report_metrics_cost_per_gateway_role() -> None:
    """Given a full run of the shop corpus, then the result names seeds per detector, outcomes per site, calls and
    tokens per role, and the tracking sections.
    """

    world = await corpus_world()

    result = await run_gap(world.context(), mode="full")

    assert result.counts["seeds_by_detector"]["OQ-STATED"]["strong"] >= 1
    assert result.counts["outcomes"]["gap"] == result.counts["gaps_new"]["total"] > 0
    assert set(result.cost) == {"gap_relevance", "gap_investigate", "gap_judge", "gap_edits"}
    assert set(result.counts["model_calls"]) == set(result.counts["tokens"]) == set(result.cost)
    tracked = {(section.lineage, section.heading_path[-1]) for section in result.noise.sections}
    assert {("upload:roadmap", "Release plan"), ("upload:sprint-14", "Sprint scope")} <= tracked
