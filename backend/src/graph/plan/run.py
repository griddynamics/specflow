"""Plan runs: the ten steps, run one by one or all by ``run_plan``, resumable from the ``PlanStore``.

A step that fails on the model endpoint, Neo4j or the store leaves the run unfinished: the host resumes it
with its run id, and no planner call is repeated. A failure that a resume would repeat (a configuration
error, a target plan that breaks an invariant) ends the run ``failed``, which frees the project.

Each step reads its inputs from and writes its output to the ``PlanStore``. ``snapshot`` starts a run
(a new run is refused while the project's previous run is unfinished) and returns its id; the others take
a context bound to that run. A run resumes at its first unfinished step only while the graph fingerprint
equals the one its Snapshot recorded, else it ends ``failed`` with ``graph_changed``. An ``update`` with
nothing changed and nothing left open ends after Snapshot: the later steps are marked done and only the
consumed build, the fingerprint and the meaning revisions are persisted.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel

from graph.core.errors import ConfigurationError, ConformanceError, GraphError
from graph.core.llm.usage import UsageSnapshot
from graph.core.reader import GraphReader
from graph.plan.checks import check_with
from graph.plan.describe import describe
from graph.plan.diff import PLAN_ID, DiffInputs, diff
from graph.plan.models import (
    DescribeArtifact,
    DiffArtifact,
    PartitionArtifact,
    PersistArtifact,
    PlanCheckReport,
    PlanMetrics,
    PlannerCounts,
    PlannerOutcome,
    PlanningSummary,
    PlanResult,
    PlanRunRecord,
    PlanRunStatus,
    PlanStepName,
    ProjectArtifact,
    ReviewArtifact,
    SnapshotArtifact,
    StepUsage,
    StitchArtifact,
    TasksArtifact,
)
from graph.plan.names import plan_ontology_version
from graph.plan.options import PlanContext
from graph.plan.partition import partition
from graph.plan.persist import persist
from graph.plan.project import project
from graph.plan.prompts import PromptName, gateway_prompt
from graph.plan.review import review
from graph.plan.snapshot import fingerprint, select_nodes, snapshot
from graph.plan.stitch import stitch
from graph.plan.tasks import plan_tasks
from graph.plan.view import PlanView
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.plan")

STEPS: Final[tuple[PlanStepName, ...]] = (
    "snapshot",
    "partition",
    "describe",
    "project",
    "tasks",
    "stitch",
    "review",
    "diff",
    "persist",
    "check",
)
ARTIFACTS: Final[Mapping[PlanStepName, type[BaseModel]]] = {
    "snapshot": SnapshotArtifact,
    "partition": PartitionArtifact,
    "describe": DescribeArtifact,
    "project": ProjectArtifact,
    "tasks": TasksArtifact,
    "stitch": StitchArtifact,
    "review": ReviewArtifact,
    "diff": DiffArtifact,
    "persist": PersistArtifact,
    "check": PlanCheckReport,
}
_PLANNER_PROMPTS: Final[Mapping[str, PromptName]] = {
    "describe": "describe",
    "project": "project",
    "tasks": "tasks",
    "review": "review",
}
GRAPH_CHANGED: Final = "graph_changed"
ReaderFactory = Callable[[PlanContext], Any]


def _graph_reader(context: PlanContext) -> GraphReader:
    return GraphReader(context.query_executor, context.ontology, database=context.database)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class StepFunctions:
    """The steps of a plan run; ``reader`` builds the spec-graph reader of a context."""

    reader: ReaderFactory = _graph_reader
    clock: Callable[[], datetime] = _now
    usage: dict[str, StepUsage] = field(default_factory=dict)

    async def snapshot(self, context: PlanContext) -> str:
        """Start a run (or redo the Snapshot of ``context.run_id``); return the run id."""

        if context.run_id is None:
            previous = await context.store.latest_run(context.project_id)
            if previous is not None and not previous.finished:
                raise ConfigurationError("unfinished_run", run_id=previous.run_id)
        artifact = await snapshot(self.reader(context), context.query_executor, context)
        run_id = context.run_id or uuid.uuid4().hex
        if context.run_id is None:
            await context.store.create_run(
                PlanRunRecord(
                    project_id=context.project_id,
                    run_id=run_id,
                    mode=artifact.mode,
                    replan=context.replan,
                    build_id=context.build_id,
                    options=context.options,
                    rules_version=context.rules.version,
                    plan_ontology_version=plan_ontology_version(),
                    fingerprint=artifact.fingerprint,
                    started_at=self.clock(),
                    progress=dict.fromkeys(STEPS, "pending"),
                )
            )
            log_event(logger, INFO, "graph.plan.run_started", run_id=run_id, mode=artifact.mode)
        bound = context.with_run_id(run_id)
        await self._run(bound, "snapshot", lambda: _value(artifact))
        if artifact.unchanged:
            await self._skip_unchanged(bound, artifact)
        return run_id

    async def partition(self, context: PlanContext) -> None:
        async def step() -> PartitionArtifact:
            artifact = await _load(context, "snapshot", SnapshotArtifact)
            return partition(artifact, context.rules, context.ontology, planner_members=context.options.planner_members)

        await self._run(context, "partition", step)

    async def describe(self, context: PlanContext) -> None:
        async def step() -> DescribeArtifact:
            view = await _view(context)
            return await describe(context, view, mode=view.snapshot.mode)

        await self._run(context, "describe", step)

    async def project(self, context: PlanContext) -> None:
        async def step() -> ProjectArtifact:
            view = await _view(context)
            described = await _load(context, "describe", DescribeArtifact)
            return await project(context, view, described, mode=view.snapshot.mode)

        await self._run(context, "project", step)

    async def tasks(self, context: PlanContext) -> None:
        async def step() -> TasksArtifact:
            view = await _view(context)
            described = await _load(context, "describe", DescribeArtifact)
            projected = await _load(context, "project", ProjectArtifact)
            return await plan_tasks(context, view, described, projected, mode=view.snapshot.mode)

        await self._run(context, "tasks", step)

    async def stitch(self, context: PlanContext) -> None:
        async def step() -> StitchArtifact:
            view = await _view(context)
            projected = await _load(context, "project", ProjectArtifact)
            return stitch(view, projected, await _load(context, "tasks", TasksArtifact))

        await self._run(context, "stitch", step)

    async def review(self, context: PlanContext) -> None:
        async def step() -> ReviewArtifact:
            return await review(
                context,
                await _view(context),
                describe=await _load(context, "describe", DescribeArtifact),
                project=await _load(context, "project", ProjectArtifact),
                tasks=await _load(context, "tasks", TasksArtifact),
                stitch=await _load(context, "stitch", StitchArtifact),
            )

        await self._run(context, "review", step)

    async def diff(self, context: PlanContext) -> None:
        async def step() -> DiffArtifact:
            view = await _view(context)
            return diff(
                view,
                describe=await _load(context, "describe", DescribeArtifact),
                project=await _load(context, "project", ProjectArtifact),
                tasks=await _load(context, "tasks", TasksArtifact),
                stitch=await _load(context, "stitch", StitchArtifact),
                inputs=DiffInputs(
                    ontology_version=context.ontology.version,
                    plan_ontology_version=plan_ontology_version(),
                    rules_version=context.rules.version,
                    replan_without_reports=view.snapshot.mode == "replan" and not context.changes,
                ),
            )

        await self._run(context, "diff", step)

    async def persist(self, context: PlanContext) -> None:
        async def step() -> PersistArtifact:
            planned = await _load(context, "diff", DiffArtifact)
            return await persist(
                context.query_executor,
                context.store,
                planned,
                project_id=context.project_id,
                run_id=_run_id(context),
                now=lambda: self.clock().isoformat(timespec="seconds"),
            )

        await self._run(context, "persist", step)

    async def check(self, context: PlanContext) -> None:
        async def step() -> PlanCheckReport:
            return await check_with(context, self.reader(context), now=self.clock())

        await self._run(context, "check", step)

    async def graph_fingerprint(self, context: PlanContext) -> str:
        """The current graph fingerprint, as Snapshot computes it, for the resume guard."""

        reader = self.reader(context)
        nodes = await reader.nodes(context.project_id)
        edges = await reader.edges(context.project_id)
        plannable, context_nodes = select_nodes(nodes, edges, context.rules)
        ids = {node.spec_id for node in plannable}
        return fingerprint(
            context.ontology,
            context.rules,
            [*plannable, *context_nodes],
            [edge for edge in edges if edge.source in ids or edge.target in ids],
            task_size=context.options.task_size,
            planner_members=context.options.planner_members,
        )

    async def _skip_unchanged(self, context: PlanContext, artifact: SnapshotArtifact) -> None:
        """Mark every planning step done with an empty artifact; Diff writes only the consumed build."""

        empty: dict[PlanStepName, BaseModel] = {
            "partition": PartitionArtifact(),
            "describe": DescribeArtifact(),
            "project": ProjectArtifact(),
            "tasks": TasksArtifact(),
            "stitch": StitchArtifact(),
            "review": ReviewArtifact(skipped=True),
            "diff": DiffArtifact(
                plan_properties={
                    "plan_id": PLAN_ID,
                    "last_build_id": artifact.build_id,
                    "graph_fingerprint": artifact.fingerprint,
                },
                revisions=artifact.revisions,
            ),
        }
        for name, payload in empty.items():
            await context.store.put_artifact(
                context.project_id, _run_id(context), name, payload.model_dump(mode="json")
            )
            await context.store.put_progress(context.project_id, _run_id(context), name, "done")

    async def _run(self, context: PlanContext, name: PlanStepName, work: Callable[[], Awaitable[BaseModel]]) -> None:
        run_id = _run_id(context)
        await context.store.put_progress(context.project_id, run_id, name, "running")
        before = context.gateway.usage()
        started = time.monotonic()
        try:
            artifact = await work()
        except GraphError as error:
            await context.store.put_progress(context.project_id, run_id, name, "failed")
            log_event(logger, WARNING, "graph.plan.step_failed", run_id=run_id, step=name, code=error.code)
            raise
        await context.store.put_artifact(context.project_id, run_id, name, artifact.model_dump(mode="json"))
        await context.store.put_progress(context.project_id, run_id, name, "done")
        duration = time.monotonic() - started
        self.usage[name] = _usage(context, name, before, duration)
        log_event(logger, INFO, "graph.plan.step_done", run_id=run_id, step=name, duration_s=round(duration, 3))


steps = StepFunctions()


async def run_plan(context: PlanContext) -> PlanResult:
    """Run (or resume) every step of a plan run and store its result; a finished run returns its result."""

    return await run_with(context, StepFunctions())


async def run_with(context: PlanContext, functions: StepFunctions) -> PlanResult:
    """``run_plan`` with given step functions (a reader factory and a clock)."""

    if context.run_id is not None:
        record = await _record(context)
        if record.finished:
            stored = await context.store.get_result(context.project_id, record.run_id)
            if stored is not None:
                return PlanResult.model_validate(stored)
        if record.progress.get("snapshot") == "done" and record.fingerprint != await functions.graph_fingerprint(
            context
        ):
            return await _finish(context, functions, failure=GRAPH_CHANGED)
        run_id = record.run_id if record.progress.get("snapshot") == "done" else await functions.snapshot(context)
    else:
        run_id = await functions.snapshot(context)
    bound = context.with_run_id(run_id)
    record = await _record(bound)
    try:
        for name in STEPS[1:]:
            if record.progress.get(name) != "done":
                await getattr(functions, name)(bound)
    except (ConfigurationError, ConformanceError) as error:
        await _finish(bound, functions, failure=error.code)
        raise
    return await _finish(bound, functions)


async def _finish(context: PlanContext, functions: StepFunctions, *, failure: str | None = None) -> PlanResult:
    record = await _record(context)
    loaded = {name: await context.store.get_artifact(context.project_id, record.run_id, name) for name in STEPS}
    artifacts = {
        name: ARTIFACTS[name].model_validate(payload) for name, payload in loaded.items() if payload is not None
    }
    result = _result(record, artifacts, functions, failure=failure, finished_at=functions.clock())
    await context.store.finish_run(result.model_dump(mode="json"))
    log_event(
        logger,
        INFO if result.status != "failed" else WARNING,
        "graph.plan.run_finished",
        run_id=record.run_id,
        status=result.status,
        failure=failure,
    )
    return result


def _result(
    record: PlanRunRecord,
    artifacts: Mapping[PlanStepName, BaseModel],
    functions: StepFunctions,
    *,
    failure: str | None,
    finished_at: datetime,
) -> PlanResult:
    snapshot_artifact = _artifact(artifacts, "snapshot", SnapshotArtifact)
    describe_artifact = _artifact(artifacts, "describe", DescribeArtifact)
    project_artifact = _artifact(artifacts, "project", ProjectArtifact)
    tasks_artifact = _artifact(artifacts, "tasks", TasksArtifact)
    review_artifact = _artifact(artifacts, "review", ReviewArtifact)
    stitch_artifact = _artifact(artifacts, "stitch", StitchArtifact)
    diff_artifact = _artifact(artifacts, "diff", DiffArtifact)
    persist_artifact = _artifact(artifacts, "persist", PersistArtifact)
    outcomes = (
        *describe_artifact.outcomes.values(),
        project_artifact.outcome,
        *tasks_artifact.outcomes.values(),
    )
    fallback = any(outcome.fallback for outcome in outcomes)
    status: PlanRunStatus = "failed" if failure else ("partially_succeeded" if fallback else "succeeded")
    issues: dict[str, int] = {}
    for issue in review_artifact.issues:
        issues[issue.severity] = issues.get(issue.severity, 0) + 1
    return PlanResult(
        project_id=record.project_id,
        run_id=record.run_id,
        status=status,
        mode=record.mode,
        unchanged=snapshot_artifact.unchanged,
        build_id=record.build_id,
        started_at=record.started_at,
        finished_at=finished_at,
        failure=failure,
        changes=diff_artifact.summary,
        stale_commits=diff_artifact.stale_commits,
        unverified_completed=diff_artifact.unverified_completed,
        in_progress_changed=diff_artifact.in_progress_changed,
        affected_dependants=diff_artifact.affected_dependants,
        conflicts=persist_artifact.conflicts,
        planning=PlanningSummary(
            describe=_counts(describe_artifact.outcomes.values()),
            project=_counts([project_artifact.outcome]),
            tasks=_counts(tasks_artifact.outcomes.values()),
            review_issues=issues,
            review_failed=review_artifact.failed,
            dropped_dependencies=len(stitch_artifact.dropped),
        ),
        checks=check if isinstance(check := artifacts.get("check"), PlanCheckReport) else None,
        metrics=_metrics(artifacts, functions),
    )


def _metrics(artifacts: Mapping[PlanStepName, BaseModel], functions: StepFunctions) -> PlanMetrics:
    partition_artifact = _artifact(artifacts, "partition", PartitionArtifact)
    describe_artifact = _artifact(artifacts, "describe", DescribeArtifact)
    project_artifact = _artifact(artifacts, "project", ProjectArtifact)
    tasks_artifact = _artifact(artifacts, "tasks", TasksArtifact)
    planned = (*project_artifact.setup, *project_artifact.closing, *tasks_artifact.tasks)
    stories: dict[str, int] = {}
    for story in partition_artifact.stories:
        if story.status == "active":
            stories[story.kind] = stories.get(story.kind, 0) + 1
    phases: dict[str, int] = {}
    for task in planned:
        phases[task.phase] = phases.get(task.phase, 0) + 1
    reasons: dict[str, int] = {}
    for entries in tasks_artifact.not_buildable.values():
        for entry in entries:
            reasons[entry.reason] = reasons.get(entry.reason, 0) + 1
    counts = partition_artifact.counts
    return PlanMetrics(
        areas=sum(1 for area in partition_artifact.areas if area.status == "active"),
        stories_by_kind=stories,
        tasks_by_phase=phases,
        not_buildable=reasons,
        open_questions_on_tasks=sum(1 for task in planned if task.has_open_questions),
        oversized_task=len(tasks_artifact.oversized),
        ambiguous_membership=counts.get("ambiguous_membership", 0),
        cross_cutting=counts.get("cross_cutting", 0),
        unplaced=counts.get("unplaced", 0),
        describe_truncated=len(describe_artifact.truncated),
        steps=dict(functions.usage),
    )


def _counts(outcomes: Any) -> PlannerCounts:
    items: list[PlannerOutcome] = list(outcomes)
    return PlannerCounts(
        calls=sum(item.calls for item in items),
        reused=sum(1 for item in items if item.reused),
        fallbacks=sum(1 for item in items if item.fallback),
        invalid=sum(item.invalid for item in items),
    )


def _usage(context: PlanContext, name: PlanStepName, before: UsageSnapshot, duration: float) -> StepUsage:
    planner = _PLANNER_PROMPTS.get(name)
    if planner is None:
        return StepUsage(duration_s=round(duration, 3))
    prompt = gateway_prompt(planner)
    after = context.gateway.usage().total(prompt=prompt)
    earlier = before.total(prompt=prompt)
    return StepUsage(
        calls=after.calls - earlier.calls,
        input_tokens=after.input_tokens - earlier.input_tokens,
        output_tokens=after.output_tokens - earlier.output_tokens,
        duration_s=round(duration, 3),
    )


async def _view(context: PlanContext) -> PlanView:
    return PlanView(
        await _load(context, "snapshot", SnapshotArtifact),
        await _load(context, "partition", PartitionArtifact),
        context.rules,
    )


async def _load[ModelT: BaseModel](context: PlanContext, name: PlanStepName, model: type[ModelT]) -> ModelT:
    payload = await context.store.get_artifact(context.project_id, _run_id(context), name)
    if payload is None:
        raise ConfigurationError("missing_artifact", step=name)
    return model.model_validate(payload)


def _artifact[ModelT: BaseModel](
    artifacts: Mapping[PlanStepName, BaseModel], name: PlanStepName, model: type[ModelT]
) -> ModelT:
    found = artifacts.get(name)
    return found if isinstance(found, model) else model.model_construct()


async def _record(context: PlanContext) -> PlanRunRecord:
    record = await context.store.get_run(context.project_id, _run_id(context))
    if record is None:
        raise ConfigurationError("unknown_run", run_id=_run_id(context))
    return record


def _run_id(context: PlanContext) -> str:
    if context.run_id is None:
        raise ConfigurationError("unbound_run")
    return context.run_id


async def _value[ModelT: BaseModel](artifact: ModelT) -> ModelT:
    return artifact
