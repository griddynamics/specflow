"""Settlement, follow-up builds and building status offline."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from graph.build import (
    MAX_FOLLOW_UPS,
    STEPS,
    BuildContext,
    FileStores,
    open_file_stores,
    project_status,
    run_build,
)
from graph.build import run as runner
from graph.build.models import (
    BacklogQueue,
    BuildResult,
    ChangeSet,
    FragmentRec,
    InferenceReport,
    NodesArtifact,
    PersistCursor,
    PlanArtifact,
    RuleReport,
    Settlement,
    StepName,
    VerificationReport,
)
from graph.build.ports import BuildRecord
from graph.build.queries import READ_PERSISTED_PRESENCE
from graph.core.errors import ConformanceError
from graph_support import OfflineExecutor, ScriptedGateway, frozen_ontology

pytest_plugins = ["graph_support"]

PROJECT = "pytest-graph-settlement"
PRESENT = [{"documents": True, "spec_nodes": True}]


def context(stores: FileStores, **overrides: Any) -> BuildContext:
    values: dict[str, Any] = {
        "project_id": PROJECT,
        "ontology": frozen_ontology(),
        "driver": None,
        "gateway": ScriptedGateway(),
        "registry": stores.registry,
        "corpus": stores.corpus,
        "extraction": stores.extraction,
        "builds": stores.builds,
        "executor": OfflineExecutor(),
    }
    values.update(overrides)
    return BuildContext(**values)


@pytest.fixture
def stores(tmp_path: Path) -> FileStores:
    return open_file_stores(tmp_path / "stores", corpus_folder=tmp_path / "corpus")


def fragment(document: str, name: str) -> FragmentRec:
    return FragmentRec(
        fragment_id=f"{document}#{name}",
        document_id=document,
        heading_path=(name,),
        ordinal=1,
        line_start=1,
        line_end=1,
        text=name,
        content_hash=name,
        tokens=1,
    )


PLAN = PlanArtifact(
    mode="first",
    change_set=ChangeSet(),
    documents=(),
    fragments=(fragment("a.md", "f1"), fragment("b.md", "f1")),
    dirty_ids=("a.md#f1", "b.md#f1"),
)


RETRYABLE = "llm_exhausted"


class Outcome:
    """What one build of a chain leaves: skipped fragments (with their error code: ``codes``, else
    ``RETRYABLE``), Verify follow-up lineages, queue, rule error."""

    def __init__(
        self,
        *,
        skipped: tuple[str, ...] = (),
        codes: dict[str, str] | None = None,
        follow_ups: tuple[str, ...] = (),
        backlog: dict[str, BacklogQueue] | None = None,
        rule_error: str | None = None,
        fail: StepName | None = None,
    ) -> None:
        self.skipped = {fragment_id: (codes or {}).get(fragment_id, RETRYABLE) for fragment_id in skipped}
        self.follow_ups = follow_ups
        self.backlog = backlog
        self.rule_error = rule_error
        self.fail = fail


class ChainSteps:
    """Fake step functions: the n-th build (plan call) of the test leaves ``outcomes[n]`` (the last one
    for any later build) and records the context it ran with."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *outcomes: Outcome) -> None:
        self.outcomes = outcomes
        self.contexts: list[BuildContext] = []
        for step in STEPS:
            monkeypatch.setitem(runner.STEP_FUNCTIONS, step, self._step(step))

    def outcome(self) -> Outcome:
        return self.outcomes[min(len(self.contexts), len(self.outcomes)) - 1]

    def _step(self, step: StepName) -> Callable[[BuildContext], Any]:
        async def run(ctx: BuildContext) -> None:
            async with runner.step_progress(ctx, step):
                if step == "plan":
                    self.contexts.append(ctx)
                    await runner.store_artifact(ctx, "plan", PLAN)
                outcome = self.outcome()
                if outcome.fail == step:
                    outcome.fail = None
                    raise ConformanceError("scripted_failure")
                if step == "extract_nodes":
                    await runner.store_artifact(ctx, step, NodesArtifact(skipped=outcome.skipped))
                elif step == "persist":
                    await runner.store_artifact(ctx, step, PersistCursor(done=True))
                    if isinstance(ctx.executor, OfflineExecutor):
                        ctx.executor.rows[READ_PERSISTED_PRESENCE.name] = PRESENT
                elif step == "infer":
                    rules = (RuleReport(rule="INF-A", error_code=outcome.rule_error),)
                    await runner.store_artifact(ctx, step, InferenceReport(rules=rules, backlog=outcome.backlog))
                elif step == "verify":
                    report = VerificationReport(ok=not outcome.follow_ups, follow_up_lineages=outcome.follow_ups)
                    await runner.store_artifact(ctx, step, report)

        return run


async def record_of(stores: FileStores, build_id: str) -> BuildRecord:
    record = await stores.builds.get_build(PROJECT, build_id)
    assert record is not None
    return record


async def result_of(stores: FileStores, build_id: str) -> BuildResult:
    result = await stores.builds.get_result(PROJECT, build_id)
    assert result is not None
    return result


# Settlement


async def test_a_build_with_nothing_left_is_settled(stores: FileStores, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = ChainSteps(monkeypatch, Outcome())
    result = await run_build(context(stores))
    assert len(fakes.contexts) == 1
    assert result.settled
    assert result.settlement == Settlement(state="settled")
    assert (await result_of(stores, result.build_id)).settlement == result.settlement


async def test_the_settlement_names_every_reason_and_the_documents_left(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    always = Outcome(
        skipped=("a.md#f1",),
        follow_ups=("b.md",),
        backlog={"INF-A": BacklogQueue(spec_ids=("S-1", "S-2")), "INF-B": BacklogQueue()},
        rule_error="llm_refused",
    )
    ChainSteps(monkeypatch, always)
    result = await run_build(context(stores))
    assert result.settlement is not None
    assert result.settlement.state == "not_settled"
    assert result.settlement.reasons == ("documents_incomplete", "inference_backlog", "inference_rule_failed")
    assert result.settlement.documents == ("a.md", "b.md")
    assert result.settlement.document_codes == {"a.md": (RETRYABLE,), "b.md": ("fragment_state",)}
    assert result.settlement.inference_backlog == {"INF-A": 2}  # an empty queue is left out
    assert (result.status, result.settled) == ("partially_succeeded", False)


async def test_an_inference_backlog_alone_starts_no_follow_up_and_keeps_settled(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = ChainSteps(monkeypatch, Outcome(backlog={"INF-A": BacklogQueue(spec_ids=("S-1",))}))
    result = await run_build(context(stores))
    assert len(fakes.contexts) == 1
    assert result.settled  # nothing to retry: settled keeps its meaning
    assert result.settlement == Settlement(
        state="not_settled", reasons=("inference_backlog",), inference_backlog={"INF-A": 1}
    )


async def test_an_inference_report_without_a_queue_reads_no_backlog(stores: FileStores) -> None:
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    await runner.store_artifact(ctx, "infer", InferenceReport(rules=(RuleReport(rule="INF-A"),)))
    await stores.builds.put_progress(PROJECT, ctx.build_id or "", "infer", "done")
    result = await runner._finish(ctx, runner._RunStats())
    assert result.settlement == Settlement(state="settled")


async def test_a_failed_build_has_no_settlement(stores: FileStores, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = ChainSteps(monkeypatch, Outcome(skipped=("a.md#f1",), fail="resolve"))
    with pytest.raises(ConformanceError):
        await run_build(context(stores))
    assert len(fakes.contexts) == 1  # a failed build starts no follow-up
    record = await stores.builds.latest_build(PROJECT)
    assert record is not None
    stored = await result_of(stores, record.build_id)
    assert (stored.status, stored.settlement) == ("failed", None)


# Follow-up builds


async def test_a_build_failing_once_is_recovered_by_its_first_follow_up(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = ChainSteps(monkeypatch, Outcome(skipped=("a.md#f1",)), Outcome())
    result = await run_build(context(stores))
    first, follow_up = (ctx.build_id or "" for ctx in fakes.contexts)
    assert (result.build_id, result.mode, result.status) == (follow_up, "update", "succeeded")
    assert result.settlement == Settlement(state="settled", follow_up_builds=(follow_up,))
    # the follow-up retries the lineage of the skipped fragment, same settings, no rebuild
    retry = fakes.contexts[1]
    assert (retry.retry_lineages, retry.rebuild, retry.settings, retry.options) == (
        ("a.md",),
        False,
        fakes.contexts[0].settings,
        fakes.contexts[0].options,
    )
    # the records are linked and the first result stays stored as it finished
    record = await record_of(stores, follow_up)
    assert (record.follow_up_of, record.follow_up_attempt) == (first, 1)
    started = await record_of(stores, first)
    assert (started.follow_up_of, started.follow_up_attempt) == (None, 0)
    stored = await result_of(stores, first)
    assert stored.status == "partially_succeeded"
    assert stored.settlement == Settlement(
        state="not_settled",
        reasons=("documents_incomplete",),
        documents=("a.md",),
        document_codes={"a.md": (RETRYABLE,)},
    )
    assert await result_of(stores, follow_up) == result


async def test_a_lineage_failing_the_same_way_stops_the_chain_after_one_follow_up(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow-up recovered nothing (same documents, same skipped fragments and codes): no second
    follow-up; the lineage stays listed with its code."""

    fakes = ChainSteps(monkeypatch, Outcome(skipped=("a.md#f1",), follow_ups=("b.md",)))
    result = await run_build(context(stores))
    assert len(fakes.contexts) == 2
    first, follow_up = (ctx.build_id or "" for ctx in fakes.contexts)
    assert result.build_id == follow_up
    assert result.settlement == Settlement(
        state="not_settled",
        reasons=("documents_incomplete",),
        documents=("a.md", "b.md"),
        document_codes={"a.md": (RETRYABLE,), "b.md": ("fragment_state",)},
        follow_up_builds=(follow_up,),
    )
    assert (await record_of(stores, follow_up)).follow_up_of == first


@pytest.mark.parametrize("code", sorted(runner.NON_RETRYABLE_CODES))
async def test_a_non_retryable_code_starts_no_follow_up(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    fakes = ChainSteps(monkeypatch, Outcome(skipped=("a.md#f1",), codes={"a.md#f1": code}))
    result = await run_build(context(stores))
    assert len(fakes.contexts) == 1
    assert result.settlement == Settlement(
        state="not_settled", reasons=("documents_incomplete",), documents=("a.md",), document_codes={"a.md": (code,)}
    )


async def test_a_follow_up_retries_only_the_retryable_lineages(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = Outcome(skipped=("a.md#f1", "b.md#f1"), codes={"a.md#f1": "llm_refused"})
    fakes = ChainSteps(monkeypatch, refused, Outcome(skipped=("a.md#f1",), codes={"a.md#f1": "llm_refused"}))
    result = await run_build(context(stores))
    assert [ctx.retry_lineages for ctx in fakes.contexts] == [(), ("b.md",)]
    assert result.settlement is not None
    assert (result.settlement.documents, result.settlement.document_codes) == (("a.md",), {"a.md": ("llm_refused",)})


async def test_a_chain_that_keeps_recovering_runs_at_most_two_follow_ups(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = ChainSteps(
        monkeypatch,
        Outcome(skipped=("a.md#f1", "b.md#f1")),
        Outcome(skipped=("a.md#f1",), follow_ups=("b.md",)),
        Outcome(skipped=("a.md#f1",)),
    )
    result = await run_build(context(stores))
    assert MAX_FOLLOW_UPS == 2
    assert len(fakes.contexts) == 1 + MAX_FOLLOW_UPS
    first, *follow_ups = (ctx.build_id or "" for ctx in fakes.contexts)
    assert result.build_id == follow_ups[-1]
    assert result.settlement == Settlement(
        state="not_settled",
        reasons=("documents_incomplete",),
        documents=("a.md",),
        document_codes={"a.md": (RETRYABLE,)},
        follow_up_builds=tuple(follow_ups),
    )
    assert [ctx.retry_lineages for ctx in fakes.contexts] == [(), ("a.md", "b.md"), ("a.md", "b.md")]
    for attempt, build_id in enumerate(follow_ups, start=1):
        record = await record_of(stores, build_id)
        assert (record.mode, record.follow_up_of, record.follow_up_attempt, record.finished) == (
            "update",
            first,
            attempt,
            True,
        )
    # each build of the chain keeps its own result; an earlier one lists the follow-ups before it
    middle = await result_of(stores, follow_ups[0])
    assert middle.settlement is not None and middle.settlement.follow_up_builds == (follow_ups[0],)


async def test_a_rebuild_s_follow_ups_are_updates_without_rebuild(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = ChainSteps(monkeypatch, Outcome(skipped=("b.md#f1",)), Outcome())
    result = await run_build(context(stores, rebuild=True))
    assert [ctx.rebuild for ctx in fakes.contexts] == [True, False]
    modes = [(await record_of(stores, ctx.build_id or "")).mode for ctx in fakes.contexts]
    assert modes == ["rebuild", "update"]
    assert result.settlement is not None and result.settlement.state == "settled"


async def test_a_failing_follow_up_raises_and_resumes_its_chain(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = ChainSteps(
        monkeypatch, Outcome(skipped=("a.md#f1", "b.md#f1")), Outcome(skipped=("a.md#f1",), fail="resolve")
    )
    ctx = context(stores)
    with pytest.raises(ConformanceError):
        await run_build(ctx)
    first, follow_up = (each.build_id or "" for each in fakes.contexts)
    failed = await record_of(stores, follow_up)
    assert (failed.status, failed.follow_up_of, failed.follow_up_attempt) == ("failed", first, 1)
    assert (await result_of(stores, first)).status == "partially_succeeded"  # stays stored

    # resumed, the follow-up finishes and continues its chain up to the bound
    resumed = await run_build(ctx.with_build_id(follow_up))
    assert len(fakes.contexts) == 3  # attempt 1 resumed (no new plan), then attempt 2
    last = fakes.contexts[-1].build_id or ""
    assert (await record_of(stores, last)).follow_up_attempt == 2
    assert resumed.settlement is not None
    assert resumed.settlement.follow_up_builds == (follow_up, last)
    assert resumed.settlement.documents == ("a.md",)


async def test_a_resumed_second_follow_up_lists_the_whole_chain(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = ChainSteps(
        monkeypatch,
        Outcome(skipped=("a.md#f1", "b.md#f1")),
        Outcome(skipped=("a.md#f1",)),
        Outcome(fail="conform"),
    )
    ctx = context(stores)
    with pytest.raises(ConformanceError):
        await run_build(ctx)
    _, first_follow_up, second = (each.build_id or "" for each in fakes.contexts)
    resumed = await run_build(ctx.with_build_id(second))
    assert (resumed.build_id, resumed.status) == (second, "succeeded")
    assert resumed.settlement == Settlement(state="settled", follow_up_builds=(first_follow_up, second))


async def test_a_finished_build_whose_follow_up_never_started_starts_it_when_resumed(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process stopped between finishing the first build and starting its follow-up (no follow-up
    allowed here): resumed, the finished build starts it; resumed again once the chain went on, it
    returns its stored result."""

    fakes = ChainSteps(monkeypatch, Outcome(skipped=("a.md#f1",)), Outcome())
    ctx = context(stores)
    monkeypatch.setattr(runner, "MAX_FOLLOW_UPS", 0)
    head = await run_build(ctx)
    assert len(fakes.contexts) == 1 and head.settlement is not None and head.settlement.documents == ("a.md",)
    monkeypatch.setattr(runner, "MAX_FOLLOW_UPS", 2)
    resumed = await run_build(ctx.with_build_id(head.build_id))
    assert len(fakes.contexts) == 2
    follow_up = fakes.contexts[1].build_id or ""
    assert (resumed.build_id, resumed.settlement) == (
        follow_up,
        Settlement(state="settled", follow_up_builds=(follow_up,)),
    )
    assert (await record_of(stores, follow_up)).follow_up_of == head.build_id
    assert await run_build(ctx.with_build_id(head.build_id)) == await result_of(stores, head.build_id)
    assert len(fakes.contexts) == 2


async def test_a_build_run_step_by_step_starts_its_follow_ups_when_run_build_finishes_it(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``examples/graph_eval.py`` runs the steps itself, then finishes through ``run_build``."""

    fakes = ChainSteps(monkeypatch, Outcome(follow_ups=("a.md",)), Outcome())
    ctx = context(stores)
    ctx = ctx.with_build_id(await runner.start_build(ctx))
    for step in STEPS:
        await runner.STEP_FUNCTIONS[step](ctx)
    last = await run_build(ctx)
    assert last.build_id == fakes.contexts[1].build_id
    assert last.settlement is not None and last.settlement.follow_up_builds == (last.build_id,)
    stepped = await result_of(stores, ctx.build_id or "")
    assert stepped.settlement is not None and stepped.settlement.documents == ("a.md",)


# Building status


async def running_record(stores: FileStores, build_id: str, minutes: int, **overrides: Any) -> BuildRecord:
    """A running record started ``minutes`` from now (after every build the test ran)."""

    ctx = context(stores)
    values: dict[str, Any] = {
        "build_id": build_id,
        "project_id": PROJECT,
        "mode": "update",
        "options": ctx.options,
        "ontology_fingerprint": runner.ontology_fingerprint(ctx.ontology),
        "label_set": ctx.ontology.labels,
        "embedding_model": ctx.gateway.config.embedding_model,
        "started_at": datetime.now(UTC) + timedelta(minutes=minutes),
        **overrides,
    }
    record = BuildRecord(**values)
    await stores.builds.create_build(record)
    return record


async def test_a_project_without_builds_is_not_building(stores: FileStores) -> None:
    status = await project_status(stores.builds, PROJECT)
    assert (status.project_id, status.building, status.build_id, status.settlement) == (PROJECT, False, None, None)


async def test_the_status_of_a_finished_chain_is_not_building_with_the_last_settlement(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    ChainSteps(monkeypatch, Outcome(skipped=("a.md#f1",)), Outcome())
    result = await run_build(context(stores))
    status = await project_status(stores.builds, PROJECT)
    assert (status.building, status.step, status.follow_up_attempt) == (False, None, 0)
    assert status.settlement == result.settlement


async def test_a_running_follow_up_reads_building_with_its_step_and_attempt(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    ChainSteps(monkeypatch, Outcome(backlog={"INF-A": BacklogQueue(spec_ids=("S-1",))}))
    finished = await run_build(context(stores))
    record = await running_record(
        stores,
        "follow",
        60,
        follow_up_of=finished.build_id,
        follow_up_attempt=1,
        progress={"plan": "done", "extract_nodes": "running"},
    )
    status = await project_status(stores.builds, PROJECT)
    assert (status.building, status.build_id, status.mode, status.step) == (True, "follow", "update", "extract_nodes")
    assert (status.started_at, status.follow_up_attempt) == (record.started_at, 1)
    assert status.settlement == finished.settlement  # the latest finished build's

    # between two steps: the next step to run
    await stores.builds.put_progress(PROJECT, "follow", "extract_nodes", "done")
    assert (await project_status(stores.builds, PROJECT)).step == "resolve"


async def test_an_interrupted_build_reads_running_until_finished_failed(
    stores: FileStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    ChainSteps(monkeypatch, Outcome(), Outcome(fail="conform"))
    ctx = context(stores)
    finished = await run_build(ctx)
    # a build that failed is not building; the settlement stays the latest finished build's
    with pytest.raises(ConformanceError):
        await run_build(ctx)
    failed = await project_status(stores.builds, PROJECT)
    assert (failed.building, failed.build_id, failed.settlement) == (False, None, finished.settlement)
    # a record whose process died stays running: building (judging it stale is the host's call)
    await running_record(stores, "died", 60, progress={"plan": "done", "resolve": "running"})
    died = await project_status(stores.builds, PROJECT)
    assert (died.building, died.build_id, died.step, died.settlement) == (True, "died", "resolve", finished.settlement)
