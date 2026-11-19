"""GapService reads: list_gaps with every filter, paging and order, get_gap with options, history and reach, and the
plan's findings port (docs/graph/gap/api.md "Gap service", lifecycle.md "Plan link"; FR-GOWN-02)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from gap_acceptance_support import Planted
from gap_detection_support import PROJECT
from gap_port_fakes import MemoryPlanView
from gap_run_support import GapWorld, corpus_world
from graph.gap import AnswerInvalidError, GapNotFoundError, GapService, run_gap
from graph.gap.gaps import gap_uuid
from graph.gap.models import EventRow, OptionRow, TaskRef
from graph.gap.service import decode_cursor, encode_cursor

AT = datetime(2099, 1, 1, tzinfo=UTC)
"""Later than any gap the tests record."""


def service(world: GapWorld, **options: Any) -> GapService:
    return GapService(
        world.executor, world.reader, world.ontology, world.rules, world.records, world.amendments, **options
    )


async def recorded(plan: MemoryPlanView | None = None) -> GapWorld:
    world = await corpus_world(plan=plan)
    await run_gap(world.context(), mode="full")
    return world


def question_gap(world: GapWorld) -> str:
    return next(gap for gap, props in world.gap_nodes().items() if props["kind"] == "open_question")


def set_props(world: GapWorld, gap_id: str, **props: Any) -> None:
    world.executor.nodes[("Gap", gap_uuid(PROJECT, gap_id))].update(props)


async def test_list_gaps_returns_open_gaps_by_severity_then_newest() -> None:
    world = await recorded()
    gaps = service(world)

    page = await gaps.list_gaps(PROJECT, limit=200)

    assert {view.gap_id for view in page.gaps} == set(world.gap_nodes()) and page.next is None
    ranks = [("blocker", "major", "minor").index(view.severity) for view in page.gaps]
    assert ranks == sorted(ranks)
    view = next(view for view in page.gaps if view.gap_id == question_gap(world))
    assert {node.spec_id for node in view.about} == {Planted.STATED_QUESTION, Planted.PLACE_ORDER}
    assert {node.label for node in view.about} == {"OpenQuestion", "FunctionalRequirement"}
    assert [item.quote for item in view.witness] == [Planted.STATED_QUESTION_LINE]
    assert view.history is None and view.reach is None and view.options == ()


async def test_list_gaps_pages_with_a_cursor() -> None:
    world = await recorded()
    gaps = service(world)
    every = (await gaps.list_gaps(PROJECT, limit=200)).gaps

    first = await gaps.list_gaps(PROJECT, limit=5)
    assert first.next is not None
    second = await gaps.list_gaps(PROJECT, limit=200, cursor=first.next)

    assert [view.gap_id for view in (*first.gaps, *second.gaps)] == [view.gap_id for view in every]
    assert decode_cursor(encode_cursor((1, -2, -5, "GAP::X"))) == (1, -2, -5, "GAP::X")


async def test_list_gaps_filters_combine() -> None:
    world = await recorded()
    gaps = service(world)
    question = question_gap(world)
    set_props(world, question, origin="development", route="ai", severity="blocker")

    assert [view.gap_id for view in (await gaps.list_gaps(PROJECT, kind="open_question")).gaps] == [question]
    assert [view.gap_id for view in (await gaps.list_gaps(PROJECT, origin=["development"])).gaps] == [question]
    assert [view.gap_id for view in (await gaps.list_gaps(PROJECT, route="ai", severity="blocker")).gaps] == [question]
    about = await gaps.list_gaps(PROJECT, about=[Planted.STATED_QUESTION])
    assert [view.gap_id for view in about.gaps] == [question]
    document = await gaps.list_gaps(PROJECT, document="upload:orders-spec", limit=200)
    assert question in {view.gap_id for view in document.gaps}
    assert (await gaps.list_gaps(PROJECT, document="upload:none")).gaps == ()
    assert (await gaps.list_gaps(PROJECT, status="resolved")).gaps == ()
    set_props(world, question, status="resolved")
    assert [view.gap_id for view in (await gaps.list_gaps(PROJECT, status="resolved")).gaps] == [question]
    assert question not in {view.gap_id for view in (await gaps.list_gaps(PROJECT, limit=200)).gaps}


async def test_the_about_filter_resolves_aliases() -> None:
    world = await recorded()
    question = question_gap(world)
    world.executor.rekey_spec(Planted.STATED_QUESTION, "QST::CANCEL_ORDER_AFTER_PAYMENT")

    page = await service(world).list_gaps(PROJECT, about=[Planted.STATED_QUESTION])

    assert [view.gap_id for view in page.gaps] == [question]


async def test_the_story_filter_needs_a_plan() -> None:
    plan = MemoryPlanView({Planted.PLACE_ORDER: [TaskRef(task_id="TASK::A", story_id="STORY::A", status="pending")]})
    world = await recorded(plan)
    question = question_gap(world)

    with_plan = await service(world, plan=plan).list_gaps(PROJECT, story_id="STORY::A")
    without = await service(world).list_gaps(PROJECT, story_id="STORY::A")

    assert question in {view.gap_id for view in with_plan.gaps}
    assert all(Planted.PLACE_ORDER in {node.spec_id for node in view.about} for view in with_plan.gaps)
    assert without.gaps == ()
    assert (await service(world, plan=plan).list_gaps(PROJECT, story_id="STORY::NONE")).gaps == ()


@pytest.mark.parametrize(
    ("arguments", "field"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 201}, "limit"),
        ({"cursor": "not-a-cursor"}, "cursor"),
        ({"cursor": encode_cursor((1, 2, "x"))[:-2] + "zz"}, "cursor"),
        ({"status": "dismissed"}, "status"),
        ({"kind": []}, "kind"),
        ({"severity": "info"}, "severity"),
        ({"origin": "robot"}, "origin"),
        ({"route": "maybe"}, "route"),
    ],
)
async def test_bad_filters_are_refused(arguments: dict[str, Any], field: str) -> None:
    world = await corpus_world()

    with pytest.raises(AnswerInvalidError) as raised:
        await service(world).list_gaps(PROJECT, **arguments)

    assert raised.value.field == field


async def test_get_gap_has_options_history_and_reach() -> None:
    plan = MemoryPlanView(
        {
            Planted.PLACE_ORDER: [TaskRef(task_id="TASK::A", story_id="STORY::A", status="pending")],
            Planted.PLACE_ORDER_FEATURE: [TaskRef(task_id="TASK::B", story_id="STORY::B", status="pending")],
        }
    )
    world = await recorded(plan)
    question = question_gap(world)
    option = OptionRow(gap_id=question, rank=1, label="Yes", recommended=True)
    await world.records.put_options(PROJECT, question, [option])
    await world.records.log(PROJECT, [EventRow(gap_id=question, at=AT, actor="ann", event="viewed")])

    view = await service(world, plan=plan).get_gap(PROJECT, question)

    assert view.options == (option,)
    assert view.history is not None
    assert view.history.events[0].event == "created"
    assert view.history.events[-1].actor == "ann" and view.history.follow_ups == ()
    assert view.reach is not None and "TASK::A" in view.reach.tasks and "STORY::A" in view.reach.stories
    bare = await service(world).get_gap(PROJECT, question)
    assert bare.reach is not None and bare.reach.tasks == ()
    with pytest.raises(GapNotFoundError):
        await service(world).get_gap(PROJECT, "GAP::000000000000")


async def test_open_counts_and_findings_satisfy_the_plans_port() -> None:
    world = await recorded()
    gaps = service(world)
    question = question_gap(world)
    set_props(world, question, severity="blocker")

    counts = await gaps.open_counts(PROJECT, [Planted.STATED_QUESTION, Planted.PLACE_ORDER, "REQ::FR::NONE"])
    page = await gaps.findings(PROJECT, spec_ids=[Planted.STATED_QUESTION])
    every = await gaps.findings(PROJECT, limit=200)

    assert counts[Planted.STATED_QUESTION] == 1 and counts["REQ::FR::NONE"] == 0
    assert counts[Planted.PLACE_ORDER] >= 1
    (finding,) = page.findings
    assert finding.finding_id == question and finding.kind == "open_question" and finding.blocking
    assert finding.text == Planted.STATED_QUESTION_LINE and not finding.orphaned and finding.story_id is None
    assert set(finding.about_spec_ids) == {Planted.STATED_QUESTION, Planted.PLACE_ORDER}
    assert every.findings[0].finding_id == question and len(every.findings) == len(world.gap_nodes())
    assert await gaps.open_counts(PROJECT, []) == {}
    with pytest.raises(AnswerInvalidError):
        await gaps.findings(PROJECT, limit=0)


async def test_a_gap_without_about_is_an_orphaned_finding_and_reads_a_clarification() -> None:
    world = await recorded()
    question = question_gap(world)
    for key in [key for key in world.executor.edges if key[0] == "ABOUT" and key[1].startswith(question)]:
        del world.executor.edges[key]
    clarification = ("Clarification", "uuid-clr")
    world.executor.nodes[clarification] = {
        "clr_id": "CLR::1",
        "mode": "option",
        "placement": "amend",
        "status": "pending",
        "answered_by": "ann",
        "answered_at": AT.isoformat(),
        "basis": '[{"fragment_id": "f", "quote": "q"}]',
    }
    world.executor.nodes[("Clarification", "uuid-bad")] = {"clr_id": "CLR::2", "status": "pending"}
    world.executor.edges[("ANSWERS", "a")] = (clarification, ("Gap", gap_uuid(PROJECT, question)))
    world.executor.edges[("ANSWERS", "b")] = (("Clarification", "uuid-bad"), ("Gap", gap_uuid(PROJECT, question)))

    page = await service(world).findings(PROJECT, limit=200)
    view = await service(world).get_gap(PROJECT, question)

    found = next(item for item in page.findings if item.finding_id == question)
    assert found.orphaned and found.about_spec_ids == ()
    assert view.clarification is not None and view.clarification.clr_id == "CLR::1"
    assert view.clarification.basis[0].quote == "q"
