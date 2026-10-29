"""Build orchestration offline (build.md "Modes", "Steps" and "Reuse in update builds"; api.md "Build";
tech-specs 5.3; FR-BLD-03, 14, 15).

Mode selection, build records, failed and resumed builds of ``graph.build.run`` on file stores and an
``OfflineExecutor`` (canned presence rows, no Neo4j). Step functions are replaced by recording fakes
where a test is about orchestration only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from graph.build import STEPS, BuildContext, FileStores, open_file_stores, run_build, steps
from graph.build import extract as extraction
from graph.build import run as runner
from graph.build.conform import DedupMemory
from graph.build.consolidate import HUBS_PASS, HUBS_PROMPT, Decision
from graph.build.extract import node_fingerprint
from graph.build.models import (
    BuildResult,
    CanonicalNode,
    ChangeSet,
    DiffArtifact,
    EdgesArtifact,
    FragmentRec,
    InferenceReport,
    NodesArtifact,
    PersistCursor,
    PlanArtifact,
    ResolveArtifact,
    TargetGraph,
    Unit,
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
from graph.core.errors import ConfigurationError, ConformanceError
from graph.core.ontology import Ontology
from graph_support import (
    OfflineExecutor,
    ScriptedGateway,
    llm_config,
    shipped_ontology,
)

# The graph fixtures (scripted_gateway, make_gateway, gap_ontology, neo4j_project) live in graph_support.
pytest_plugins = ["graph_support"]

PROJECT = "pytest-graph-run"
PERSISTED = {READ_PERSISTED_PRESENCE.name: [{"documents": True, "spec_nodes": True}]}


class Empty(BaseModel):
    pass


def context(stores: FileStores, *, persisted: bool = False, **overrides: Any) -> BuildContext:
    values: dict[str, Any] = {
        "project_id": PROJECT,
        "ontology": shipped_ontology(),
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


# Mode selection (build.md "Modes")


async def test_a_project_without_persisted_graph_gets_a_first_build(stores: FileStores) -> None:
    ctx = context(stores)
    assert await runner.select_mode(ctx) == "first"
    build_id = await runner.start_build(ctx)
    record = await record_of(ctx, build_id)
    assert (record.mode, record.status, record.progress, record.finished) == ("first", "running", {}, False)
    assert record.options == ctx.options and record.label_set == ctx.ontology.labels
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


# Mode selection against newer full builds (build.md "Modes")

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
        "label_set": ctx.ontology.labels,
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


# Plan of update and rebuild builds (build.md Plan 4-5, "Modes"; FR-BLD-03)

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
    await stores.extraction.put("nodes", results.node_key(intro, intro.content_hash), fingerprint, {"nodes": []})
    await stores.extraction.put("nodes", results.node_key(export, export.content_hash), "older", {"nodes": []})
    await stores.extraction.put("nodes", results.node_key(audit, audit.content_hash), fingerprint, {"nodes": []})

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
            (runner.extraction, "extract_edges", EdgesArtifact()),
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
    await runner.decision_memory(context(stores)).write(
        HUBS_PASS, HUBS_PROMPT, {("a", "b"): Decision(same=True, label="Module")}, {}
    )
    dedup = DedupMemory(stores.extraction, PROJECT, llm_config().model_for("small"))
    await dedup.write({dedup.key("x", "y"): False})

    captured = Captured(monkeypatch)
    options = SMALL.model_copy(update={"hub_catalog": False})
    rebuild = await run_steps(
        context(stores, options=options, rebuild=True, gateway=gateway, executor=OfflineExecutor(rows))
    )
    nodes, resolve_kwargs, edges, conform_kwargs = (
        captured.kwargs[name] for name in ("extract_nodes", "resolve", "extract_edges", "conform")
    )
    assert nodes["results"].read is False and edges["results"].read is False
    assert nodes["known_terms_source"] == "none"  # no hub catalog, never the persisted hubs
    assert resolve_kwargs["persisted"] is None and conform_kwargs["persisted"] is None  # no persisted label
    decisions = await resolve_kwargs["memory"].pairs(HUBS_PASS, HUBS_PROMPT, [("b", "a")])
    assert decisions == {("a", "b"): Decision(same=True, label="Module")}
    assert await conform_kwargs["memory"].verdicts([("y", "x")]) == {dedup.key("x", "y"): False}
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
    assert captured.kwargs["extract_nodes"]["known_terms_source"] == "persisted"
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


# Extractor fingerprints (build.md "Reuse in update builds": a changed fingerprint re-extracts)


class PartOntology:
    """The shipped ontology with one prompt part's fingerprint replaced (all the fingerprints read)."""

    def __init__(self, part: str) -> None:
        self._ontology = shipped_ontology()
        self._part = part

    def fingerprint(self, part: str) -> str:
        value = self._ontology.fingerprint(part)
        return "edited-" + value if part == self._part else value


Fingerprints = dict[str, str]


def all_fingerprints(ontology: Any = None, config: Any = None, options: BuildOptions | None = None) -> Fingerprints:
    ontology = ontology or shipped_ontology()
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
        ("NODES_PROMPT_VERSION", {"nodes"}),
        ("NODES_PROMPT", {"nodes"}),
        ("EDGES_PROMPT_VERSION", {"edges"}),
        ("EDGES_PROMPT", {"edges"}),
        ("ATTRIBUTES_PROMPT", {"attributes"}),
    ],
)
def test_a_prompt_version_changes_only_its_fingerprint(
    constant: str, kinds: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = all_fingerprints()
    monkeypatch.setattr(extraction, constant, getattr(extraction, constant) + "-next")
    after = all_fingerprints()
    assert {kind for kind in before if before[kind] != after[kind]} == kinds


def test_the_graphiti_version_changes_the_node_and_edge_fingerprints(monkeypatch: pytest.MonkeyPatch) -> None:
    """The node and edge calls go through graphiti-core's prompts and parsing; attributes do not."""

    before = all_fingerprints()
    monkeypatch.setattr(extraction, "graphiti_version", lambda: "0.0.0-other")
    after = all_fingerprints()
    assert {kind for kind in before if before[kind] != after[kind]} == {"nodes", "edges"}


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


# Edge fallback of a failed edge call (build.md "Failures"; FR-BLD-12)


def edge_item(relation: str, source: str, target: str, positions: list[int], fragments: list[str]) -> dict[str, Any]:
    return {
        "relation": relation,
        "source": source,
        "target": target,
        "fact": f"{source} {relation} {target}",
        "positions": positions,
        "fragments": fragments,
    }


async def test_a_failed_edge_unit_falls_back_to_its_last_edges_by_content_then_by_identity(
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

    cid_of = extraction._cids_by_key(
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

    async def fallback() -> list[Any]:
        found = await extraction._fallback_edges(results, fingerprint, [unit], fragments_by_id, cid_of)
        return found.get("u", [])

    assert await fallback() == []  # nothing stored: no fallback
    # stored under the identity of the id the first fragment replaces (or another fingerprint: unusable)
    await stores.extraction.put("edges", results.unit_edge_key("a.md", "a.md#f0"), "other-fp", by_identity)
    assert await fallback() == []
    await stores.extraction.put("edges", results.unit_edge_key("a.md", "a.md#f0"), fingerprint, by_identity)
    edges = await fallback()
    # endpoints mapped by identity key or alias; fragments by id (a replaced id to its successor,
    # unknown ids dropped); an edge without both ends, or between one node, left out
    assert [(e.relation, e.source_cid, e.target_cid, e.fragment_ids, e.reused, e.unit_id) for e in edges] == [
        ("PART_OF", "c3", "c1", (f1.fragment_id,), True, "u"),
        ("INTEGRATES_WITH", "c1", "c2", (f2.fragment_id, f1.fragment_id), True, "u"),
    ]
    assert edges[0].fact == "Feature|surface|f PART_OF Module|surface|a"

    # the unit's own identity (document and current first fragment) comes before the replaced id
    own = {"edges": [edge_item("PART_OF", "Feature|surface|f", "Module|surface|b", [1], [f1.fragment_id])], "near": []}
    await stores.extraction.put("edges", results.unit_edge_key("a.md", f1.fragment_id), fingerprint, own)
    assert [(e.source_cid, e.target_cid, e.fragment_ids) for e in await fallback()] == [("c3", "c2", (f1.fragment_id,))]

    # the same content wins over any identity answer; its fragments come by position
    same = {"edges": [edge_item("INTEGRATES_WITH", "Module|surface|b", "Module|surface|a", [1, 5], ["x"])], "near": []}
    await stores.extraction.put("edges", results.last_edge_key("unit-now"), fingerprint, same)
    assert [(e.source_cid, e.target_cid, e.fragment_ids) for e in await fallback()] == [("c2", "c1", (f2.fragment_id,))]


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
