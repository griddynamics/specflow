"""Build orchestration offline."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import yaml
from pydantic import BaseModel

from graph.build import STEPS, BuildContext, FileStores, open_file_stores, run_build, steps
from graph.build import edges as edge_extraction
from graph.build import extract as extraction
from graph.build import run as runner
from graph.build.conform import DEDUP_PROMPT, DedupMemory, SameCommitment
from graph.build.consolidate import HUBS_PASS, HUBS_PROMPT, Decision
from graph.build.extract import node_fingerprint, node_passes
from graph.build.models import (
    AnsweredElsewhere,
    BuildMetrics,
    BuildResult,
    CanonicalNode,
    CatalogParts,
    ChangeSet,
    ConsolidationCounters,
    DedupRefusal,
    DiffArtifact,
    EdgeCounters,
    EdgesArtifact,
    EdgeWindowCall,
    FragmentRec,
    HubMerge,
    HubName,
    InferenceReport,
    JudgedItem,
    NodesArtifact,
    PairCandidate,
    PersistCursor,
    PlanArtifact,
    QuarantinedEdge,
    QuarantinedTargetEdge,
    RefusedDedupPair,
    ResolveArtifact,
    ResolveCounters,
    RuleReport,
    StepName,
    TargetCounters,
    TargetGraph,
    TargetNode,
    Unit,
    VerificationMetrics,
    VerificationReport,
)
from graph.build.options import BuildOptions, FragmentTokens
from graph.build.ports import BuildRecord
from graph.build.queries import (
    READ_CURRENT_NODES,
    READ_PERSISTED_DOCUMENTS,
    READ_PERSISTED_FRAGMENTS,
    READ_PERSISTED_HUBS,
    READ_PERSISTED_PRESENCE,
)
from graph.core.errors import ConfigurationError, ConformanceError, LLMError
from graph.core.identity import SpecIdRow, SpecIdState
from graph.core.llm.usage import UsageCounts
from graph.core.ontology import Ontology
from graph_support import (
    GatewayFactory,
    OfflineExecutor,
    ScriptedGateway,
    chat,
    frozen_ontology,
    llm_config,
    load_frozen_ontology,
)
from logger_harness import events, fields

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

PROJECT = "pytest-graph-run"
PERSISTED = {READ_PERSISTED_PRESENCE.name: [{"documents": True, "spec_nodes": True}]}


class Empty(BaseModel):
    pass


def context(stores: FileStores, *, persisted: bool = False, **overrides: Any) -> BuildContext:
    values: dict[str, Any] = {
        "project_id": PROJECT,
        "ontology": frozen_ontology(),  # mechanics tests: the frozen v1.3 ontology
        "driver": None,
        "gateway": ScriptedGateway(),
        "registry": stores.registry,
        "corpus": stores.corpus,
        "extraction": stores.extraction,
        "builds": stores.builds,
        "executor": OfflineExecutor(PERSISTED if persisted else None),
    }
    values.update(overrides)
    return BuildContext(**values)


@pytest.fixture
def stores(tmp_path: Path) -> FileStores:
    return open_file_stores(tmp_path / "stores", corpus_folder=tmp_path / "corpus")


async def record_of(ctx: BuildContext, build_id: str) -> BuildRecord:
    record = await ctx.builds.get_build(ctx.project_id, build_id)
    assert record is not None
    return record


# Mode selection


async def test_a_project_without_persisted_graph_gets_a_first_build(stores: FileStores) -> None:
    ctx = context(stores)
    assert await runner.select_mode(ctx) == "first"
    build_id = await runner.start_build(ctx)
    record = await record_of(ctx, build_id)
    assert (record.mode, record.status, record.progress, record.finished) == ("first", "running", {}, False)
    assert record.options == ctx.options and record.label_set == runner.label_set(ctx.ontology)
    # mode selection only asks whether anything is persisted (select_mode, then start_build)
    assert ctx.executor.reads == [(READ_PERSISTED_PRESENCE.name, PROJECT)] * 2


async def test_a_persisted_graph_without_a_build_record_requires_a_rebuild(stores: FileStores) -> None:
    ctx = context(stores, persisted=True)
    with pytest.raises(ConfigurationError) as raised:
        await run_build(ctx)
    assert raised.value.code == "rebuild_required"
    assert await stores.builds.latest_build(PROJECT) is None  # nothing recorded
    assert ctx.gateway.calls == []


async def test_a_persisted_graph_of_an_unfinished_first_build_names_that_build(stores: FileStores) -> None:
    started = await runner.start_build(context(stores))
    ctx = context(stores, persisted=True)
    with pytest.raises(ConfigurationError) as raised:
        await runner.select_mode(ctx)
    assert (raised.value.code, raised.value.fields) == ("unfinished_build", {"build_id": started})


async def test_step_functions_need_a_build_id(stores: FileStores) -> None:
    ctx = context(stores)
    for step in ("extract_nodes", "resolve", "persist"):
        with pytest.raises(ConfigurationError) as raised:
            await getattr(steps, step)(ctx)
        assert raised.value.code == "build_id_required"
    with pytest.raises(ConfigurationError) as raised:
        await steps.extract_nodes(ctx.with_build_id("nope"))
    assert (raised.value.code, raised.value.fields) == ("build_not_found", {"build_id": "nope"})


async def test_a_step_without_its_input_artifact_fails_and_is_marked_failed(stores: FileStores) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    with pytest.raises(ConfigurationError) as raised:
        await steps.resolve(ctx)
    assert (raised.value.code, raised.value.fields["step"]) == ("artifact_missing", "plan")
    assert (await record_of(ctx, ctx.build_id or "")).progress == {"resolve": "failed"}


# Failed and resumed builds (run_build)


class FakeSteps:
    """Replaces every step function: records the steps run and raises where told."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.ran: list[str] = []
        self.fail: dict[str, Exception] = {}
        for step in STEPS:
            monkeypatch.setitem(runner.STEP_FUNCTIONS, step, self._step(step))

    def _step(self, step: str) -> Callable[[BuildContext], Any]:
        async def run(ctx: BuildContext) -> None:
            async with runner.step_progress(ctx, step):
                self.ran.append(step)
                if step in self.fail:
                    raise self.fail.pop(step)

        return run


async def finished_first_build(stores: FileStores, monkeypatch: pytest.MonkeyPatch) -> str:
    with monkeypatch.context() as patch:
        FakeSteps(patch)
        result = await run_build(context(stores))
    assert result.status == "succeeded"
    return result.build_id


@pytest.mark.parametrize(
    ("error", "code"),
    [(ConformanceError("boom_code", step="x"), "boom_code"), (RuntimeError("secret text"), "RuntimeError")],
)
async def test_a_failed_step_finishes_the_build_failed_and_resumable(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    fakes = FakeSteps(monkeypatch)
    fakes.fail["resolve"] = error
    ctx = context(stores)
    with pytest.raises(type(error)) as raised:
        await run_build(ctx)
    assert raised.value is error
    assert fakes.ran == ["plan", "extract_nodes", "resolve"]

    record = await stores.builds.latest_build(PROJECT)
    assert record is not None
    result = await stores.builds.get_result(PROJECT, record.build_id)
    assert result is not None
    assert (result.project_id, result.build_id, result.mode) == (PROJECT, record.build_id, "first")
    assert (result.status, result.failed_step, result.error_code, result.settled) == ("failed", "resolve", code, False)
    assert result.started_at == record.started_at <= result.finished_at
    assert set(result.metrics.duration_s) == {"plan", "extract_nodes", "resolve"}
    assert (record.status, record.finished_at, record.finished) == ("failed", result.finished_at, False)
    assert record.progress == {"plan": "done", "extract_nodes": "done", "resolve": "failed"}
    assert runner.first_open_step(record) == STEPS.index("resolve")

    # resume: the same build from its first open step; a finished build then returns its result
    fakes.ran.clear()
    resumed = await run_build(ctx.with_build_id(record.build_id))
    assert fakes.ran == list(STEPS[STEPS.index("resolve") :])
    assert (resumed.build_id, resumed.status, resumed.failed_step, resumed.error_code) == (
        record.build_id,
        "succeeded",
        None,
        None,
    )
    assert set(resumed.metrics.duration_s) == set(fakes.ran)
    finished = await record_of(ctx, record.build_id)
    assert finished.finished and all(state == "done" for state in finished.progress.values())
    fakes.ran.clear()
    assert await run_build(ctx.with_build_id(record.build_id)) == resumed
    assert fakes.ran == []


async def test_per_step_llm_usage_is_the_gateways_usage_while_the_step_ran(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = ScriptedGateway()
    gateway.answer("probe.1", None, {})
    fakes = FakeSteps(monkeypatch)

    async def calling(ctx: BuildContext) -> None:
        async with runner.step_progress(ctx, "resolve"):
            fakes.ran.append("resolve")
            for _ in range(3):
                await ctx.gateway.structured(
                    "small", None, [{"role": "user", "content": "x" * 40}], Empty, prompt="probe.1"
                )
            await ctx.gateway.embed(["a", "b"])

    monkeypatch.setitem(runner.STEP_FUNCTIONS, "resolve", calling)
    result = await run_build(context(stores, gateway=gateway))
    assert set(result.metrics.llm) == {"resolve"}
    usage = result.metrics.llm["resolve"]
    total = gateway.usage().total()
    assert (
        (usage.calls, usage.requests, usage.input_tokens, usage.output_tokens)
        == (
            total.calls,
            total.requests,
            total.input_tokens,
            total.output_tokens,
        )
        == (4, 4, 3 * 10 + 2, 3)
    )


async def test_judge_failures_are_counted_per_judging_step_and_warned_above_a_tenth(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    gateway = ScriptedGateway()
    answers = iter([{}, LLMError("exhausted"), {}, {}])  # hub judge: 1 of 4 fails
    gateway.handle(HUBS_PROMPT, lambda _: next(answers))
    gateway.answer(DEDUP_PROMPT, None, {})  # dedup: none fails
    gateway.handle("conform.judge", lambda _: LLMError("exhausted"))  # conform judge: all fail
    fakes = FakeSteps(monkeypatch)

    async def ask(ctx: BuildContext, call_class: str, prompt: str, times: int) -> None:
        for _ in range(times):
            with contextlib.suppress(LLMError):
                await ctx.gateway.structured(call_class, None, [{"role": "user", "content": "x"}], Empty, prompt=prompt)

    async def resolving(ctx: BuildContext) -> None:
        async with runner.step_progress(ctx, "resolve"):
            fakes.ran.append("resolve")
            await ask(ctx, "judge", HUBS_PROMPT, 4)

    async def conforming(ctx: BuildContext) -> None:
        async with runner.step_progress(ctx, "conform"):
            fakes.ran.append("conform")
            await ask(ctx, "small", DEDUP_PROMPT, 3)
            await ask(ctx, "judge", "conform.judge", 2)

    monkeypatch.setitem(runner.STEP_FUNCTIONS, "resolve", resolving)
    monkeypatch.setitem(runner.STEP_FUNCTIONS, "conform", conforming)
    result = await run_build(context(stores, gateway=gateway))
    assert result.status == "succeeded"  # reported, never a gate
    metrics = result.metrics
    assert metrics.judge_calls == {"conform_judge": 2, "hub_consolidation": 4, "requirement_dedup": 3}
    assert metrics.judge_failed_calls == {"conform_judge": 2, "hub_consolidation": 1, "requirement_dedup": 0}
    warned = [fields(record) for record in events(caplog, "graph.build.judge_failures")]
    assert [(record["step"], record["calls"], record["failed_calls"]) for record in warned] == [
        ("hub_consolidation", 4, 1),
        ("conform_judge", 2, 2),
    ]
    assert all(record.levelname == "WARNING" for record in events(caplog, "graph.build.judge_failures"))


async def test_steps_run_one_by_one_record_judge_usage_as_run_build_does(stores: FileStores) -> None:
    """``examples/graph_eval.py`` runs the steps itself, so ``run_build`` finishes with no
    step of its own; ``StepUsage`` records each step as ``run_build`` would and fills the metrics."""

    gateway = ScriptedGateway()
    answers = iter([{}, LLMError("exhausted"), {}])
    gateway.handle(HUBS_PROMPT, lambda _: next(answers))
    gateway.answer(DEDUP_PROMPT, None, {})
    ctx = context(stores, gateway=gateway)
    usage = runner.StepUsage()

    async def step(name: StepName, call_class: str, prompt: str, times: int) -> None:
        before = ctx.gateway.usage()
        for _ in range(times):
            with contextlib.suppress(LLMError):
                await ctx.gateway.structured(call_class, None, [{"role": "user", "content": "x"}], Empty, prompt=prompt)
        usage.record(name, before, ctx.gateway.usage())

    await step("resolve", "judge", HUBS_PROMPT, 2)
    await step("conform", "small", DEDUP_PROMPT, 1)
    assert (usage.judge_calls(), usage.judge_failed_calls()) == (
        {"hub_consolidation": 2, "requirement_dedup": 1},
        {"hub_consolidation": 1, "requirement_dedup": 0},
    )
    filled = usage.filled(BuildMetrics())
    assert set(filled.llm) == {"resolve", "conform"}
    assert filled.judge_calls == {"hub_consolidation": 2, "requirement_dedup": 1}
    assert filled.judge_failed_calls == {"hub_consolidation": 1, "requirement_dedup": 0}
    # a step run_build ran again (a failed step resumed) keeps its own llm; judge counts add up
    rerun = BuildMetrics(llm={"resolve": UsageCounts(calls=1)}, judge_calls={"hub_consolidation": 1})
    merged = usage.filled(rerun)
    assert merged.llm["resolve"] == UsageCounts(calls=1)
    assert merged.judge_calls == {"hub_consolidation": 3, "requirement_dedup": 1}


async def test_a_result_stored_again_with_the_steps_usage_reads_back_with_its_judge_counts(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``examples/graph_eval.py`` runs the steps itself, lets ``run_build`` finish the
    record (no step of its own: empty usage), then stores the result again filled with ``StepUsage``,
    so the stored ``result.json.gz`` (read by ``graph_report``) carries llm and judge counts."""

    gateway = ScriptedGateway()
    gateway.handle(HUBS_PROMPT, lambda _: {})
    fakes = FakeSteps(monkeypatch)

    async def resolving(ctx: BuildContext) -> None:
        async with runner.step_progress(ctx, "resolve"):
            fakes.ran.append("resolve")
            for _ in range(2):
                await ctx.gateway.structured(
                    "judge", None, [{"role": "user", "content": "x"}], Empty, prompt=HUBS_PROMPT
                )

    monkeypatch.setitem(runner.STEP_FUNCTIONS, "resolve", resolving)
    ctx = context(stores, gateway=gateway)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    usage = runner.StepUsage()
    for step in STEPS:
        before = ctx.gateway.usage()
        await runner.STEP_FUNCTIONS[step](ctx)
        usage.record(step, before, ctx.gateway.usage())
    finished = await run_build(ctx)
    assert (finished.metrics.llm, finished.metrics.judge_calls) == ({}, {})  # ran no step itself
    await stores.builds.finish_build(finished.model_copy(update={"metrics": usage.filled(finished.metrics)}))
    stored = await stores.builds.get_result(ctx.project_id, finished.build_id)
    assert stored is not None and stored.status == "succeeded"
    assert stored.metrics.judge_calls == {"hub_consolidation": 2}
    assert stored.metrics.judge_failed_calls == {"hub_consolidation": 0}
    assert set(stored.metrics.llm) == {"resolve"}
    record = await stores.builds.get_build(ctx.project_id, finished.build_id)
    assert record is not None and record.finished


def test_a_result_without_judge_counts_reads_none() -> None:
    assert (BuildMetrics().judge_calls, BuildMetrics().judge_failed_calls) == (None, None)


async def test_per_step_llm_usage_carries_the_cached_input_tokens(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch, make_gateway: GatewayFactory
) -> None:
    cached = iter([0, 30, 50])

    def handler(_request: httpx.Request) -> httpx.Response:
        body = chat("{}", usage=(100, 5))
        body["usage"]["prompt_tokens_details"] = {"cached_tokens": next(cached)}
        return httpx.Response(200, json=body)

    gateway, _ = make_gateway(handler)
    fakes = FakeSteps(monkeypatch)

    async def calling(ctx: BuildContext) -> None:
        async with runner.step_progress(ctx, "resolve"):
            fakes.ran.append("resolve")
            await ctx.gateway.structured("small", None, [{"role": "user", "content": "x"}], Empty, prompt="probe.1")

    async def calling_twice(ctx: BuildContext) -> None:
        async with runner.step_progress(ctx, "conform"):
            fakes.ran.append("conform")
            for _ in range(2):
                await ctx.gateway.structured("judge", None, [{"role": "user", "content": "x"}], Empty, prompt="probe.2")

    monkeypatch.setitem(runner.STEP_FUNCTIONS, "resolve", calling)
    monkeypatch.setitem(runner.STEP_FUNCTIONS, "conform", calling_twice)
    result = await run_build(context(stores, gateway=gateway))
    llm = result.metrics.llm
    assert {step: (usage.input_tokens, usage.cached_input_tokens) for step, usage in llm.items()} == {
        "resolve": (100, 0),
        "conform": (200, 80),
    }
    assert gateway.usage().total().cached_input_tokens == 80


# Mode selection against newer full builds

T0 = datetime(2026, 10, 1, tzinfo=UTC)


async def recorded(
    stores: FileStores,
    mode: str,
    minutes: int,
    *,
    status: str | None = "succeeded",
    **overrides: Any,
) -> BuildRecord:
    """A build record started ``minutes`` after ``T0``, finished with ``status`` (None: still running)."""

    ctx = context(stores)
    values: dict[str, Any] = {
        "build_id": f"{mode}-{minutes}",
        "project_id": PROJECT,
        "mode": mode,
        "options": ctx.options,
        "ontology_fingerprint": runner.ontology_fingerprint(ctx.ontology),
        "label_set": runner.label_set(ctx.ontology),
        "relation_set": runner.relation_set(ctx.ontology),
        "embedding_model": ctx.gateway.config.embedding_model,
        "started_at": T0 + timedelta(minutes=minutes),
        **overrides,
    }
    record = BuildRecord(**values)
    await stores.builds.create_build(record)
    if status is not None:
        await stores.builds.finish_build(
            BuildResult(
                project_id=PROJECT,
                build_id=record.build_id,
                mode=record.mode,
                status=status,
                started_at=record.started_at,
                finished_at=record.started_at + timedelta(seconds=30),
            )
        )
    return record


async def test_a_newer_unfinished_or_failed_full_build_names_that_build(stores: FileStores) -> None:
    await recorded(stores, "first", 0)
    await recorded(stores, "update", 10, status="failed")  # update records never decide the mode
    assert await runner.select_mode(context(stores, persisted=True)) == "update"

    running = await recorded(stores, "rebuild", 20, status=None)
    for ctx in (context(stores, persisted=True), context(stores, persisted=True)):
        with pytest.raises(ConfigurationError) as raised:
            await runner.select_mode(ctx)
        assert (raised.value.code, raised.value.fields) == ("unfinished_build", {"build_id": running.build_id})
    failed = await recorded(stores, "rebuild", 30, status="failed")
    with pytest.raises(ConfigurationError) as raised:
        await run_build(context(stores, persisted=True))
    assert (raised.value.code, raised.value.fields) == ("unfinished_build", {"build_id": failed.build_id})
    # the host may always ask for a rebuild
    assert await runner.select_mode(context(stores, persisted=True, rebuild=True)) == "rebuild"
    await recorded(stores, "rebuild", 40, status="partially_succeeded")
    assert await runner.select_mode(context(stores, persisted=True)) == "update"


@pytest.mark.parametrize(
    ("change", "code", "fields"),
    [
        ("options", "frozen_options_changed", {"fields": "fragment_tokens"}),
        ("labels", "ontology_changed", {"part": "labels"}),
        ("relations", "ontology_changed", {"part": "relations"}),
        ("embedding_model", "embedding_model_changed", {}),
    ],
)
async def test_an_update_that_breaks_what_the_last_full_build_froze_is_refused_before_any_model_call(
    stores: FileStores, gap_ontology: Ontology, change: str, code: str, fields: dict[str, str]
) -> None:
    overrides: dict[str, Any] = {}
    record: dict[str, Any] = {}
    if change == "options":
        overrides["options"] = BuildOptions(fragment_tokens=FragmentTokens(min=100, target=2000, max=3000))
    elif change == "labels":
        overrides["ontology"] = gap_ontology
    elif change == "relations":
        record["relation_set"] = ("PART_OF",)
    else:
        overrides["gateway"] = ScriptedGateway(llm_config(embedding_model="other-embedding"))
    last = await recorded(stores, "first", 0, **record)
    ctx = context(stores, persisted=True, **overrides)
    with pytest.raises(ConfigurationError) as raised:
        await run_build(ctx)
    assert (raised.value.code, raised.value.fields) == (code, fields)
    assert ctx.gateway.calls == []
    latest = await stores.builds.latest_build(PROJECT)
    assert latest is not None and latest.build_id == last.build_id  # nothing recorded
    # a rebuild accepts the change and freezes it anew
    rebuild = context(stores, persisted=True, rebuild=True, **overrides)
    assert await runner.select_mode(rebuild) == "rebuild"


async def test_a_record_without_a_relation_set_is_not_checked_for_relations(stores: FileStores) -> None:
    await recorded(stores, "first", 0, relation_set=None)
    assert await runner.select_mode(context(stores, persisted=True)) == "update"


PLAN_ONTOLOGY: Final[dict[str, Any]] = {
    "format_version": 2,
    "name": "plan-test",
    "version": "0.1",
    "owner": "plan",
    "node_types": {
        "work_area": {
            "label": "WorkArea",
            "origin": "planner",
            "tier": "plan",
            "description": "A work area.",
        },
    },
    "edge_types": {
        "IN_AREA": {
            "origin": "planner",
            "tier": "plan",
            "description": "A module is planned in a work area.",
            "pairs": [{"from": "work_area", "to": "module"}],
        },
    },
}


def plan_ontology(stores: FileStores, tmp_path: Path) -> Ontology:
    path = tmp_path / "plan-ontology.yaml"
    path.write_text(yaml.safe_dump(PLAN_ONTOLOGY, sort_keys=False), encoding="utf-8")
    return load_frozen_ontology(submodule_files=[path])


@pytest.mark.parametrize("part", ["labels", "relations"])
async def test_names_owned_by_a_submodule_do_not_block_an_update(stores: FileStores, tmp_path: Path, part: str) -> None:
    ontology = plan_ontology(stores, tmp_path)
    record: dict[str, Any] = {}
    # An older record holds the full label set, including names a submodule owns.
    record["label_set"] = tuple(frozen_ontology().labels)
    if part == "labels":
        record["relation_set"] = runner.relation_set(frozen_ontology())
    else:
        record["relation_set"] = tuple(frozen_ontology().relations)
    await recorded(stores, "first", 0, **record)

    assert await runner.select_mode(context(stores, persisted=True, ontology=ontology)) == "update"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (("Feature", "Finding"), ("Feature",)),
        (("Feature", "WorkArea"), ("Feature",)),
        (("PART_OF", "FLAGS", "CITES"), ("PART_OF",)),
        (("PART_OF", "IN_AREA"), ("PART_OF",)),
    ],
)
def test_an_old_full_set_record_compares_only_names_the_current_ontology_owns(
    record: tuple[str, ...], expected: tuple[str, ...], gap_ontology: Ontology
) -> None:
    assert runner._build_owned_subset(record, gap_ontology) == expected


# Plan of update and rebuild builds

DOC = (
    "# Orders\n\nThe orders service keeps every order.\n\n## Export\n\nStaff can export orders as CSV.\n\n"
    "## Audit\n\nEvery change is logged.\n"
)
SMALL = BuildOptions(fragment_tokens=FragmentTokens(min=1, target=5, max=50), unit_tokens=100)


class RecordingStore:
    """An ``ExtractionStore`` over another one that records the kinds read."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.read_kinds: list[str] = []

    async def get(self, kind: str, key: str, fingerprint: str) -> Any:
        self.read_kinds.append(kind)
        return await self.inner.get(kind, key, fingerprint)

    async def put(self, kind: str, key: str, fingerprint: str, payload: Any) -> None:
        await self.inner.put(kind, key, fingerprint, payload)


async def first_plan(stores: FileStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlanArtifact:
    """Plan of a finished ``first`` build of ``DOC`` (the other steps faked)."""

    (tmp_path / "corpus").mkdir(exist_ok=True)
    (tmp_path / "corpus" / "a.md").write_text(DOC, encoding="utf-8")
    with monkeypatch.context() as patch:
        FakeSteps(patch)
        patch.setitem(runner.STEP_FUNCTIONS, "plan", runner._plan_step)
        result = await run_build(context(stores, options=SMALL))
    ctx = context(stores, options=SMALL).with_build_id(result.build_id)
    return await runner.load_artifact(ctx, "plan", PlanArtifact)


def persisted_rows(plan_artifact: PlanArtifact, **status: tuple[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Canned reads of a graph persisted from ``plan_artifact``; ``status`` maps fragment ordinals
    (``f<ordinal>``) to a stored ``(status, error_code)``."""

    fragments = []
    for fragment in plan_artifact.fragments:
        stored, code = status.get(f"f{fragment.ordinal}", ("active", None))
        row = fragment.model_dump(
            mode="json",
            include={
                "fragment_id",
                "document_id",
                "heading_path",
                "ordinal",
                "line_start",
                "line_end",
                "text",
                "content_hash",
            },
        )
        extracted = None if stored == "skipped" else fragment.content_hash
        row.update(status=stored, error_code=code, extracted_hash=extracted)
        fragments.append(row)
    return {
        **PERSISTED,
        READ_PERSISTED_DOCUMENTS.name: [document.model_dump(mode="json") for document in plan_artifact.documents],
        READ_PERSISTED_FRAGMENTS.name: fragments,
    }


async def test_an_update_extracts_new_changed_retried_and_stale_fragments_only(
    stores: FileStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await first_plan(stores, tmp_path, monkeypatch)
    intro, export, audit = first.fragments
    rows = persisted_rows(first, f2=("skipped", "llm_malformed"))
    # nodes are stored for the intro only: the export's stored result does not match the fingerprint
    ctx = context(stores, options=SMALL, executor=OfflineExecutor(rows))
    record = await recorded(stores, "update", 1, status=None)
    results = runner.stored_results(ctx, record.mode)
    assert results.read
    fingerprint = node_fingerprint(ctx.ontology, ctx.gateway, SMALL)
    for node_list in node_passes(ctx.ontology):  # both passes stored, but the export under an older fingerprint
        for fragment, stored_under in ((intro, fingerprint), (export, "older"), (audit, fingerprint)):
            key = results.node_key(fragment, fragment.content_hash, node_list)
            await stores.extraction.put("nodes", key, stored_under, {"nodes": []})

    build_id = await steps.plan(ctx)
    plan_artifact = await runner.load_artifact(ctx.with_build_id(build_id), "plan", PlanArtifact)
    assert plan_artifact.mode == "update"
    assert plan_artifact.change_set.unchanged == ("a.md",)
    assert [fragment.change for fragment in plan_artifact.fragments] == ["unchanged"] * 3
    # the stale export only; the skipped audit waits for a change or a retry
    assert plan_artifact.dirty_ids == (export.fragment_id,)
    assert [unit.windows for unit in plan_artifact.units] == [((1, 2),)]
    assert plan_artifact.retired_ids == ()

    retry = context(stores, options=SMALL, executor=OfflineExecutor(rows), retry_lineages=("a.md",))
    retried = await runner.load_artifact(retry.with_build_id(await steps.plan(retry)), "plan", PlanArtifact)
    assert retried.change_set.retried == ("a.md",)
    assert [fragment.change for fragment in retried.fragments] == ["unchanged", "unchanged", "retried"]
    assert retried.dirty_ids == (export.fragment_id, audit.fragment_id)


async def test_a_rebuild_extracts_everything_without_reading_stored_results(
    stores: FileStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await first_plan(stores, tmp_path, monkeypatch)
    store = RecordingStore(stores.extraction)
    executor = OfflineExecutor(persisted_rows(first, f2=("skipped", "x")))
    ctx = context(stores, options=SMALL, rebuild=True, extraction=store, executor=executor)
    plan_artifact = await runner.load_artifact(ctx.with_build_id(await steps.plan(ctx)), "plan", PlanArtifact)
    assert plan_artifact.mode == "rebuild"
    assert plan_artifact.dirty_ids == tuple(fragment.fragment_id for fragment in first.fragments)
    assert "nodes" not in store.read_kinds


class Captured:
    """Stand-ins for the extraction, resolve and conform functions that record what run passes them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.kwargs: dict[str, dict[str, Any]] = {}
        for module, name, value in (
            (runner.extraction, "extract_nodes", NodesArtifact()),
            (runner.resolution, "resolve", ResolveArtifact()),
            (runner.edge_extraction, "extract_edges", EdgesArtifact()),
            (runner.conformance, "conform", TargetGraph()),
        ):
            monkeypatch.setattr(module, name, self._stub(name, value))

    def _stub(self, name: str, value: Any) -> Callable[..., Any]:
        async def run(*args: Any, **kwargs: Any) -> Any:
            self.kwargs[name] = kwargs
            return value

        return run


async def run_steps(ctx: BuildContext) -> BuildContext:
    ctx = ctx.with_build_id(await steps.plan(ctx))
    for step in (steps.extract_nodes, steps.resolve, steps.extract_edges, steps.conform):
        await step(ctx)
    return ctx


async def test_a_rebuild_reuses_vectors_decisions_and_verdicts_but_no_extraction_result(
    stores: FileStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await first_plan(stores, tmp_path, monkeypatch)
    rows = persisted_rows(first)
    rows[READ_PERSISTED_HUBS.name] = [{"name": "Orders service", "label": "Module"}]
    gateway = ScriptedGateway()
    # what earlier builds stored: a vector, a hub consolidation decision, a dedup verdict
    cache = runner.vector_cache(context(stores, gateway=gateway))
    await cache.get_or_embed(["Module: Orders service"])
    hashes = {"a": "ha", "b": "hb", "x": "hx", "y": "hy"}
    stored = Decision(same=True, label="Module", confidence="high")
    await runner.decision_memory(context(stores)).write(HUBS_PASS, HUBS_PROMPT, {("a", "b"): stored}, {}, hashes)
    dedup = DedupMemory(stores.extraction, PROJECT, llm_config().model_for("small"))
    await dedup.write({("x", "y"): SameCommitment(same=False, confidence="high")}, hashes)

    captured = Captured(monkeypatch)
    options = SMALL.model_copy(update={"hub_catalog": False})
    rebuild = await run_steps(
        context(stores, options=options, rebuild=True, gateway=gateway, executor=OfflineExecutor(rows))
    )
    nodes, resolve_kwargs, edges, conform_kwargs = (
        captured.kwargs[name] for name in ("extract_nodes", "resolve", "extract_edges", "conform")
    )
    assert nodes["results"].read is False and edges["results"].read is False
    assert nodes["persisted_hubs"] == []  # a rebuild never shows the persisted hubs
    assert resolve_kwargs["persisted"] is None and conform_kwargs["persisted"] is None  # no persisted label
    decisions = await resolve_kwargs["memory"].pairs(HUBS_PASS, HUBS_PROMPT, [("b", "a")], hashes)
    assert decisions.found == {("a", "b"): stored}
    dedup_memory = conform_kwargs["judging"].dedup_memory
    assert dedup_memory is not None
    assert (await dedup_memory.verdicts([("y", "x")], hashes)).found == {dedup.key("x", "y"): False}
    embeds = len(gateway.calls_for(call_class="embedding"))
    await resolve_kwargs["vectors"].get_or_embed(["Module: Orders service"])
    assert len(gateway.calls_for(call_class="embedding")) == embeds  # the stored vector, no new call
    reads = [name for name, _ in rebuild.executor.reads]
    assert READ_PERSISTED_HUBS.name not in reads and READ_CURRENT_NODES.name not in reads

    # once the rebuild finished, an update reads the stored results, the persisted hubs and labels
    record = await record_of(rebuild, rebuild.build_id or "")
    await stores.builds.finish_build(
        BuildResult(
            project_id=PROJECT,
            build_id=record.build_id,
            mode="rebuild",
            status="succeeded",
            started_at=record.started_at,
            finished_at=record.started_at,
        )
    )
    update = await run_steps(context(stores, options=options, gateway=gateway, executor=OfflineExecutor(rows)))
    assert (await record_of(update, update.build_id or "")).mode == "update"
    assert captured.kwargs["extract_nodes"]["results"].read is True
    assert captured.kwargs["extract_nodes"]["persisted_hubs"] == [HubName(name="Orders service", label="Module")]
    assert captured.kwargs["resolve"]["persisted"] == {} and captured.kwargs["conform"]["persisted"] == {}


# Diff re-run and metrics


async def test_a_new_diff_resets_the_persist_cursor_and_reopens_the_later_steps(stores: FileStores) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    for step, artifact in (
        ("plan", PlanArtifact(mode="first", change_set=ChangeSet(), documents=(), fragments=())),
        ("extract_nodes", NodesArtifact()),
        ("extract_edges", EdgesArtifact()),
        ("conform", TargetGraph()),
        ("persist", PersistCursor(batch_index=2, batches=5)),
    ):
        await runner.store_artifact(ctx, step, artifact)
    for step in ("plan", "extract_nodes", "resolve", "extract_edges", "conform", "diff", "persist", "infer"):
        await stores.builds.put_progress(PROJECT, ctx.build_id or "", step, "done")

    await steps.diff(ctx)
    assert await runner.load_artifact(ctx, "persist", PersistCursor) == PersistCursor()
    record = await record_of(ctx, ctx.build_id or "")
    progress = record.progress
    assert (progress["diff"], progress["persist"], progress["infer"]) == ("done", "failed", "failed")
    assert "verify" not in record.progress
    assert runner.first_open_step(record) == STEPS.index("persist")


def fragment_rec(name: str, **update: Any) -> FragmentRec:
    return FragmentRec(
        fragment_id=f"a.md#{name}",
        document_id="a.md",
        heading_path=("A", name),
        ordinal=int(name[1:]),
        line_start=int(name[1:]) + 1,
        line_end=int(name[1:]) + 1,
        text=name,
        content_hash=name,
        tokens=1,
        **update,
    )


async def test_metrics_count_fragments_by_status_and_units_extracted_or_reused(stores: FileStores) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    fragments = (
        fragment_rec("f1"),  # new, extracted
        fragment_rec("f2", change="unchanged", status="skipped", error_code="llm_refused"),  # skipped earlier
        fragment_rec("f3", change="changed"),  # skipped now
        fragment_rec("f4", change="unchanged"),  # reused
        fragment_rec("f5", change="unchanged", status="skipped", error_code="llm_malformed"),  # recovered
    )

    def unit(name: str, members: tuple[str, ...], windows: tuple[tuple[int, int], ...]) -> Unit:
        return Unit(
            unit_id=name,
            document_id="a.md",
            fragment_ids=tuple(f"a.md#{member}" for member in members),
            tokens=1,
            content_hash=name,
            windows=windows,
        )

    plan_artifact = PlanArtifact(
        mode="first",
        change_set=ChangeSet(),
        documents=(),
        fragments=fragments,
        retired_ids=("a.md#old",),
        dirty_ids=("a.md#f1", "a.md#f3"),
        units=(
            unit("u1", ("f1", "f2"), ((0, 1),)),
            unit("u2", ("f3",), ((0, 1),)),
            unit("u3", ("f4",), ()),
            unit("u4", ("f5",), ()),
        ),
    )
    await runner.store_artifact(ctx, "plan", plan_artifact)
    await runner.store_artifact(ctx, "extract_nodes", NodesArtifact(skipped={"a.md#f3": "llm_malformed"}))
    await runner.store_artifact(ctx, "extract_edges", EdgesArtifact(called_units=("u4",), recovered=("a.md#f5",)))

    result = await runner._finish(ctx, runner._RunStats())
    assert result.status == "partially_succeeded"
    assert result.skipped_fragments == {"a.md#f2": "llm_refused", "a.md#f3": "llm_malformed"}
    assert result.metrics.fragments_by_status == {"active": 3, "retired": 1, "skipped": 2}
    # a unit is extracted when its node windows or its edge call ran
    assert (result.metrics.units_extracted, result.metrics.units_reused) == (3, 1)


# Extractor fingerprints


class PartOntology:
    """The frozen ontology with one prompt part's fingerprint replaced, or its spine ``exclude``
    (the out-of-scope marker of the node and attribute prompts) set (all the fingerprints read)."""

    def __init__(self, part: str = "", exclude: Mapping[str, frozenset[str]] | None = None) -> None:
        self._ontology = frozen_ontology()
        self._part = part
        self._exclude = exclude

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ontology, name)

    def fingerprint(self, part: str) -> str:
        value = self._ontology.fingerprint(part)
        return "edited-" + value if part == self._part else value

    def spine_exclude(self) -> Mapping[str, frozenset[str]]:
        return self._ontology.spine_exclude() if self._exclude is None else self._exclude


Fingerprints = dict[str, str]


def all_fingerprints(ontology: Any = None, config: Any = None, options: BuildOptions | None = None) -> Fingerprints:
    ontology = ontology or frozen_ontology()
    gateway = ScriptedGateway(config or llm_config())
    options = options or BuildOptions()
    return {
        "nodes": extraction.node_fingerprint(ontology, gateway, options),
        "edges": extraction.edge_fingerprint(ontology, gateway, options),
        "attributes": extraction.attribute_fingerprint(ontology, gateway),
    }


def changed(after: Fingerprints) -> set[str]:
    before = all_fingerprints()
    return {kind for kind in before if before[kind] != after[kind]}


def test_the_fingerprints_are_stable_and_distinct() -> None:
    first = all_fingerprints()
    assert first == all_fingerprints()
    assert len(set(first.values())) == 3


@pytest.mark.parametrize(
    ("part", "kinds"),
    [("nodes", {"nodes"}), ("edges", {"edges"}), ("attributes", {"attributes"}), ("resolution", set())],
)
def test_an_ontology_prompt_part_changes_only_its_fingerprint(part: str, kinds: set[str]) -> None:
    assert changed(all_fingerprints(ontology=PartOntology(part))) == kinds


def test_the_out_of_scope_marker_changes_the_node_and_attribute_fingerprints() -> None:
    """The spine's ``exclude`` is outside the prompt parts, yet the node and attribute prompts render it."""

    assert frozen_ontology().spine_exclude() == {}
    assert changed(all_fingerprints(ontology=PartOntology(exclude={"x": frozenset({"y"})}))) == {
        "nodes",
        "attributes",
    }


@pytest.mark.parametrize(
    ("model", "kinds"),
    [
        ({"main_model": "other-main"}, {"nodes", "edges"}),  # extraction calls use the main model
        ({"small_model": "other-small"}, {"attributes"}),
        ({"judge_model": "other-judge"}, set()),
        ({"embedding_model": "other-embedding"}, set()),
    ],
)
def test_the_call_model_changes_the_fingerprints_of_its_calls(model: dict[str, str], kinds: set[str]) -> None:
    assert changed(all_fingerprints(config=llm_config(**model))) == kinds


@pytest.mark.parametrize(
    ("constant", "kinds"),
    [
        ("NODE_PROMPTS", {"nodes"}),
        ("EDGE_WINDOW_PROMPT", {"edges"}),
        ("ATTRIBUTES_PROMPT", {"attributes"}),
    ],
)
def test_a_prompt_version_changes_only_its_fingerprint(
    constant: str, kinds: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = all_fingerprints()
    value = getattr(extraction, constant)
    bumped = {name: f"{prompt}-next" for name, prompt in value.items()} if isinstance(value, dict) else f"{value}-next"
    monkeypatch.setattr(extraction, constant, bumped)
    after = all_fingerprints()
    assert {kind for kind in before if before[kind] != after[kind]} == kinds


@pytest.mark.parametrize(
    ("renderer", "kinds"),
    [
        ("node_fixed_text", {"nodes"}),
        ("edge_window_system_message", {"edges"}),
        ("attribute_system_message", {"attributes"}),
    ],
)
def test_the_rendered_system_message_changes_only_its_fingerprint(
    renderer: str, kinds: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reworded format-level rule changes the system message, so its extractor re-extracts even
    when the prompt name was not bumped."""

    before = all_fingerprints()
    rendered = getattr(extraction, renderer)
    monkeypatch.setattr(extraction, renderer, lambda *args, **kwargs: rendered(*args, **kwargs) + " Reworded.")
    after = all_fingerprints()
    assert {kind for kind in before if before[kind] != after[kind]} == kinds


OPTION_EDITS: dict[str, dict[str, Any]] = {
    "fragment_tokens": {"fragment_tokens": FragmentTokens(min=200, target=1500, max=2500)},
    "hub_catalog": {"hub_catalog": False},
    "known_terms_limit": {"known_terms_limit": 60},
    "catalog_limit": {"catalog_limit": 75},
    "locality": {"locality": "strict"},
    "unit_tokens": {"unit_tokens": 30000},
    "max_node_windows": {"max_node_windows": 4},
}


@pytest.mark.parametrize(
    ("option", "kinds"),
    [
        ("fragment_tokens", {"nodes", "edges"}),
        ("unit_tokens", {"nodes", "edges"}),
        ("hub_catalog", {"nodes"}),
        ("known_terms_limit", {"nodes"}),
        ("max_node_windows", {"nodes"}),
        ("catalog_limit", {"edges"}),
        ("locality", set()),  # conform only: shapes no extraction call
    ],
)
def test_a_shaping_option_changes_only_the_fingerprints_it_shapes(option: str, kinds: set[str]) -> None:
    assert set(OPTION_EDITS) == set(BuildOptions.model_fields)  # every option is classified here
    options = BuildOptions().model_copy(update=OPTION_EDITS[option])
    assert getattr(options, option) != getattr(BuildOptions(), option)
    assert changed(all_fingerprints(options=options)) == kinds


# Edge fallback of a failed edge call


def edge_item(relation: str, source: str, target: str, positions: list[int], fragments: list[str]) -> dict[str, Any]:
    return {
        "relation": relation,
        "source": source,
        "target": target,
        "fact": f"{source} {relation} {target}",
        "positions": positions,
        "fragments": fragments,
    }


async def test_a_failed_edge_window_falls_back_to_its_last_edges_by_content_then_by_identity(
    stores: FileStores,
) -> None:
    results = extraction.StoredResults(stores.extraction, PROJECT, read=True)
    fingerprint = "edges-fp"
    # the unit now: its first fragment replaced "a.md#f0" (a changed first body line), the second is new
    f1 = fragment_rec("f1", previous_id="a.md#f0")
    f2 = fragment_rec("f2")
    unit = Unit(
        unit_id="u",
        document_id="a.md",
        fragment_ids=(f1.fragment_id, f2.fragment_id),
        tokens=2,
        content_hash="unit-now",
        windows=(),
    )
    fragments_by_id = {f1.fragment_id: f1, f2.fragment_id: f2}
    window = (0, 2)  # the unit's one edge window

    # canonical nodes now: "Module|surface|b" is an alias of the node keyed "Module|surface|b2"; an alias
    # never shadows another node's key
    def canonical(cid: str, key: str, *aliases: str) -> CanonicalNode:
        return CanonicalNode(
            cid=cid,
            identity_key=key,
            identity_aliases=aliases,
            name=key,
            label=key.split("|", maxsplit=1)[0],
            member_ids=(cid,),
        )

    cid_of = edge_extraction._cids_by_key(
        [
            canonical("c1", "Module|surface|a", "Feature|surface|f"),
            canonical("c2", "Module|surface|b2", "Module|surface|b"),
            canonical("c3", "Feature|surface|f"),
        ]
    )
    by_identity = {
        "edges": [
            edge_item("PART_OF", "Feature|surface|f", "Module|surface|a", [0], ["a.md#f0", "a.md#gone"]),
            edge_item("INTEGRATES_WITH", "Module|surface|a", "Module|surface|b", [1], ["a.md#f2", "a.md#f0"]),
            edge_item("PART_OF", "Feature|surface|f", "Module|surface|missing", [0], ["a.md#f0"]),  # no node now
            edge_item("INTEGRATES_WITH", "Module|surface|b", "Module|surface|b2", [0], ["a.md#f2"]),  # one node now
        ],
        "near": [],
    }

    async def found() -> Any:
        fallbacks = await edge_extraction._fallback_edges(
            results, fingerprint, [(unit, window)], fragments_by_id, cid_of
        )
        return fallbacks.get(("u", window))

    async def fallback() -> list[Any]:
        got = await found()
        return got.edges if got is not None else []

    assert await fallback() == []  # nothing stored: no fallback
    # stored under the identity of the id the first fragment replaces (or another fingerprint: unusable)
    await stores.extraction.put("edges", results.window_edge_key("a.md", "a.md#f0"), "other-fp", by_identity)
    assert await fallback() == []
    await stores.extraction.put("edges", results.window_edge_key("a.md", "a.md#f0"), fingerprint, by_identity)
    edges = await fallback()
    # endpoints mapped by identity key or alias; fragments by id (a replaced id to its successor
    # unknown ids dropped); an edge without both ends, or between one node, left out
    assert [(e.relation, e.source_cid, e.target_cid, e.fragment_ids, e.reused, e.unit_id) for e in edges] == [
        ("PART_OF", "c3", "c1", (f1.fragment_id,), True, "u"),
        ("INTEGRATES_WITH", "c1", "c2", (f2.fragment_id, f1.fragment_id), True, "u"),
    ]
    assert edges[0].fact == "Feature|surface|f PART_OF Module|surface|a"
    # stored without its window's fragment ids: it covers none of this window's fragments
    assert (await found()).uncovered == (f1.fragment_id, f2.fragment_id)

    # the unit's own identity (document and current first fragment) comes before the replaced id
    own = {
        "edges": [edge_item("PART_OF", "Feature|surface|f", "Module|surface|b", [1], [f1.fragment_id])],
        "near": [],
        "window_fragments": ["a.md#f0"],  # a narrower window, before f1 replaced a.md#f0
    }
    await stores.extraction.put("edges", results.window_edge_key("a.md", f1.fragment_id), fingerprint, own)
    assert [(e.source_cid, e.target_cid, e.fragment_ids) for e in await fallback()] == [("c3", "c2", (f1.fragment_id,))]
    assert (await found()).uncovered == (f2.fragment_id,)  # f1 by the id it replaces; f2 was not in that window

    # the same content wins over any identity answer; its fragments come by position
    same = {"edges": [edge_item("INTEGRATES_WITH", "Module|surface|b", "Module|surface|a", [1, 5], ["x"])], "near": []}
    digest = edge_extraction.window_hash(unit, window, fragments_by_id)
    await stores.extraction.put("edges", results.last_edge_key(digest), fingerprint, same)
    assert [(e.source_cid, e.target_cid, e.fragment_ids) for e in await fallback()] == [("c2", "c1", (f2.fragment_id,))]
    assert (await found()).uncovered == ()  # the same content covers the whole window


def test_edge_metrics_add_the_inferred_edges_the_infer_step_changed() -> None:
    """``BuildMetrics.edges_by_relation``: the edges the diff leaves once persisted, with the inferred
    edges infer added and removed afterwards (a relation left without edges is not listed)."""

    persisted = DiffArtifact(edges_by_relation={"PART_OF": 3, "RELATES_TO": 1})
    report = InferenceReport(
        inferred_added=("REQ::FR::A|PART_OF|MOD::X", "MOD::X|INTEGRATES_WITH|MOD::Y"),
        inferred_removed=("REQ::FR::B|RELATES_TO|MOD::X",),
    )
    assert runner._edges_after(persisted, report) == {"INTEGRATES_WITH": 1, "PART_OF": 4}
    assert runner._edges_after(persisted, None) == {"PART_OF": 3, "RELATES_TO": 1}
    assert runner._edges_after(None, report) == {}


# Quarantine: ends by spec_id in the result, identity keys only in the artifact

FR_KEY, MOD_KEY, AUDIT_KEY = "requirement|span|secret plan text", "Module|surface|vault", "Capability|surface|audit"


def quarantine_entry(
    reason: str, relation: str, source: tuple[str, str], target: tuple[str, str]
) -> QuarantinedTargetEdge:
    return QuarantinedTargetEdge.model_validate(
        {
            "reason": reason,
            "relation": relation,
            "source_label": source[0],
            "target_label": target[0],
            "basis": "stated",
            "fragment_ids": ("a.md#f1",),
            "source_key": source[1],
            "target_key": target[1],
        }
    )


QUARANTINED = (
    quarantine_entry("illegal_pair", "PART_OF", ("Capability", AUDIT_KEY), ("Module", MOD_KEY)),
    quarantine_entry("locality", "RELATES_TO", ("Module", MOD_KEY), ("FunctionalRequirement", FR_KEY)),
    quarantine_entry("unknown_relation", "LIKES", ("FunctionalRequirement", FR_KEY), ("Module", MOD_KEY)),
)


def spec_row(spec_id: str, key: str, state: SpecIdState = "active") -> SpecIdRow:
    return SpecIdRow(spec_id=spec_id, identity_key=key, uuid=f"uuid-{spec_id}", state=state)


async def test_the_result_lists_the_quarantine_by_spec_id_and_counts_it(stores: FileStores) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    await stores.registry.record(
        PROJECT, "earlier", [spec_row("MOD::VAULT", MOD_KEY), spec_row("FR::OLD", FR_KEY), spec_row("FR::GONE", "x")]
    )
    await runner.store_artifact(ctx, "conform", TargetGraph(quarantine=QUARANTINED))
    # This build's diff rows win over the registry: the requirement continues as FR::PLAN (not yet persisted).
    rows = (spec_row("FR::PLAN", FR_KEY), spec_row("FR::OLD", FR_KEY, "alias"))
    await runner.store_artifact(ctx, "diff", DiffArtifact(registry_rows=rows))

    result = await runner._finish(ctx, runner._RunStats())
    assert [(e.reason, e.relation, e.source_spec_id, e.target_spec_id) for e in result.quarantine] == [
        ("illegal_pair", "PART_OF", None, "MOD::VAULT"),  # an end without spec_id: None
        ("locality", "RELATES_TO", "MOD::VAULT", "FR::PLAN"),
        ("unknown_relation", "LIKES", "FR::PLAN", "MOD::VAULT"),
    ]
    assert result.quarantine[0].source_label == "Capability" and result.quarantine[0].fragment_ids == ("a.md#f1",)
    assert result.metrics.quarantine_by_reason == {"illegal_pair": 1, "locality": 1, "unknown_relation": 1}
    assert result.metrics.quarantine_by_pair == {
        "LIKES|FunctionalRequirement|Module": 1,
        "PART_OF|Capability|Module": 1,
        "RELATES_TO|Module|FunctionalRequirement": 1,
    }
    stored = await stores.builds.get_result(PROJECT, ctx.build_id or "")
    assert stored is not None and stored.quarantine == result.quarantine
    dumped = result.model_dump_json()
    assert not any(key in dumped for key in (FR_KEY, MOD_KEY, AUDIT_KEY, "source_key", "target_key"))


async def test_the_result_joins_the_inference_quarantine_after_conform_s(stores: FileStores) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    await stores.registry.record(PROJECT, "earlier", [spec_row("MOD::VAULT", MOD_KEY), spec_row("FR::OLD", FR_KEY)])
    counters = TargetCounters(conform_judge_calls=2)
    await runner.store_artifact(ctx, "conform", TargetGraph(quarantine=QUARANTINED[:1], counters=counters))
    proposal = QuarantinedEdge(
        reason="invalid_proposal",
        relation="PART_OF",
        source_label="FunctionalRequirement",
        target_label="Module",
        basis="inferred",
        source_spec_id="FR::OLD",
        target_spec_id="MOD::VAULT",
    )
    await runner.store_artifact(ctx, "infer", InferenceReport(quarantine=(proposal,)))
    await stores.builds.put_progress(PROJECT, ctx.build_id or "", "infer", "done")

    result = await runner._finish(ctx, runner._RunStats())
    assert [(e.reason, e.source_spec_id) for e in result.quarantine] == [
        ("illegal_pair", None),
        ("invalid_proposal", "FR::OLD"),
    ]
    assert result.metrics.quarantine_by_reason == {"illegal_pair": 1, "invalid_proposal": 1}
    assert result.metrics.conform_judge_calls == 2
    assert result.metrics.quarantine_by_pair == {
        "PART_OF|Capability|Module": 1,
        "PART_OF|FunctionalRequirement|Module": 1,
    }


async def test_a_conform_artifact_without_quarantine_reads_n_a_and_one_that_quarantined_nothing_zero(
    stores: FileStores,
) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    v13 = TargetGraph().model_dump(mode="json")
    del v13["quarantine"]
    await stores.builds.put_artifact(PROJECT, ctx.build_id or "", "conform", v13)
    assert await runner.load_artifact(ctx, "conform", TargetGraph) == TargetGraph()
    result = await runner._finish(ctx, runner._RunStats())
    assert result.quarantine == ()
    assert (result.metrics.quarantine_by_reason, result.metrics.quarantine_by_pair) == (None, None)

    await runner.store_artifact(ctx, "conform", TargetGraph())
    result = await runner._finish(ctx, runner._RunStats())
    assert (result.metrics.quarantine_by_reason, result.metrics.quarantine_by_pair) == ({}, {})


async def test_the_result_lists_guard_refused_dedup_pairs_by_spec_id_only(stores: FileStores) -> None:
    """D8: the conform artifact's refused pairs reach the result by ``spec_id`` (None without one) and
    are counted per guard; their identity keys never leave the artifact."""

    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    other_key = "requirement|span|another secret sentence"
    await stores.registry.record(PROJECT, "earlier", [spec_row("FR::OLD", FR_KEY)])
    refused = (
        RefusedDedupPair(
            first_key=other_key, second_key=FR_KEY, label="QualityRequirement", guard="numbers", name_cosine=0.97
        ),
        RefusedDedupPair(
            first_key=FR_KEY,
            second_key="requirement|span|third secret",
            label="FunctionalRequirement",
            guard="negation",
            name_cosine=0.9,
            rich_cosine=0.88,
        ),
    )
    await runner.store_artifact(ctx, "conform", TargetGraph(dedup_refused=refused))
    await runner.store_artifact(ctx, "diff", DiffArtifact(registry_rows=(spec_row("FR::NEW", other_key),)))

    result = await runner._finish(ctx, runner._RunStats())
    assert result.dedup_refused == (
        DedupRefusal(
            first_spec_id="FR::NEW",
            second_spec_id="FR::OLD",
            label="QualityRequirement",
            guard="numbers",
            name_cosine=0.97,
        ),
        DedupRefusal(
            first_spec_id="FR::OLD",
            second_spec_id=None,
            label="FunctionalRequirement",
            guard="negation",
            name_cosine=0.9,
            rich_cosine=0.88,
        ),
    )
    assert result.metrics.dedup_refused_by_guard == {"negation": 1, "numbers": 1}
    stored = await stores.builds.get_result(PROJECT, ctx.build_id or "")
    assert stored is not None and stored.dedup_refused == result.dedup_refused
    dumped = result.model_dump_json()
    assert not any(key in dumped for key in (FR_KEY, other_key, "secret", "first_key", "second_key"))


async def test_a_collapsed_refusal_names_its_nodes_spec_id_on_both_ends(stores: FileStores) -> None:
    """Review MEDIUM 3: a collapsed pair's ends are the node's key and an alias: both by the node's spec_id."""

    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    alias = "requirement|span|an alias sentence"
    survivor = TargetNode(
        identity_key=FR_KEY,
        identity_aliases=(alias,),
        label="QualityRequirement",
        name="Export completes quickly",
        evidence=(),
    )
    pair = RefusedDedupPair(
        first_key=alias,
        second_key=FR_KEY,
        label="QualityRequirement",
        guard="numbers",
        name_cosine=0.97,
        collapsed=True,
    )
    await stores.registry.record(PROJECT, "earlier", [spec_row("FR::OLD", FR_KEY)])
    await runner.store_artifact(ctx, "conform", TargetGraph(nodes=(survivor,), dedup_refused=(pair,)))
    result = await runner._finish(ctx, runner._RunStats())
    (refusal,) = result.dedup_refused
    assert (refusal.first_spec_id, refusal.second_spec_id, refusal.collapsed) == ("FR::OLD", "FR::OLD", True)
    assert alias not in result.model_dump_json()


async def test_a_conform_artifact_without_the_refused_list_reads_n_a(stores: FileStores) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    older = TargetGraph().model_dump(mode="json")
    del older["dedup_refused"]
    await stores.builds.put_artifact(PROJECT, ctx.build_id or "", "conform", older)
    result = await runner._finish(ctx, runner._RunStats())
    assert (result.dedup_refused, result.metrics.dedup_refused_by_guard) == ((), None)

    await runner.store_artifact(ctx, "conform", TargetGraph())
    result = await runner._finish(ctx, runner._RunStats())
    assert result.metrics.dedup_refused_by_guard == {}


def test_a_stored_result_without_quarantine_loads() -> None:
    stamp = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    payload = {"project_id": PROJECT, "build_id": "b", "mode": "first", "status": "succeeded"}
    result = BuildResult.model_validate(payload | {"started_at": stamp, "finished_at": stamp, "metrics": {}})
    assert result.quarantine == () and result.metrics.quarantine_by_reason is None
    assert result.dedup_refused == () and result.metrics.dedup_refused_by_guard is None


def test_identity_and_dedup_records_round_trip_and_older_artifacts_load_without_them() -> None:
    """every new field is defaulted, so artifacts written before them load."""

    resolved = ResolveArtifact(
        counters=ResolveCounters(consolidation=ConsolidationCounters(pair_calls=2, unsure_groups=1)),
        merges=(HubMerge(anchor_key="M|surface|a", member_key="M|surface|b", source="pair", confidence="high"),),
        pair_candidates=(
            PairCandidate(key_a="a", key_b="b", part="M", name_cos=0.7, rich_cos=0.9, source="both", outcome="same"),
            PairCandidate(key_a="a", key_b="c", part="M", name_cos=0.6, source="alias", outcome="over_cap"),
        ),
    )
    refused = RefusedDedupPair(
        first_key="r|span|a", second_key="r|span|b", label="R", guard="numbers", name_cosine=0.97
    )
    target = TargetGraph(counters=TargetCounters(dedup_unsure=1), dedup_refused=(refused,))
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    result = BuildResult(
        project_id=PROJECT,
        build_id="b",
        mode="first",
        status="succeeded",
        dedup_refused=(
            DedupRefusal(first_spec_id="R-1", second_spec_id="R-2", label="R", guard="numbers", name_cosine=0.97),
        ),
        metrics=BuildMetrics(dedup_refused_by_guard={"numbers": 1}),
        started_at=stamp,
        finished_at=stamp,
    )
    for model in (resolved, target, result):
        assert type(model).model_validate_json(model.model_dump_json()) == model

    older = {"canonical": [], "counters": {"consolidation": {"merges": 1}}}
    loaded = ResolveArtifact.model_validate(older)
    assert (loaded.merges, loaded.pair_candidates, loaded.counters.consolidation.decisions_expired) == ((), (), 0)
    stored = TargetGraph.model_validate({"nodes": [], "edges": [], "counters": {"dedup_merges": 1}, "quarantine": []})
    assert (stored.dedup_refused, stored.counters.dedup_candidates_rich_only) == ((), 0)


def test_edge_window_and_cross_document_records_round_trip_and_older_artifacts_load_without_them() -> None:
    """every new field is defaulted, so artifacts written before them load, and the new records round-trip."""

    edges = EdgesArtifact(
        counters=EdgeCounters(windows_called=2, windows_reused=1, windows_fallback=0, warm_ups=1),
        called_windows=(EdgeWindowCall(unit_id="u1", window=(0, 2), cause="dirty"),),
        catalog_parts={"u1": CatalogParts(mentioned=("c1",), heading=("c2",), near=(), referenced=("c3",))},
    )
    target = TargetGraph(
        counters=TargetCounters(
            derive_suppressed_by_edge=1,
            foreign_edges={"near": {"kept": 2, "quarantined:locality": 1}},
            oq_answer_judged=3,
            oq_partly_kept=1,
            oq_answered_dropped=1,
        ),
        answered_elsewhere=(AnsweredElsewhere(key="q|span|a", fragment="f9", quote="It is CSV."),),
    )
    item = JudgedItem(
        rule="R",
        kind="link",
        subject="M-1",
        source_spec_id="R-1",
        relation="X",
        target_spec_id="R-2",
        outcome="accepted",
        confidence=0.9,
        fragment_ids=("f1", "f7"),
    )
    report = InferenceReport(
        rules=(RuleReport(rule="R", items_by_outcome={"accepted": 1}),),
        judged_items=(item,),
    )
    diff_artifact = DiffArtifact(modified_causes={"links": 2, "statements": 1})
    metrics = VerificationMetrics(
        owners_by_source={"stated": 1, "implied": 0, "inferred": 0, "none": 2},
        cross_document_edges={"X": {"inferred": 1}},
        oq_answer_judged=3,
    )
    for model in (edges, target, report, diff_artifact, metrics):
        assert type(model).model_validate_json(model.model_dump_json()) == model

    older_edges = EdgesArtifact.model_validate({"raw_edges": [], "counters": {"calls": 1}})
    assert (
        older_edges.called_windows,
        older_edges.catalog_parts,
        older_edges.counters.windows_called,
    ) == (
        None,
        None,
        None,
    )
    older_target = TargetGraph.model_validate({"nodes": [], "edges": [], "counters": {}, "quarantine": []})
    counters = older_target.counters
    assert older_target.answered_elsewhere == ()
    assert (
        counters.derive_suppressed_by_edge,
        counters.foreign_edges,
        counters.oq_answer_judged,
    ) == (None,) * 3
    older_report = InferenceReport.model_validate({"rules": [{"rule": "R", "subjects": 1}]})
    assert (older_report.judged_items, older_report.rules[0].items_by_outcome) == (
        None,
        None,
    )
    assert DiffArtifact.model_validate({"operations": []}).modified_causes is None
    assert VerificationMetrics.model_validate({}).owners_by_source is None


async def test_the_verify_step_hands_verify_the_inference_report(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    report = InferenceReport(rules=())
    await runner.store_artifact(ctx, "infer", report)
    seen: dict[str, object] = {}

    async def verify_build(*_: object, **kwargs: object) -> VerificationReport:
        seen.update(kwargs)
        return VerificationReport()

    monkeypatch.setattr(runner.verification, "verify_build", verify_build)
    await runner.verify(ctx)
    assert seen["inference"] == report
