"""Offline tests of inference limits by mode, the persisted queue, drains and chunked judge memory."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast, override

import pytest

from graph.build import infer as inference
from graph.build import infer_bound
from graph.build.files import FileBuildStore, FileExtractionStore
from graph.build.infer_bound import Bounded, RuleLimits
from graph.build.models import BacklogQueue, BuildResult, InferenceReport
from graph.build.options import BuildOptions
from graph.build.ports import BuildRecord
from graph.core.db import Executor
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm import JudgeRunner
from graph.core.llm.gateway import request_tokens
from graph.core.ontology import InferenceRule, InferenceSpec, Ontology
from graph_support import ScriptedGateway
from test_graph_infer import (
    CONT1,
    MODEL,
    STAMP,
    V2,
    XDOC,
    FakeGraph,
    attach_to,
    scripted,
    subject_of,
    with_billing_hub,
    xdoc_answer,
    xdoc_graph,
)

QUEUE_ORDER = ("X::FR-2", "X::FR-3", "X::FR-4")


# The pure bound


def test_limits_by_mode_keep_the_cap_and_add_first_cap_and_budget_in_full_builds() -> None:
    spec = V2.inference
    assert infer_bound.limits_of(spec, CONT1, first=False) == RuleLimits(100)
    assert infer_bound.limits_of(spec, CONT1, first=True) == RuleLimits(100, 2000, 11_000_000)
    assert infer_bound.limits_of(spec, XDOC, first=False) == RuleLimits(36)
    assert infer_bound.limits_of(spec, XDOC, first=True) == RuleLimits(36, 2000, 11_000_000)


@pytest.mark.parametrize(
    ("limits", "position", "tokens", "spent", "expected"),
    [
        (RuleLimits(2), 1, 10, 0, None),
        (RuleLimits(2), 2, 10, 0, "cap"),
        (RuleLimits(2, 4, 100), 1, 10, 500, None),  # the cap is kept when the budget is spent
        (RuleLimits(2, 4, 100), 3, 10, 90, None),
        (RuleLimits(2, 4, 100), 3, 11, 90, "budget"),
        (RuleLimits(2, 4, 100), 4, 1, 0, "first_cap"),
        (RuleLimits(2, 2, 100), 2, 1, 0, "cap"),  # first_cap no larger than the cap
        (RuleLimits(2, 4), 3, 10**9, 10**9, None),  # no budget
    ],
)
def test_stop_names_the_limit_that_stops_a_rule(
    limits: RuleLimits, position: int, tokens: int, spent: int, expected: str | None
) -> None:
    assert infer_bound.stop(limits, position, tokens, spent) == expected


def test_bound_stops_at_the_first_subject_that_does_not_fit_counting_earlier_rules() -> None:
    assert infer_bound.bound([10] * 5, RuleLimits(2, 4, 35)) == Bounded(3, 30, "budget")
    assert infer_bound.bound([10, 1, 1], RuleLimits(1, 3, 15), spent=20) == Bounded(1, 30, "budget")
    assert infer_bound.bound([10] * 3, RuleLimits(5)) == Bounded(3, 30, None)


@pytest.mark.parametrize(
    ("changed", "queued", "update", "expected"),
    [
        (8, "abc", True, [0, 1, 2, 3, 4, 5, 6, "a", "b", "c", 7, "x"]),  # 5 kept, the queue fills 3
        (8, "abc", False, [0, 1, 2, 3, 4, 5, 6, 7, "a", "b", "c", "x"]),  # no reservation outside updates
        (5, "abc", True, [0, 1, 2, 3, 4, "a", "b", "c", "x"]),  # (1) holds half the cap, not more
        (8, "", True, [0, 1, 2, 3, 4, 5, 6, 7, "x"]),
        (12, "abcdefg", True, [0, 1, 2, 3, 4, "a", "b", "c", "d", "e", "f", "g", *range(5, 12), "x"]),
    ],
)
def test_an_update_reserves_half_the_cap_for_the_queue_when_changed_subjects_exceed_it(
    changed: int, queued: str, *, update: bool, expected: list[Any]
) -> None:
    order = infer_bound.judging_order(list(range(changed)), list(queued), ["x"], cap=10, update=update)
    assert order == expected


def test_the_queue_puts_failed_subjects_first_and_ages() -> None:
    assert infer_bound.next_queue([], [], None, build_id="b-2") is None
    fresh = infer_bound.next_queue(["s2", "s1"], ["s1"], None, build_id="b-2")
    assert fresh == BacklogQueue(spec_ids=("s1", "s2"), since_build="b-2", age_builds=1)
    older = infer_bound.next_queue(["s3"], [], fresh, build_id="b-3")
    assert older == BacklogQueue(spec_ids=("s3",), since_build="b-2", age_builds=2)
    assert infer_bound.carried(older) == BacklogQueue(spec_ids=("s3",), since_build="b-2", age_builds=3)


# infer within the limits


def orphans(count: int) -> FakeGraph:
    """``count`` orphan requirements fr-1.. (``X::FR-1``..) of equal size, each with candidate feat-1."""

    graph = FakeGraph().node("feat-1", "Feature")
    for index in range(1, count + 1):
        graph.node(f"fr-{index}", "FunctionalRequirement")
        graph.similar[f"fr-{index}"] = ["feat-1"]
    return graph


def bounded(
    *rules: InferenceRule, cap: int = 1, first_cap: int | None = None, first_budget: int | None = None
) -> Ontology:
    """V2 with ``rules`` (default: INF-CONT-01 with ``cap``) and the given ``first_cap`` and ``first_budget``."""

    chosen = rules or (dataclasses.replace(CONT1, cap=cap),)
    spec = InferenceSpec(
        cap=V2.inference.cap,
        floor=V2.inference.floor,
        rules=chosen,
        first_cap=first_cap,
        first_budget=first_budget,
    )
    return dataclasses.replace(V2, inference=spec)


def declining(count: int, **overrides: Any) -> ScriptedGateway:
    answers = {(CONT1.prompt, f"fr-{index}"): attach_to(None) for index in range(1, count + 1)}
    answers.update({(CONT1.prompt, name): value for name, value in overrides.items()})
    return scripted(answers)


async def build(
    graph: FakeGraph,
    ontology: Ontology,
    gateway: ScriptedGateway,
    *,
    memory: inference.JudgeMemory | None = None,
    changes: inference.BuildChanges | None = None,
    build_id: str = "b-1",
) -> InferenceReport:
    return await inference.infer(
        cast(Executor, graph),
        "p-1",
        ontology,
        JudgeRunner(gateway, ontology),
        stamp=dataclasses.replace(STAMP, build_id=build_id),
        memory=memory,
        changes=changes,
    )


def called(gateway: ScriptedGateway, rule: InferenceRule = CONT1) -> list[Any]:
    return [call.key for call in gateway.calls_for(rule.prompt)]


async def test_limits_per_mode_with_input_counted_before_each_call() -> None:
    """Four equal orphans, cap 1. An update stops at the cap; a first build goes on to first_cap, or
    while the build's counted input (the rendered messages, as sent) fits the budget: the subject
    past the cap that does not fit is not called and stops the rule."""

    gateway = declining(4)
    (rule,) = (await build(orphans(4), bounded(first_cap=4), gateway)).rules
    assert (rule.judged, rule.limit, rule.backlog) == (1, "cap", 3)
    tokens = request_tokens(gateway.calls_for(CONT1.prompt)[0].messages)
    assert rule.budget_tokens == tokens > 0

    first = inference.BuildChanges(first=True)
    (rule,) = (await build(orphans(4), bounded(first_cap=3), declining(4), changes=first)).rules
    assert (rule.judged, rule.limit, rule.deferred) == (3, "first_cap", 1)
    (rule,) = (await build(orphans(4), bounded(first_cap=4), declining(4), changes=first)).rules
    assert (rule.judged, rule.limit, rule.budget_tokens) == (4, None, 4 * tokens)

    gateway = declining(4)
    report = await build(orphans(4), bounded(first_cap=4, first_budget=3 * tokens - 1), gateway, changes=first)
    (rule,) = report.rules
    assert called(gateway) == ["fr-1", "fr-2"]
    assert (rule.judged, rule.limit, rule.budget_tokens) == (2, "budget", 2 * tokens)
    assert report.backlog == {CONT1.id: BacklogQueue(spec_ids=("X::FR-3", "X::FR-4"), since_build="b-1", age_builds=1)}

    gateway = declining(4)  # a budget smaller than one call: the cap is kept all the same
    (rule,) = (await build(orphans(4), bounded(first_cap=4, first_budget=1), gateway, changes=first)).rules
    assert called(gateway) == ["fr-1"] and (rule.judged, rule.limit) == (1, "budget")


async def test_the_budget_is_the_builds_across_rules() -> None:
    """The second rule's past-cap subject does not fit once the first rule spent the budget."""

    other = dataclasses.replace(CONT1, id="INF-CONT-09", cap=1)
    ontology = bounded(dataclasses.replace(CONT1, cap=1), other, first_cap=4)
    gateway = declining(2)
    report = await build(orphans(2), ontology, gateway, changes=inference.BuildChanges(first=True))
    tokens = (report.rules[0].budget_tokens or 0) // 2
    assert tokens > 0
    gateway = declining(2)
    ontology = bounded(dataclasses.replace(CONT1, cap=1), other, first_cap=4, first_budget=3 * tokens)
    report = await build(orphans(2), ontology, gateway, changes=inference.BuildChanges(first=True))
    assert [(rule.judged, rule.limit) for rule in report.rules] == [(2, None), (1, "budget")]


async def test_a_no_change_build_drains_the_queue_then_never_judged_subjects() -> None:
    """Nothing changed: the queue goes first (a queued subject that is no subject any more drops
    out), then never-judged ones, under the cap; the queue keeps its age."""

    previous = {CONT1.id: BacklogQueue(spec_ids=("X::FR-3", "X::FR-GONE"), since_build="b-0", age_builds=2)}
    gateway = declining(4)
    changes = inference.BuildChanges(graph_changed=False, backlog=previous)
    report = await build(orphans(4), bounded(cap=2), gateway, changes=changes)
    (rule,) = report.rules
    assert called(gateway) == ["fr-3", "fr-1"]
    assert (rule.judged, rule.deferred, rule.limit, rule.backlog) == (2, 2, "cap", 2)
    assert report.backlog == {CONT1.id: BacklogQueue(spec_ids=("X::FR-2", "X::FR-4"), since_build="b-0", age_builds=3)}
    assert report.backlog_counts() == {CONT1.id: 2}


async def test_a_failed_judgement_goes_to_the_head_of_the_queue() -> None:
    gateway = declining(3, **{"fr-1": LLMError("exhausted")})
    report = await build(orphans(3), bounded(cap=2), gateway)
    (rule,) = report.rules
    assert (rule.failed, rule.judged, rule.deferred) == (1, 1, 1)
    assert report.backlog is not None and report.backlog[CONT1.id].spec_ids == ("X::FR-1", "X::FR-3")


async def test_an_xdoc_update_judges_its_scope_then_drains_its_queue_only() -> None:
    """Two never-judged hubs: "Orders service" (5 members) and "Billing service" (queued). An update
    with no member change judges the queued hub only; one where a member joined "Orders service"
    judges it first, then the queue under the cap."""

    def graph() -> FakeGraph:
        return with_billing_hub(xdoc_graph(hub_attributes={"type": "backend"}), {"fr-f": ["d1"], "fr-g": ["d2"]})

    answers = {(XDOC.prompt, "Orders service"): xdoc_answer(), (XDOC.prompt, "Billing service"): xdoc_answer()}
    queue = {XDOC.id: BacklogQueue(spec_ids=("X::MOD-TWO",), since_build="b-0", age_builds=1)}
    joined = inference.UpdateScope(added=frozenset({"X::FR-A"}), changed=frozenset({"X::FR-A"}))

    async def update(scope: inference.UpdateScope, cap: int) -> tuple[list[Any], InferenceReport]:
        gateway = scripted(answers)
        changes = inference.BuildChanges(update=scope, backlog=queue)
        report = await build(graph(), bounded(dataclasses.replace(XDOC, cap=cap)), gateway, changes=changes)
        return called(gateway, XDOC), report

    calls, report = await update(inference.UpdateScope(), 36)
    assert calls == ["Billing service"] and report.backlog == {}
    assert (report.rules[0].deferred, report.rules[0].limit) == (1, None)  # "Orders service": out of scope, not queued
    calls, report = await update(joined, 1)
    assert calls == ["Orders service"] and report.rules[0].limit == "cap"
    assert report.backlog == {XDOC.id: BacklogQueue(spec_ids=("X::MOD-TWO",), since_build="b-0", age_builds=2)}
    calls, _ = await update(joined, 2)
    assert calls == ["Orders service", "Billing service"]


# Judge memory per chunk


class _Recording(inference.JudgeMemory):
    """Judge memory that records each write (its keys) in ``events``, next to the swap."""

    def __init__(self, store: FileExtractionStore, events: list[Any]) -> None:
        super().__init__(store, "p-1", MODEL)
        self.events = events

    @override
    async def remember(self, rule: InferenceRule, entries: Any) -> None:
        if entries:
            self.events.append(sorted(key.rsplit("|", 1)[-1] if "|members" in key else "judgement" for key in entries))
        await super().remember(rule, entries)


async def test_judgements_are_written_per_chunk_before_the_swap_and_members_after_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []
    swap = inference._apply

    async def recorded_swap(*args: Any) -> Any:
        events.append("swap")
        return await swap(*args)

    monkeypatch.setattr(infer_bound, "CHUNK", 1)
    monkeypatch.setattr(inference, "_apply", recorded_swap)
    graph = with_billing_hub(xdoc_graph(hub_attributes={"type": "backend"}), {"fr-f": ["d1"], "fr-g": ["d2"]})
    answers = {(XDOC.prompt, "Orders service"): xdoc_answer(), (XDOC.prompt, "Billing service"): xdoc_answer()}
    memory = _Recording(FileExtractionStore(tmp_path), events)
    await build(graph, bounded(XDOC), scripted(answers), memory=memory)
    judgement = ["judgement", "judgement"]  # a judgement and its seen key
    assert events == [judgement, judgement, "swap", ["members", "members"]]


async def test_a_resumed_infer_reuses_the_judgements_written_before_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(infer_bound, "CHUNK", 1)
    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph = orphans(3)
    with pytest.raises(LLMFatalError):
        await build(graph, bounded(cap=3), declining(3, **{"fr-2": LLMFatalError()}), memory=memory)
    assert graph.writes == []  # no swap
    gateway = scripted({(CONT1.prompt, name): attach_to(None) for name in ("fr-2", "fr-3")})
    (rule,) = (await build(graph, bounded(cap=3), gateway, memory=memory)).rules
    assert called(gateway) == ["fr-2", "fr-3"]
    assert (rule.reused, rule.judged) == (1, 2)


async def test_a_resumed_infer_charges_its_budget_from_the_input_its_completed_calls_were_counted_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First build, cap 1, a budget of three calls less one token: fr-1 is judged and flushed, then a
    fatal error. Resumed, the build has spent fr-1's input already: fr-2 (the cap) is judged, fr-3 does
    not fit; restarting the budget at 0 would judge fr-3 too."""

    monkeypatch.setattr(infer_bound, "CHUNK", 1)
    tokens = (await build(orphans(1), bounded(cap=1), declining(1))).rules[0].budget_tokens or 0
    assert tokens > 0
    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph, ontology = orphans(4), bounded(cap=1, first_cap=4, first_budget=3 * tokens - 1)
    first = inference.BuildChanges(first=True)
    with pytest.raises(LLMFatalError):
        await build(graph, ontology, declining(4, **{"fr-2": LLMFatalError()}), memory=memory, changes=first)
    assert await memory.spent("b-1") == tokens
    gateway = declining(4)
    report = await build(graph, ontology, gateway, memory=memory, changes=first)
    (rule,) = report.rules
    assert called(gateway) == ["fr-2"]
    assert (rule.reused, rule.judged, rule.limit) == (1, 1, "budget")
    assert await memory.spent("b-1") == 2 * tokens
    assert await memory.spent("b-2") == 0  # per build


async def test_a_no_change_build_queues_subjects_whose_inputs_changed_for_the_next_build(tmp_path: Path) -> None:
    """fr-2 was judged; its candidates change (a new feature) in a build whose diff changed nothing: it
    is not judged there but queued (``changed_queued``), and the next build judges it from the queue."""

    memory = inference.JudgeMemory(FileExtractionStore(tmp_path), "p-1", MODEL)
    graph = orphans(3)
    await build(graph, bounded(cap=3), declining(3), memory=memory)
    graph.node("feat-2", "Feature")
    graph.similar["fr-2"] = ["feat-1", "feat-2"]
    gateway = declining(3)
    nothing = inference.BuildChanges(graph_changed=False)
    report = await build(graph, bounded(cap=3), gateway, memory=memory, changes=nothing)
    (rule,) = report.rules
    assert called(gateway) == []
    assert (rule.reused, rule.deferred, rule.changed_queued, rule.backlog) == (2, 1, 1, 1)
    assert report.backlog == {CONT1.id: BacklogQueue(spec_ids=("X::FR-2",), since_build="b-1", age_builds=1)}
    gateway = declining(3)
    changes = inference.BuildChanges(graph_changed=False, backlog=report.backlog or {})
    report = await build(graph, bounded(cap=3), gateway, memory=memory, changes=changes, build_id="b-2")
    assert called(gateway) == ["fr-2"]
    assert (report.rules[0].changed_queued, report.backlog) == (0, {})
    (rule,) = (await build(graph, bounded(cap=3), declining(3), memory=memory)).rules
    assert rule.changed_queued is None  # only a build whose diff changed nothing reports it


# Calls in a sliding window


async def test_a_slow_call_holds_only_its_own_place_in_the_window() -> None:
    """150 subjects: fr-1's call waits until every other call finished. A chunk of 100 waiting for its
    slowest call would never start the last 50 while fr-1 waits."""

    release = asyncio.Event()
    finished: list[str] = []

    async def answer(request: Any) -> Any:
        name = subject_of(request)
        if name == "fr-1":
            try:
                await asyncio.wait_for(release.wait(), timeout=5)
            except TimeoutError:
                finished.append("blocked")
        else:
            finished.append(name)
            if len(finished) == 149:
                release.set()
        return attach_to(None)

    gateway = scripted({}).handle(CONT1.prompt, answer)
    (rule,) = (await build(orphans(150), bounded(cap=150), gateway)).rules
    assert "blocked" not in finished
    assert (rule.judged, rule.declined) == (150, 150)


async def test_verdicts_are_evaluated_in_judging_order_whatever_order_the_calls_finish_in() -> None:
    """Later subjects answer first and three judgements fail: the report (the failed subjects at the
    queue's head in judging order) is that of calls finishing in order."""

    failing = {"fr-3", "fr-7", "fr-12"}

    def answering(*, delay: bool) -> ScriptedGateway:
        async def answer(request: Any) -> Any:
            name = subject_of(request)
            if delay:
                await asyncio.sleep((21 - int(name.split("-")[1])) / 1000)
            return LLMError("exhausted") if name in failing else attach_to("C1")

        return scripted({}).handle(CONT1.prompt, answer)

    ordered = await build(orphans(20), bounded(cap=20), answering(delay=False))
    late_first = await build(orphans(20), bounded(cap=20), answering(delay=True))
    assert late_first == ordered
    assert ordered.backlog is not None
    assert ordered.backlog[CONT1.id].spec_ids == ("X::FR-12", "X::FR-3", "X::FR-7")  # spec_id order
    assert (ordered.rules[0].judged, ordered.rules[0].failed) == (17, 3)


async def test_judge_memory_is_written_after_every_chunk_of_completed_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(infer_bound, "CHUNK", 3)
    events: list[Any] = []
    memory = _Recording(FileExtractionStore(tmp_path), events)
    await build(orphans(7), bounded(cap=7), declining(7), memory=memory)
    # 3, 3 and the last subject: each a judgement and its seen key
    assert [len(event) for event in events] == [6, 6, 2]


# The queue across builds


def started(build_id: str, minute: int) -> BuildRecord:
    return BuildRecord(
        build_id=build_id,
        project_id="p-1",
        mode="update",
        options=BuildOptions(),
        ontology_fingerprint="fp",
        label_set=("Feature",),
        embedding_model="embed",
        started_at=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
    )


async def finish(builds: FileBuildStore, record: BuildRecord) -> None:
    await builds.finish_build(
        BuildResult(
            project_id="p-1",
            build_id=record.build_id,
            mode=record.mode,
            status="succeeded",
            started_at=record.started_at,
            finished_at=record.started_at,
        )
    )


async def test_the_queue_round_trips_through_the_latest_finished_builds_infer_artifact(tmp_path: Path) -> None:
    builds = FileBuildStore(tmp_path / "builds")
    memory = inference.JudgeMemory(FileExtractionStore(tmp_path / "extraction"), "p-1", MODEL)
    graph, ontology = orphans(3), bounded(cap=1)
    assert await inference.previous_backlog(builds, "p-1") == {}
    expected = [("b-1", 1, ("X::FR-2", "X::FR-3")), ("b-2", 2, ("X::FR-3",)), ("b-3", 3, None)]
    for minute, (build_id, age, queue) in enumerate(expected, 1):
        backlog = await inference.previous_backlog(builds, "p-1")
        record = started(build_id, minute)
        await builds.create_build(record)
        assert await inference.previous_backlog(builds, "p-1") == backlog  # the running build is not read
        report = await build(
            graph,
            ontology,
            declining(3),
            memory=memory,
            changes=inference.BuildChanges(backlog=backlog),
            build_id=build_id,
        )
        await builds.put_artifact("p-1", build_id, "infer", report.model_dump(mode="json"))
        await finish(builds, record)
        if queue is None:
            assert report.backlog == {} and report.rules[0].backlog == 0
        else:
            assert report.backlog == {CONT1.id: BacklogQueue(spec_ids=queue, since_build="b-1", age_builds=age)}
    # The latest finished build without an infer artifact: no queue (only its order is lost).
    await builds.create_build(started("b-4", 9))
    await finish(builds, started("b-4", 9))
    assert await inference.previous_backlog(builds, "p-1") == {}
