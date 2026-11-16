"""Stubs of the gap host tests (``api.gap.runner`` and the gap routes): a stub ``run_gap``, a stub gateway and builds
stored in a real file build store. No graph store, no database, no model."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from api.gap.runner import GapRunner, GapRunnerDependencies
from api.graph_build.runner import GraphBuildRunner
from api.graph_build.runner import RunnerDependencies as BuildDependencies
from api.plan.runner import GraphPlanRunner, PlanReads
from api.plan.runner import RunnerDependencies as PlanDependencies
from graph.build import BuildOptions, BuildRecord, BuildResult, FileStores, open_file_stores
from graph.build.models import ChangeReport, Settlement
from graph.gap import GapRunResult, GapSettings, NoiseReport, NoiseSection
from graph.gap.run_store import GapRunRecord, MemoryGapRunStore
from graph.plan import PlanResult
from settings import GraphBuildSettings

T0 = datetime(2026, 11, 1, tzinfo=UTC)
PROJECT = str(uuid.UUID(int=1))
OTHER = str(uuid.UUID(int=2))

SETTINGS = GraphBuildSettings(
    LLM_GATEWAY_URL="https://gateway.example.test/openai",
    LLM_GATEWAY_API_KEY="gateway-key-7a1f",
    GRAPH_LLM_MODEL="main/model",
    GRAPH_SMALL_MODEL="small/model",
    GRAPH_EMBEDDING_MODEL="embed/model",
    GRAPH_EMBEDDING_DIM="8",
)
NOISE = NoiseReport(
    sections=(
        NoiseSection(lineage="doc:1", heading_path=("Status",), relevance="tracking", by="rules", removed=("REQ-9",)),
    )
)


class StubStore:
    """A graph store the contexts accept; nothing reads it (the reads are stubs)."""

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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]

    def usage(self) -> Any:
        class Snapshot:
            def total(self) -> StubUsage:
                return StubUsage()

        return Snapshot()

    async def aclose(self) -> None:
        self.closed = True


class StubGapRun:
    """Stands in for ``run_gap``: stores a run record, reports progress, waits for ``release``, then finishes the run
    (or raises ``fail``). Records every call and the most runs that ever ran at once."""

    def __init__(self, *, block: bool = False, fail: Exception | None = None, log: list[str] | None = None) -> None:
        self.contexts: list[Any] = []
        self.modes: list[str] = []
        self.site_ids: list[tuple[str, ...]] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        if not block:
            self.release.set()
        self.fail = fail
        self.active = 0
        self.max_active = 0
        self.log = log

    async def __call__(self, ctx: Any, *, mode: str = "update", site_ids: tuple[str, ...] = ()) -> GapRunResult:
        self.contexts.append(ctx)
        self.modes.append(mode)
        self.site_ids.append(site_ids)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            return await self._run(ctx, mode)
        finally:
            self.active -= 1

    async def _run(self, ctx: Any, mode: str) -> GapRunResult:
        store = ctx.store
        run_id = ctx.run_id or uuid.uuid4().hex
        build_id = ctx.build.build_id
        if ctx.run_id is None:
            record = GapRunRecord(
                project_id=ctx.project_id, run_id=run_id, mode=mode, build_id=build_id, started_at=datetime.now(UTC)
            )
            await store.create_run(record)
        await store.put_progress(ctx.project_id, run_id, "scope", "done")
        await store.put_progress(ctx.project_id, run_id, "close", "running")
        ctx.on_progress("close", 2, 5)
        if self.log is not None:
            self.log.append(f"gap:{build_id}")
        self.started.set()
        await self.release.wait()
        if self.fail is not None:
            raise self.fail
        result = GapRunResult(
            run_id=run_id, mode=mode, build_id=build_id, status="completed", counts={"gaps_new": 1}, noise=NOISE
        )
        await store.finish_run(ctx.project_id, result)
        return result


async def store_build(
    root: Any,
    build_id: str,
    *,
    minutes: int,
    report: ChangeReport | None = None,
    status: str = "succeeded",
    settlement: Settlement | None = None,
    project_id: str = PROJECT,
    **values: Any,
) -> BuildResult:
    """A finished build with its stored result (``report`` as its change report)."""

    builds = open_file_stores(root).builds
    record = BuildRecord(
        build_id=build_id,
        project_id=project_id,
        mode="update",
        options=BuildOptions(),
        ontology_fingerprint="fp",
        label_set=(),
        embedding_model="embed/model",
        started_at=T0 + timedelta(minutes=minutes),
    )
    await builds.create_build(record)
    await builds.put_progress(project_id, build_id, "persist", "done")
    result = BuildResult(
        project_id=project_id,
        build_id=build_id,
        mode="update",
        status=status,
        started_at=record.started_at,
        finished_at=record.started_at + timedelta(minutes=1),
        change_report=report or ChangeReport(),
        settlement=settlement,
        **values,
    )
    await builds.finish_build(result)
    return result


def make_gap_runner(
    tmp_path: Any,
    run: Any = None,
    *,
    gap: GapSettings | None = None,
    runs: Any = None,
    ensured: list[Any] | None = None,
    builds: Any = None,
) -> GapRunner:
    """A runner on ``tmp_path`` (build store) and a memory run store, with stub ports, readers and gateway."""

    StubGateway.instances.clear()

    async def ensure(*args: Any) -> None:
        if ensured is not None:
            ensured.append(args)

    deps = GapRunnerDependencies(
        run=run or StubGapRun(),
        runs=runs if runs is not None else MemoryGapRunStore(),
        builds=builds if builds is not None else open_file_stores(tmp_path).builds,
        ontology=object(),
        rules=object(),
        ensure=ensure,
        gateway_factory=StubGateway,
        ports=lambda _transaction: ("records", "amendments"),
        investigator=lambda _gateway: "investigator",
        executor=lambda _store: "executor",
        reader=lambda _store, _ontology: "reader",
        plan=lambda _store, _ontology: "plan",
        service=lambda *args, **kwargs: (args, kwargs),
    )
    settings = SETTINGS.model_copy(update={"state_dir": str(tmp_path)})
    return GapRunner(settings, gap or GapSettings(), store=object(), transaction=lambda: None, dependencies=deps)


async def drained(runner: GapRunner, project_id: str = PROJECT) -> None:
    await asyncio.wait_for(runner.wait_idle(project_id), 5)


class StubBuild:
    """Stands in for ``run_build``: stores and finishes an update build, logs ``build:<id>``."""

    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.count = 0

    async def __call__(self, ctx: Any) -> BuildResult:
        self.count += 1
        build_id = f"build-{self.count}"
        started = datetime.now(UTC)
        record = BuildRecord(
            build_id=build_id,
            project_id=ctx.project_id,
            mode="update",
            options=BuildOptions(),
            ontology_fingerprint="fp",
            label_set=(),
            embedding_model="embed/model",
            started_at=started,
        )
        await ctx.builds.create_build(record)
        result = BuildResult(
            project_id=ctx.project_id,
            build_id=build_id,
            mode="update",
            status="succeeded",
            started_at=started,
            finished_at=datetime.now(UTC),
            change_report=ChangeReport(),
        )
        await ctx.builds.finish_build(result)
        self.log.append(f"build:{build_id}")
        return result


class StubPlan:
    """Stands in for ``run_plan``: logs ``plan:<build id>`` and returns a result (nothing stored)."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def __call__(self, ctx: Any) -> PlanResult:
        self.log.append(f"plan:{ctx.build_id}")
        now = datetime.now(UTC)
        return PlanResult(
            project_id=ctx.project_id,
            run_id=uuid.uuid4().hex,
            status="succeeded",
            mode="first",
            build_id=ctx.build_id,
            started_at=now,
            finished_at=now,
        )


class NoPlanReader:
    async def plan(self, _project_id: str) -> None:
        return None


@dataclass
class Pipeline:
    """The build, gap and plan runners of one process, wired as ``api.runtime`` wires them."""

    builds: GraphBuildRunner
    gaps: GapRunner
    plans: GraphPlanRunner
    run: StubGapRun
    stores: FileStores

    async def build(self, project_id: str = PROJECT) -> None:
        await self.builds.start(project_id)
        await asyncio.wait_for(self.builds._runs[project_id].task, 5)

    async def aclose(self) -> None:
        for runner in (self.plans, self.builds, self.gaps):
            await runner.aclose()


def make_pipeline(tmp_path: Any, log: list[str], run: StubGapRun) -> Pipeline:
    stores = open_file_stores(tmp_path)

    async def nothing(*_args: Any) -> None:
        return None

    gaps = make_gap_runner(tmp_path, run, builds=stores.builds)
    settings = SETTINGS.model_copy(update={"state_dir": str(tmp_path)})
    builds = GraphBuildRunner(
        settings,
        store=StubStore(),
        session_factory=lambda: None,
        dependencies=BuildDependencies(
            build=StubBuild(log),
            stores=stores,
            ontology=object(),
            ensure=nothing,
            refresh=nothing,
            gateway_factory=StubGateway,
        ),
        gaps=gaps,
    )
    reads = PlanReads(NoPlanReader(), None)
    plans = GraphPlanRunner(
        settings,
        store=StubStore(),
        dependencies=PlanDependencies(
            plan=StubPlan(log),
            builds=stores.builds,
            ontology=object(),
            rules=object(),
            ensure=nothing,
            gateway_factory=StubGateway,
            reads=lambda _store, _ontology, _rules: reads,
        ),
        gaps=gaps,
    )
    return Pipeline(builds=builds, gaps=gaps, plans=plans, run=run, stores=stores)
