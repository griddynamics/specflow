"""Tier 1: the API's gap wiring: the in-process gap runner (``api.gap.runner``) and the plan view adapter.

The runner is checked on a stub ``run_gap``, a stub gateway, a memory run store and a real file build store under
``tmp_path``. No graph store, no database, no model.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from api.gap.plan_view import PlanReaderView
from api.gap.runner import RunnerClosedError
from api.plan.state import NoGraphBuildError
from gap_host_support import (
    NOISE,
    OTHER,
    PROJECT,
    StubGapRun,
    StubGateway,
    drained,
    make_gap_runner,
    make_pipeline,
    store_build,
)
from graph.build.models import ChangedNode, ChangeReport, DedupRefusal, QuarantinedEdge, Settlement
from graph.core import ConfigurationError
from graph.core.reader import GraphNode
from graph.gap import GapRunResult, GapSettings, TaskRef
from graph.gap.run_store import GapRunRecord, MemoryGapRunStore
from logger_harness import events, fields

REPORT = ChangeReport(nodes=(ChangedNode(spec_id="REQ-1", label="Requirement", classes=("changed_meaning",)),))


# One run at a time, polled status (FR-GWEB-01) -------------------------------------------------------------------


async def test_two_builds_in_quick_succession_run_one_gap_run_after_the_other(tmp_path, caplog):
    await store_build(tmp_path, "build-1", minutes=0)
    await store_build(tmp_path, "build-2", minutes=5, report=REPORT)
    run = StubGapRun(block=True)
    runner = make_gap_runner(tmp_path, run)

    with caplog.at_level("INFO"):
        await runner.after_build(PROJECT, "build-1")
        await asyncio.wait_for(run.started.wait(), 5)
        await runner.after_build(PROJECT, "build-2")
        running = await runner.status(PROJECT)
        run.release.set()
        await drained(runner)
    done = await runner.status(PROJECT)

    assert run.max_active == 1
    assert [context.build.build_id for context in run.contexts] == ["build-1", "build-2"]
    assert run.modes == ["update", "update"]
    assert run.contexts[0].changes == ()  # the project's first gap run
    [report] = run.contexts[1].changes  # the builds after the first gap run's build
    assert (report.build_id, report.changed_meaning) == ("build-2", ("REQ-1",))
    assert (running.status, running.build_id, running.queued) == ("running", "build-1", 1)
    assert (running.step, running.steps_done, running.step_items_done, running.step_items_total) == ("close", 1, 2, 5)
    assert (done.status, done.build_id, done.queued, done.counts) == ("completed", "build-2", 0, {"gaps_new": 1})
    assert done.result is not None and done.result.noise == NOISE
    assert [fields(event)["build_id"] for event in events(caplog, "api.gap.finished")] == ["build-1", "build-2"]
    assert all(gateway.closed for gateway in StubGateway.instances)


async def test_a_waiting_update_is_replaced_by_the_newer_build(tmp_path):
    for index in range(3):
        await store_build(tmp_path, f"build-{index}", minutes=index * 5)
    run = StubGapRun(block=True)
    runner = make_gap_runner(tmp_path, run)

    await runner.after_build(PROJECT, "build-0")
    await asyncio.wait_for(run.started.wait(), 5)
    await runner.after_build(PROJECT, "build-1")
    await runner.after_build(PROJECT, "build-2")
    queued = await runner.status(PROJECT)
    run.release.set()
    await drained(runner)

    assert queued.queued == 1
    assert [context.build.build_id for context in run.contexts] == ["build-0", "build-2"]


async def test_projects_have_their_own_queues(tmp_path):
    await store_build(tmp_path, "build-a", minutes=0)
    await store_build(tmp_path, "build-b", minutes=0, project_id=OTHER)
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run)

    await runner.after_build(PROJECT, "build-a")
    await runner.after_build(OTHER, "build-b")
    await drained(runner, PROJECT)
    await drained(runner, OTHER)

    assert sorted((context.project_id, context.build.build_id) for context in run.contexts) == [
        (PROJECT, "build-a"),
        (OTHER, "build-b"),
    ]
    assert (await runner.status(OTHER)).build_id == "build-b"


async def test_status_is_idle_before_the_first_run_and_wait_idle_returns_at_once(tmp_path):
    runner = make_gap_runner(tmp_path)

    await asyncio.wait_for(runner.wait_idle(PROJECT), 1)

    assert (await runner.status(PROJECT)).status == "idle"


# Context -------------------------------------------------------------------------------------------------------------


async def test_the_context_carries_the_build_facts_ports_and_settings(tmp_path):
    await store_build(
        tmp_path,
        "build-1",
        minutes=0,
        dedup_refused=(
            DedupRefusal(
                first_spec_id="REQ-1", second_spec_id="REQ-2", label="Requirement", guard="numbers", name_cosine=0.9
            ),
        ),
        quarantine=(
            QuarantinedEdge(
                reason="cycle_broken",
                relation="DEPENDS_ON",
                source_label="A",
                target_label="B",
                basis="stated",
                source_spec_id="A-1",
                target_spec_id="B-1",
                fragment_ids=("f1",),
            ),
        ),
    )
    gap = GapSettings(ai_answers=False)
    ensured: list = []
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run, gap=gap, ensured=ensured)

    await runner.after_build(PROJECT, "build-1")
    await drained(runner)

    [context] = run.contexts
    assert (context.records, context.amendments, context.investigator) == ("records", "amendments", "investigator")
    assert (context.executor, context.reader, context.plan, context.settings) == ("executor", "reader", "plan", gap)
    assert [(pair.first_spec_id, pair.second_spec_id) for pair in context.build.refused_pairs] == [("REQ-1", "REQ-2")]
    assert [(edge.source_spec_id, edge.target_spec_id) for edge in context.build.cut_edges] == [("A-1", "B-1")]
    assert len(ensured) == 1


# Skips and failures ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gap", "build", "reason"),
    [
        pytest.param(GapSettings(auto_run_after_build=False), {}, "auto_run_off", id="auto-run-off"),
        pytest.param(GapSettings(enabled=False), {}, "disabled", id="disabled"),
        pytest.param(GapSettings(), {"status": "failed"}, "build_failed", id="failed-build"),
        pytest.param(
            GapSettings(),
            {"settlement": Settlement(state="not_settled", reasons=("documents_incomplete",))},
            "documents_incomplete",
            id="documents-incomplete",
        ),
    ],
)
async def test_a_build_that_needs_no_gap_run_queues_none(tmp_path, caplog, gap, build, reason):
    await store_build(tmp_path, "build-1", minutes=0, **build)
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run, gap=gap)

    with caplog.at_level("INFO"):
        await runner.after_build(PROJECT, "build-1")
        await drained(runner)

    assert run.contexts == []
    [skipped] = events(caplog, "api.gap.skipped")
    assert fields(skipped)["reason"] == reason
    assert (await runner.status(PROJECT)).status == "idle"


async def test_a_build_not_settled_for_another_reason_still_queues_its_run(tmp_path):
    await store_build(
        tmp_path, "build-1", minutes=0, settlement=Settlement(state="not_settled", reasons=("inference_backlog",))
    )
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run)

    await runner.after_build(PROJECT, "build-1")
    await drained(runner)

    assert len(run.contexts) == 1


async def test_a_failed_gap_run_is_logged_reported_and_never_raised(tmp_path, caplog):
    await store_build(tmp_path, "build-1", minutes=0)
    run = StubGapRun(fail=ConfigurationError("invalid_gap_settings", variable="GAP_INVESTIGATOR"))
    runner = make_gap_runner(tmp_path, run)

    with caplog.at_level("INFO"):
        await runner.after_build(PROJECT, "build-1")
        await drained(runner)
    state = await runner.status(PROJECT)

    assert (state.status, state.error_code) == ("failed", "invalid_gap_settings")
    assert state.run_id is not None  # the run's record stays unfinished: the next run resumes it
    [failed] = events(caplog, "api.gap.failed")
    assert fields(failed)["error_code"] == "invalid_gap_settings"
    assert fields(failed)["error_fields"] == {"variable": "GAP_INVESTIGATOR"}


async def test_a_failing_hook_never_fails_the_build(tmp_path, caplog):
    class BrokenBuilds:
        async def get_result(self, _project_id, _build_id):
            raise OSError("disk")

    runner = make_gap_runner(tmp_path, builds=BrokenBuilds())

    with caplog.at_level("INFO"):
        await runner.after_build(PROJECT, "build-1")

    [failed] = events(caplog, "api.gap.after_build_failed")
    assert (fields(failed)["error_code"], fields(failed)["error_type"]) == ("unexpected_error", "OSError")


# Resume and change reports ---------------------------------------------------------------------------------------


async def test_an_unfinished_run_is_resumed_first_with_its_own_mode_and_build(tmp_path):
    await store_build(tmp_path, "build-1", minutes=0)
    await store_build(tmp_path, "build-2", minutes=5, report=REPORT)
    runs = MemoryGapRunStore()
    await runs.create_run(
        GapRunRecord(
            project_id=PROJECT,
            run_id="run-old",
            mode="full",
            build_id="build-1",
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
            progress={"scope": "done", "close": "running"},
        )
    )
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run, runs=runs)

    interrupted = await runner.status(PROJECT)
    await runner.after_build(PROJECT, "build-2")
    await drained(runner)

    assert (interrupted.status, interrupted.error_code, interrupted.step) == ("failed", "interrupted", "close")
    assert [(c.run_id, c.build.build_id) for c in run.contexts] == [("run-old", "build-1"), (None, "build-2")]
    assert run.modes == ["full", "update"]
    assert [report.build_id for report in run.contexts[1].changes] == ["build-2"]
    assert (await runs.get_run(PROJECT, "run-old")).finished


async def test_resuming_the_run_of_the_same_build_runs_once(tmp_path):
    await store_build(tmp_path, "build-1", minutes=0)
    runs = MemoryGapRunStore()
    await runs.create_run(
        GapRunRecord(
            project_id=PROJECT,
            run_id="run-old",
            mode="update",
            build_id="build-1",
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
    )
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run, runs=runs)

    await runner.after_build(PROJECT, "build-1")
    await drained(runner)

    assert [context.run_id for context in run.contexts] == ["run-old"]


async def test_a_broken_chain_gives_no_change_reports_and_says_why(tmp_path, caplog):
    await store_build(tmp_path, "build-2", minutes=5)
    runs = MemoryGapRunStore()
    await finished_run(runs, "run-1", "build-gone")
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run, runs=runs)

    with caplog.at_level("INFO"):
        await runner.after_build(PROJECT, "build-2")
        await drained(runner)

    assert run.contexts[0].changes == ()
    [missing] = events(caplog, "api.gap.change_reports_missing")
    logged = fields(missing)
    assert {name: logged[name] for name in ("build_id", "last_build_id", "reason", "missing_build_id")} == {
        "build_id": "build-2",
        "last_build_id": "build-gone",
        "reason": "since_build_unknown",
        "missing_build_id": "build-gone",
    }


async def test_after_a_failed_gap_run_the_next_run_gets_no_change_reports(tmp_path, caplog):
    await store_build(tmp_path, "build-1", minutes=0)
    await store_build(tmp_path, "build-2", minutes=5, report=REPORT)
    runs = MemoryGapRunStore()
    await finished_run(runs, "run-1", "build-1", status="failed")
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run, runs=runs)

    with caplog.at_level("INFO"):
        await runner.after_build(PROJECT, "build-2")
        await drained(runner)

    assert run.contexts[0].changes == ()
    [missing] = events(caplog, "api.gap.change_reports_missing")
    assert fields(missing)["reason"] == "last_gap_run_failed"


async def finished_run(runs: MemoryGapRunStore, run_id: str, build_id: str, *, status: str = "completed") -> None:
    await runs.create_run(
        GapRunRecord(project_id=PROJECT, run_id=run_id, mode="update", build_id=build_id, started_at=datetime.now(UTC))
    )
    await runs.finish_run(PROJECT, GapRunResult(run_id=run_id, mode="update", build_id=build_id, status=status))


# Full runs, noise reports, closing ---------------------------------------------------------------------------------


async def test_a_full_run_needs_a_finished_build(tmp_path):
    runner = make_gap_runner(tmp_path)

    with pytest.raises(NoGraphBuildError):
        await runner.start_full(PROJECT)


async def test_a_full_run_runs_on_the_latest_build_and_a_second_start_joins_it(tmp_path):
    await store_build(tmp_path, "build-1", minutes=0)
    await store_build(tmp_path, "build-2", minutes=5)
    run = StubGapRun(block=True)
    runner = make_gap_runner(tmp_path, run)

    first = await runner.start_full(PROJECT)
    await asyncio.wait_for(run.started.wait(), 5)
    second = await runner.start_full(PROJECT)
    run.release.set()
    await drained(runner)

    assert first.status in {"queued", "running"} and second.status == "running"
    assert run.modes == ["full"]
    assert run.contexts[0].build.build_id == "build-2"


async def test_the_noise_report_of_a_finished_run(tmp_path):
    await store_build(tmp_path, "build-1", minutes=0)
    runner = make_gap_runner(tmp_path)
    await runner.after_build(PROJECT, "build-1")
    await drained(runner)
    run_id = (await runner.status(PROJECT)).run_id
    assert run_id is not None

    assert await runner.noise(PROJECT, run_id) == NOISE
    assert await runner.noise(OTHER, run_id) is None
    assert await runner.noise(PROJECT, "unknown") is None
    assert await runner.noise(PROJECT, "../escape") is None


async def test_closing_cancels_the_running_run_and_refuses_new_ones(tmp_path):
    await store_build(tmp_path, "build-1", minutes=0)
    run = StubGapRun(block=True)
    runner = make_gap_runner(tmp_path, run)
    await runner.after_build(PROJECT, "build-1")
    await asyncio.wait_for(run.started.wait(), 5)

    await runner.aclose()
    await runner.after_build(PROJECT, "build-1")

    with pytest.raises(RunnerClosedError):
        await runner.start_full(PROJECT)
    assert len(run.contexts) == 1
    assert (await runner.status(PROJECT)).error_code == "interrupted"


async def test_the_service_is_built_once_from_the_runner_resources(tmp_path):
    gap = GapSettings(ai_answers=False)
    runner = make_gap_runner(tmp_path, gap=gap)

    first = await runner.service()
    again = await runner.service()

    args, kwargs = first
    assert first is again
    assert args[0] == "executor" and args[1] == "reader" and args[4:] == ("records", "amendments")
    assert kwargs == {"settings": gap, "plan": "plan"}


# Plan view -------------------------------------------------------------------------------------------------------


class Task:
    def __init__(self, task_id: str, story_id: str, status: str, *implements: str) -> None:
        self.task_id, self.story_id, self.status, self.implements = task_id, story_id, status, implements


class TaskReader:
    async def tasks(self, _project_id):
        return [
            Task("T-1", "S-1", "pending", "REQ-1"),
            Task("T-2", "S-2", "completed", "REQ-1", "REQ-2"),
            Task("T-3", "S-2", "pending", "REQ-OLD"),
        ]


class NodeReader:
    async def nodes(self, _project_id):
        return [
            GraphNode(spec_id="REQ-1", uuid="1", label="Requirement", name="One", spec_id_aliases=("REQ-OLD",)),
            GraphNode(spec_id="REQ-2", uuid="2", label="Requirement", name="Two"),
        ]


async def test_the_plan_view_lists_tasks_and_stories_per_spec_id_through_aliases():
    view = PlanReaderView(TaskReader(), NodeReader())

    tasks = await view.tasks_for(PROJECT, ["REQ-OLD", "REQ-2", "REQ-9"])
    stories = await view.stories_for(PROJECT, ["REQ-1"])

    assert [ref.task_id for ref in tasks["REQ-OLD"]] == ["T-1", "T-2", "T-3"]
    assert tasks["REQ-2"] == (TaskRef(task_id="T-2", story_id="S-2", status="completed"),)
    assert tasks["REQ-9"] == ()
    assert stories == {"REQ-1": ("S-1", "S-2")}
    assert await view.tasks_for(PROJECT, []) == {}


# Plan link -------------------------------------------------------------------------------------------------------


async def test_a_failed_gap_run_never_stops_the_plan_run(tmp_path, caplog):
    log: list[str] = []
    run = StubGapRun(fail=ConfigurationError("unfinished_gap_run", run_id="r"), log=log)
    pipeline = make_pipeline(tmp_path, log, run)

    with caplog.at_level("INFO"):
        await pipeline.build()
        await pipeline.plans.start(PROJECT)
        await asyncio.wait_for(pipeline.plans._runs[PROJECT].task, 5)
    await pipeline.aclose()

    assert log == ["build:build-1", "gap:build-1", "plan:build-1"]
    assert len(events(caplog, "api.gap.failed")) == 1
    assert events(caplog, "api.plan.gap_wait_failed") == []


async def test_a_plan_run_without_a_gap_run_queued_starts_at_once(tmp_path):
    log: list[str] = []
    pipeline = make_pipeline(tmp_path, log, StubGapRun(log=log))
    await pipeline.build()
    await drained(pipeline.gaps)

    await pipeline.plans.start(PROJECT)
    await asyncio.wait_for(pipeline.plans._runs[PROJECT].task, 5)
    await pipeline.aclose()

    assert log == ["build:build-1", "gap:build-1", "plan:build-1"]
