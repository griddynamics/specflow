"""Tier 1: the API's plan wiring: the in-process plan runner.

The runner is checked on a stub ``run_plan`` and ``estimate_plan``, a stub gateway, stub graph reads and real
file stores under ``tmp_path``. No graph store, no model.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from api.plan.runner import GraphPlanRunner, PlanReads, RunnerDependencies
from api.plan.state import NoGraphBuildError
from graph.build import BuildOptions, BuildRecord, BuildResult, open_file_stores
from graph.build.models import ChangedEdge, ChangedNode, ChangeReport
from graph.core import ConfigurationError
from graph.core.reader import GraphNode
from graph.plan import STEPS, PlanEstimate, PlanResult, PlanStepEstimate, PlanTask, open_plan_store
from graph.plan.models import (
    DoneWhen,
    PlanInfo,
    PlanMetrics,
    PlannerCounts,
    PlanningSummary,
    PlanOptions,
    PlanRunRecord,
    WorkAreaInfo,
)
from graph_plan_support import plan_ontology, plan_rules
from logger_harness import events, fields
from settings import GraphBuildSettings

T0 = datetime(2026, 11, 1, tzinfo=UTC)
PROJECT = str(uuid.UUID(int=1))

SETTINGS = GraphBuildSettings(
    LLM_GATEWAY_URL="https://gateway.example.test/openai",
    LLM_GATEWAY_API_KEY="gateway-key-3c6e",
    GRAPH_LLM_MODEL="main/model",
    GRAPH_SMALL_MODEL="small/model",
    GRAPH_EMBEDDING_MODEL="embed/model",
    GRAPH_EMBEDDING_DIM="8",
)

ONTOLOGY = plan_ontology()
RULES = plan_rules(ONTOLOGY)
FEATURE_LABELS = sorted({label for rule in RULES.types.values() if rule.story == "feature" for label in rule.labels})
OTHER_LABEL = next(
    label for rule in RULES.types.values() if rule.role == "member" and rule.story is None for label in rule.labels
)


class StubStore:
    """A graph store the plan context accepts; nothing reads it (the reads are stubs)."""

    def executor(self) -> Any:
        return object()

    async def ensure_schema(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class StubUsage:
    calls = 0


class StubGateway:
    instances: ClassVar[list[StubGateway]] = []

    def __init__(self, _llm: Any) -> None:
        self.closed = False
        StubGateway.instances.append(self)

    def usage(self):
        class Snapshot:
            def total(self):
                return StubUsage()

        return Snapshot()

    async def aclose(self) -> None:
        self.closed = True


class StubPlanReader:
    def __init__(self) -> None:
        self.info: PlanInfo | None = None
        self.tasks_list: list[PlanTask] = []
        self.areas_list: list[WorkAreaInfo] = []

    async def plan(self, _project_id: str) -> PlanInfo | None:
        return self.info

    async def areas(self, _project_id: str) -> list[Any]:
        return list(self.areas_list)

    async def stories(self, _project_id: str) -> list[Any]:
        return []

    async def tasks(self, _project_id: str) -> list[PlanTask]:
        return list(self.tasks_list)


class StubGraphReader:
    def __init__(self, nodes: list[GraphNode]) -> None:
        self.nodes_list = nodes

    async def nodes(self, _project_id: str, labels: list[str] | None = None) -> list[GraphNode]:
        return [node for node in self.nodes_list if labels is None or node.label in labels]


class StubPlan:
    """Stands in for ``run_plan``: stores a run record, waits for ``release``, then finishes it."""

    def __init__(self, *, fail: Exception | None = None, block: bool = False, reader: StubPlanReader | None = None):
        self.contexts: list[Any] = []
        self.release = asyncio.Event()
        self.waiting = asyncio.Event()
        if not block:
            self.release.set()
        self.fail = fail
        self.reader = reader

    async def __call__(self, context) -> PlanResult:
        self.contexts.append(context)
        if self.fail is not None:
            raise self.fail
        run_id = context.run_id or uuid.uuid4().hex
        if context.run_id is None:
            await context.store.create_run(_record(run_id, context.build_id, replan=context.replan))
        await context.store.put_progress(context.project_id, run_id, "snapshot", "done")
        await context.store.put_progress(context.project_id, run_id, "partition", "running")
        context.report_progress("snapshot", 1, 1)  # a finished step's count is not shown
        context.report_progress("partition", 3, 7)
        self.waiting.set()
        await self.release.wait()
        result = PlanResult(
            project_id=context.project_id,
            run_id=run_id,
            status="succeeded",
            mode="first" if not context.replan else "update",
            build_id=context.build_id,
            started_at=T0,
            finished_at=datetime.now(UTC),
        )
        await context.store.finish_run(result.model_dump(mode="json"))
        if self.reader is not None:
            self.reader.info = _info(context.build_id)
        return result


def _record(run_id: str, build_id: str, *, replan: bool = False, started_at: datetime | None = None) -> PlanRunRecord:
    return PlanRunRecord(
        project_id=PROJECT,
        run_id=run_id,
        mode="first",
        replan=replan,
        build_id=build_id,
        options=PlanOptions(),
        rules_version=RULES.version,
        plan_ontology_version="2.0",
        started_at=started_at or datetime.now(UTC),
    )


def _info(build_id: str) -> PlanInfo:
    return PlanInfo(plan_id="plan-1", last_build_id=build_id, created_at=T0, updated_at=T0)


def _node(spec_id: str, label: str, name: str, aliases: tuple[str, ...] = ()) -> GraphNode:
    return GraphNode(spec_id=spec_id, uuid=spec_id.lower(), label=label, name=name, spec_id_aliases=aliases)


def _task(task_id: str, phase: str, status: str = "pending", **values: Any) -> PlanTask:
    defaults: dict[str, Any] = {
        "task_id": task_id,
        "area_id": "AREA::A",
        "action": "implement",
        "revision": 1,
        "story_id": "STORY::A",
        "phase": phase,
        "title": f"Task {task_id}",
        "complexity": "medium",
        "planned_by": "model",
        "status": status,
        "created_at": T0,
        "updated_at": T0,
    }
    return PlanTask(**(defaults | values))


async def _finished_build(root, build_id: str = "build-1", *, minutes: int = 0) -> None:
    builds = open_file_stores(root).builds
    record = BuildRecord(
        build_id=build_id,
        project_id=PROJECT,
        mode="first",
        options=BuildOptions(),
        ontology_fingerprint="fp",
        label_set=(),
        embedding_model="embed/model",
        started_at=T0 + timedelta(minutes=minutes),
        finished_at=T0 + timedelta(minutes=minutes + 1),
        status="succeeded",
    )
    await builds.create_build(record)


async def _reported_build(
    root,
    build_id: str,
    *,
    minutes: int,
    report: ChangeReport | None = None,
    status: str = "succeeded",
    persist: str | None = "done",
) -> None:
    """A build that ran and stored its result, with ``report`` as its change report."""

    builds = open_file_stores(root).builds
    record = BuildRecord(
        build_id=build_id,
        project_id=PROJECT,
        mode="update",
        options=BuildOptions(),
        ontology_fingerprint="fp",
        label_set=(),
        embedding_model="embed/model",
        started_at=T0 + timedelta(minutes=minutes),
    )
    await builds.create_build(record)
    if persist is not None:
        await builds.put_progress(PROJECT, build_id, "persist", persist)
    await builds.finish_build(
        BuildResult(
            project_id=PROJECT,
            build_id=build_id,
            mode="update",
            status=status,
            started_at=record.started_at,
            finished_at=record.started_at + timedelta(minutes=1),
            change_report=report,
        )
    )


def _report(spec_id: str, *classes: str, edge: str | None = None) -> ChangeReport:
    return ChangeReport(
        nodes=(ChangedNode(spec_id=spec_id, label=OTHER_LABEL, classes=classes),),
        edges=(ChangedEdge(ref=edge, change="added", basis="stated"),) if edge else (),
    )


def make_runner(tmp_path, plan=None, *, reader: StubPlanReader | None = None, nodes=(), estimate=None, jira=None):
    StubGateway.instances.clear()
    reads = PlanReads(reader or StubPlanReader(), StubGraphReader(list(nodes)))
    ensured: list[Any] = []

    async def ensure(store, ontology, llm) -> None:
        ensured.append(store)

    async def estimated(context) -> PlanEstimate:
        return PlanEstimate(mode="first")

    deps = RunnerDependencies(
        plan=plan or StubPlan(),
        estimate=estimate or estimated,
        ontology=ONTOLOGY,
        rules=RULES,
        ensure=ensure,
        gateway_factory=StubGateway,
        reads=lambda _store, _ontology, _rules: reads,
    )
    settings = SETTINGS.model_copy(update={"state_dir": str(tmp_path)})
    runner = GraphPlanRunner(settings, store=StubStore(), dependencies=deps, jira=jira)
    return runner, ensured


async def _settle(runner: GraphPlanRunner) -> None:
    for task in [run.task for run in runner._runs.values()]:
        await task


# Start and status ---------------------------------------------------------------------


async def test_a_project_without_a_finished_build_cannot_start_a_plan(tmp_path):
    runner, _ = make_runner(tmp_path)

    with pytest.raises(NoGraphBuildError):
        await runner.start(PROJECT)

    assert not runner.running(PROJECT)
    assert (await runner.status(PROJECT)).status == "idle"


async def test_the_first_plan_runs_once_on_the_latest_build_and_reports_its_steps(tmp_path):
    await _finished_build(tmp_path, "build-1")
    await _finished_build(tmp_path, "build-2", minutes=5)
    reader = StubPlanReader()
    plan = StubPlan(block=True, reader=reader)
    runner, ensured = make_runner(tmp_path, plan, reader=reader)

    first = await runner.start(PROJECT)
    await plan.waiting.wait()
    again = await runner.start(PROJECT)  # a double click joins the running run
    running = await runner.status(PROJECT)
    plan.release.set()
    await _settle(runner)
    done = await runner.status(PROJECT)

    assert first.status == "running" and again.status == "running"
    assert len(plan.contexts) == 1
    context = plan.contexts[0]
    assert (context.build_id, context.replan, context.run_id) == ("build-2", False, None)
    assert running.step == "partition" and running.steps_done == 1 and running.steps_total == len(STEPS)
    assert (running.step_items_done, running.step_items_total) == (3, 7)  # the parts of the running step
    assert not running.has_plan
    assert done.status == "succeeded" and done.has_plan and done.finished_at is not None and done.unchanged is False
    assert done.step_items_done is None and done.step_items_total is None
    assert len(ensured) == 1
    assert all(gateway.closed for gateway in StubGateway.instances)


@pytest.mark.parametrize(
    ("plan_build", "replan", "expected"),
    [("build-1", False, False), ("build-1", True, True), ("build-0", False, True)],
)
async def test_an_existing_plan_is_updated_and_replanned_from_an_older_build_or_on_request(
    tmp_path, plan_build, replan, expected
):
    await _finished_build(tmp_path, "build-1")
    reader = StubPlanReader()
    reader.info = _info(plan_build)
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan, reader=reader)

    await runner.start(PROJECT, replan=replan)
    await _settle(runner)

    assert plan.contexts[0].replan is expected
    assert plan.contexts[0].changes == ()


async def test_fr_gpln_01_one_build_after_the_plan_passes_its_report_and_updates(tmp_path):
    await _finished_build(tmp_path, "build-1")
    await _reported_build(
        tmp_path, "build-2", minutes=5, report=_report("REQ::A", "changed_meaning", edge="REQ::A|PART_OF|MOD::M")
    )
    reader = StubPlanReader()
    reader.info = _info("build-1")
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan, reader=reader)

    await runner.start(PROJECT)
    await _settle(runner)

    context = plan.contexts[0]
    assert (context.build_id, context.replan) == ("build-2", False)
    [report] = context.changes
    assert (report.build_id, report.previous_build_id) == ("build-2", "build-1")
    assert [(node.spec_id, node.classes) for node in report.nodes] == [("REQ::A", ("changed_meaning",))]
    assert [(edge.source, edge.relation, edge.target, edge.change) for edge in report.edges] == [
        ("REQ::A", "PART_OF", "MOD::M", "added")
    ]


async def test_fr_gpln_01_several_builds_after_the_plan_pass_every_report_in_build_order(tmp_path, caplog):
    caplog.set_level("INFO")
    await _finished_build(tmp_path, "build-1")
    await _reported_build(tmp_path, "build-3", minutes=5, report=_report("REQ::A", "changed_evidence"))
    # A failed build that never reached persist changed nothing: the chain skips it.
    await _reported_build(tmp_path, "build-2", minutes=7, status="failed", persist=None)
    await _reported_build(tmp_path, "build-4", minutes=9, report=_report("REQ::B", "changed_meaning"))
    reader = StubPlanReader()
    reader.info = _info("build-1")
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan, reader=reader)

    await runner.start(PROJECT)
    await _settle(runner)

    context = plan.contexts[0]
    assert (context.build_id, context.replan) == ("build-4", False)
    assert [(report.previous_build_id, report.build_id) for report in context.changes] == [
        ("build-1", "build-3"),
        ("build-3", "build-4"),
    ]
    assert events(caplog, "api.plan.change_reports_missing") == []
    [started] = events(caplog, "api.plan.started")
    assert fields(started)["change_reports"] == 2


@pytest.mark.parametrize(
    ("damage", "reason", "missing"),
    [
        ("no_report", "report_missing", "build-2"),
        ("unknown_plan_build", "since_build_unknown", "build-0"),
        ("failed_after_persist", "unfinished_build_in_chain", "build-2"),
        ("failed_during_persist", "unfinished_build_in_chain", "build-2"),
        ("bad_edge_ref", "invalid_report", None),
    ],
)
async def test_fr_gpln_01_a_missing_report_replans_and_logs_the_reason_once(tmp_path, caplog, damage, reason, missing):
    caplog.set_level("INFO")
    await _finished_build(tmp_path, "build-1")
    if damage.startswith("failed_"):
        persist = "done" if damage == "failed_after_persist" else "failed"
        await _reported_build(tmp_path, "build-2", minutes=5, status="failed", persist=persist)
    else:
        report = None if damage == "no_report" else _report("REQ::A", "changed_meaning")
        if damage == "bad_edge_ref":
            report = _report("REQ::A", "changed_meaning", edge="REQ::A|PART_OF")
        await _reported_build(tmp_path, "build-2", minutes=5, report=report)
    await _reported_build(tmp_path, "build-3", minutes=9, report=_report("REQ::B", "changed_meaning"))
    reader = StubPlanReader()
    reader.info = _info("build-0" if damage == "unknown_plan_build" else "build-1")
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan, reader=reader)

    await runner.start(PROJECT)
    await _settle(runner)

    context = plan.contexts[0]
    assert (context.build_id, context.replan, context.changes) == ("build-3", True, ())
    [event] = events(caplog, "api.plan.change_reports_missing")
    logged = fields(event)
    assert (logged["reason"], logged["missing_build_id"], logged["build_id"]) == (reason, missing, "build-3")
    assert set(logged) >= {"project_id", "last_build_id"}


async def test_fr_gpln_01_no_plan_yet_is_a_first_run_without_reports(tmp_path, caplog):
    caplog.set_level("INFO")
    await _finished_build(tmp_path, "build-1")
    await _reported_build(tmp_path, "build-2", minutes=5, report=_report("REQ::A", "changed_meaning"))
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan)

    await runner.start(PROJECT)
    await _settle(runner)

    assert (plan.contexts[0].replan, plan.contexts[0].changes) == (False, ())
    assert events(caplog, "api.plan.change_reports_missing") == []


async def test_fr_gpln_01_a_replan_request_passes_no_reports(tmp_path, caplog):
    caplog.set_level("INFO")
    await _finished_build(tmp_path, "build-1")
    await _reported_build(tmp_path, "build-2", minutes=5, report=_report("REQ::A", "changed_meaning"))
    reader = StubPlanReader()
    reader.info = _info("build-1")
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan, reader=reader)

    await runner.start(PROJECT, replan=True)
    await _settle(runner)

    assert (plan.contexts[0].replan, plan.contexts[0].changes) == (True, ())
    assert events(caplog, "api.plan.change_reports_missing") == []


async def test_fr_gpln_01_the_preview_estimates_an_update_with_the_reports(tmp_path):
    await _finished_build(tmp_path, "build-1")
    await _reported_build(tmp_path, "build-2", minutes=5, report=_report("REQ::A", "changed_meaning"))
    reader = StubPlanReader()
    reader.info = _info("build-1")
    contexts: list[Any] = []

    async def estimate(context) -> PlanEstimate:
        contexts.append(context)
        return PlanEstimate(mode="update")

    runner, _ = make_runner(tmp_path, reader=reader, estimate=estimate)

    preview = await runner.preview(PROJECT)

    assert preview.mode == "update" and contexts[0].replan is False
    assert [report.build_id for report in contexts[0].changes] == ["build-2"]


async def test_fr_gpln_01_the_preview_estimates_a_replan_without_a_report_and_logs_nothing(tmp_path, caplog):
    caplog.set_level("INFO")
    await _finished_build(tmp_path, "build-1")
    await _reported_build(tmp_path, "build-2", minutes=5)  # no report: the preview estimates a replan
    reader = StubPlanReader()
    reader.info = _info("build-1")
    contexts: list[Any] = []

    async def estimate(context) -> PlanEstimate:
        contexts.append(context)
        return PlanEstimate(mode="replan")

    runner, _ = make_runner(tmp_path, reader=reader, estimate=estimate)

    preview = await runner.preview(PROJECT)

    assert preview.mode == "replan" and contexts[0].replan is True
    assert events(caplog, "api.plan.change_reports_missing") == []


@pytest.mark.parametrize("replan", [True, False])
async def test_fr_gpln_01_a_resumed_run_keeps_the_build_and_replan_choice_of_its_record(tmp_path, replan):
    await _finished_build(tmp_path, "build-1")
    await _reported_build(tmp_path, "build-2", minutes=5, report=_report("REQ::A", "changed_meaning"))
    await _reported_build(tmp_path, "build-3", minutes=9, report=_report("REQ::B", "changed_meaning"))
    store = open_plan_store(tmp_path)
    await store.create_run(_record("run-old", "build-2", replan=replan, started_at=T0))
    reader = StubPlanReader()
    reader.info = _info("build-1")
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan, reader=reader)

    await runner.start(PROJECT)  # a plain start resumes the interrupted run
    await _settle(runner)

    context = plan.contexts[0]
    assert (context.run_id, context.build_id, context.replan) == ("run-old", "build-2", replan)
    assert [report.build_id for report in context.changes] == ([] if replan else ["build-2"])


async def test_a_failed_run_reports_its_error_code(tmp_path):
    await _finished_build(tmp_path)
    runner, _ = make_runner(tmp_path, StubPlan(fail=ConfigurationError("no_spec_graph")))

    await runner.start(PROJECT)
    await _settle(runner)
    state = await runner.status(PROJECT)

    assert (state.status, state.error_code, state.has_plan) == ("failed", "no_spec_graph", False)


async def test_an_interrupted_run_shows_as_failed_and_the_next_start_resumes_it(tmp_path):
    await _finished_build(tmp_path)
    store = open_plan_store(tmp_path)
    await store.create_run(_record("run-old", "build-1", started_at=T0))
    plan = StubPlan()
    runner, _ = make_runner(tmp_path, plan)

    before = await runner.status(PROJECT)
    await runner.start(PROJECT)
    await _settle(runner)

    assert (before.status, before.error_code, before.run_id) == ("failed", "interrupted", "run-old")
    assert plan.contexts[0].run_id == "run-old"
    assert (await runner.status(PROJECT)).status == "succeeded"


async def test_close_cancels_a_running_plan(tmp_path):
    await _finished_build(tmp_path)
    plan = StubPlan(block=True)
    runner, _ = make_runner(tmp_path, plan)
    await runner.start(PROJECT)
    await plan.waiting.wait()

    await runner.aclose()

    assert not runner.running(PROJECT)
    with pytest.raises(RuntimeError):
        await runner.start(PROJECT)


# Content and preview -------------------------------------------------------------------


async def test_content_is_none_without_a_plan(tmp_path):
    runner, _ = make_runner(tmp_path)

    assert await runner.content(PROJECT) is None


async def test_content_orders_tasks_by_phase_marks_ready_tasks_and_names_what_they_implement(tmp_path):
    reader = StubPlanReader()
    reader.info = _info("build-1")
    reader.tasks_list = [
        _task("TASK::3", "logic", implements=("FEAT::OLD",), waits_for=("TASK::1",)),
        _task("TASK::1", "setup", status="completed"),
        _task("TASK::2", "contract", waits_for=("TASK::4",)),
        _task("TASK::4", "contract", status="in_progress", done_when=(DoneWhen(text="It works.", source="spec"),)),
    ]
    feature = FEATURE_LABELS[0]
    runner, _ = make_runner(tmp_path, reader=reader, nodes=[_node("FEAT::NEW", feature, "Checkout", ("FEAT::OLD",))])

    content = await runner.content(PROJECT)

    assert content is not None and content.plan_id == "plan-1" and content.last_build_id == "build-1"
    assert [task.task_id for task in content.tasks] == ["TASK::1", "TASK::2", "TASK::4", "TASK::3"]
    by_id = {task.task_id: task for task in content.tasks}
    assert by_id["TASK::3"].ready and not by_id["TASK::2"].ready and not by_id["TASK::1"].ready
    assert by_id["TASK::3"].implements[0].model_dump() == {"spec_id": "FEAT::OLD", "label": feature, "name": "Checkout"}
    assert by_id["TASK::4"].done_when[0].model_dump() == {"text": "It works.", "source": "spec"}


async def test_content_reports_the_run_quality_review_findings_and_what_rules_planned(tmp_path):
    reader = StubPlanReader()
    reader.info = _info("build-1").model_copy(update={"last_run_id": "run-1"})
    reader.tasks_list = [_task("TASK::1", "logic"), _task("TASK::2", "logic", planned_by="rules")]
    reader.areas_list = [
        WorkAreaInfo(
            area_id="AREA::A",
            kind="module",
            title="Orders",
            not_buildable=({"spec_id": "REQ::OLD", "reason": "process", "note": "A review step."},),
            planned_by="model",
            status="active",
            created_at=T0,
            updated_at=T0,
        )
    ]
    store = open_plan_store(tmp_path)
    await store.create_run(_record("run-1", "build-1"))
    result = PlanResult(
        project_id=PROJECT,
        run_id="run-1",
        status="partially_succeeded",
        mode="first",
        build_id="build-1",
        started_at=T0,
        finished_at=T0,
        planning=PlanningSummary(
            project=PlannerCounts(calls=2, fallbacks=1, invalid=2),
            tasks=PlannerCounts(calls=7, fallbacks=2, invalid=3),
            review_issues={"blocking": 1},
        ),
        metrics=PlanMetrics(unplaced=4, oversized_task=1),
    )
    await store.finish_run(result.model_dump(mode="json"))
    issue = {"area_id": "AREA::A", "task_id": None, "problem": "No task builds the module.", "severity": "blocking"}
    await store.put_artifact(PROJECT, "run-1", "review", {"issues": [issue]})
    runner, _ = make_runner(tmp_path, reader=reader, nodes=[_node("REQ::NEW", "Requirement", "Review", ("REQ::OLD",))])

    content = await runner.content(PROJECT)

    assert content is not None and content.quality is not None
    assert content.quality.model_dump(exclude={"finished_at"}) == {
        "run_id": "run-1",
        "status": "partially_succeeded",
        "fallbacks": {"project": 1, "tasks": 2},
        "tasks_planned": 2,
        "review": {"blocking": 1},
        "review_failed": False,
        "unplaced": 4,
        "oversized": 1,
    }
    assert [item.model_dump() for item in content.review] == [issue]
    assert [task.planned_by for task in content.tasks] == ["model", "rules"]
    area = content.areas[0]
    assert area.planned_by == "model"
    assert [entry.model_dump() for entry in area.not_buildable] == [
        {"spec_id": "REQ::OLD", "name": "Review", "reason": "process", "note": "A review step."}
    ]


async def test_content_has_no_quality_when_the_run_result_is_gone(tmp_path):
    reader = StubPlanReader()
    reader.info = _info("build-1").model_copy(update={"last_run_id": "run-gone"})
    runner, _ = make_runner(tmp_path, reader=reader)

    content = await runner.content(PROJECT)

    assert content is not None and content.quality is None and content.review == []


async def test_preview_lists_the_features_and_sums_the_estimate(tmp_path):
    await _finished_build(tmp_path)
    contexts: list[Any] = []

    async def estimate(context) -> PlanEstimate:
        contexts.append(context)
        return PlanEstimate(
            mode="first",
            steps={
                "describe": PlanStepEstimate(calls=2, input_tokens=300),
                "tasks": PlanStepEstimate(calls=3, input_tokens=700),
            },
        )

    nodes = [_node("FEAT::B", FEATURE_LABELS[0], "Billing"), _node("REQ::X", OTHER_LABEL, "A requirement")]
    runner, _ = make_runner(tmp_path, nodes=nodes, estimate=estimate)

    preview = await runner.preview(PROJECT)

    assert preview.build_id == "build-1" and preview.mode == "first"
    assert (preview.calls, preview.input_tokens) == (5, 1000)
    assert [feature.model_dump() for feature in preview.features] == [{"spec_id": "FEAT::B", "name": "Billing"}]
    assert contexts[0].build_id == "build-1"
    assert all(gateway.closed for gateway in StubGateway.instances)


async def test_preview_keeps_the_features_when_the_estimate_fails_and_is_empty_without_a_build(tmp_path):
    runner, _ = make_runner(tmp_path)
    assert (await runner.preview(PROJECT)).model_dump() == {
        "build_id": None,
        "mode": None,
        "unchanged": None,
        "calls": None,
        "input_tokens": None,
        "features": [],
    }

    await _finished_build(tmp_path)

    async def failing(context) -> PlanEstimate:
        raise ConfigurationError("no_spec_graph")

    nodes = [_node("FEAT::B", FEATURE_LABELS[0], "Billing")]
    runner, _ = make_runner(tmp_path, nodes=nodes, estimate=failing)
    preview = await runner.preview(PROJECT)

    assert preview.build_id == "build-1" and preview.calls is None
    assert [feature.spec_id for feature in preview.features] == ["FEAT::B"]


# Jira sync hook ----------------------------------------------------------------------------------


class StubJira:
    """The plan runner's Jira hook: records the projects."""

    def __init__(self) -> None:
        self.projects: list[str] = []

    async def after_plan(self, project_id: str) -> None:
        self.projects.append(project_id)


def _with_result(plan: StubPlan, **changes: Any):
    async def run(context) -> PlanResult:
        return (await plan(context)).model_copy(update=changes)

    return run


@pytest.mark.parametrize(
    ("changes", "calls"),
    [
        ({}, 1),  # succeeded
        ({"unchanged": True}, 1),  # an unchanged run too
        ({"status": "partially_succeeded"}, 1),
        ({"status": "failed", "failure": "boom"}, 0),
    ],
)
async def test_a_returned_plan_run_starts_the_jira_sync_except_a_failed_one(tmp_path, changes, calls):
    await _finished_build(tmp_path)
    jira = StubJira()
    runner, _ = make_runner(tmp_path, _with_result(StubPlan(), **changes), jira=jira)

    await runner.start(PROJECT)
    await _settle(runner)

    assert jira.projects == [PROJECT] * calls


async def test_a_plan_run_that_raises_starts_no_jira_sync(tmp_path):
    await _finished_build(tmp_path)
    jira = StubJira()
    runner, _ = make_runner(tmp_path, StubPlan(fail=ConfigurationError("no_spec_graph")), jira=jira)

    await runner.start(PROJECT)
    await _settle(runner)

    assert jira.projects == []
