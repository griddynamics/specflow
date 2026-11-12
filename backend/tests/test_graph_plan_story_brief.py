"""``PlanService.story_brief`` and the open findings joined into the brief and the story preview through the findings
port (``graph.plan`` never imports ``graph.gap``; the real ``Findings`` library satisfies the port structurally)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gap_support import Clock, MemoryGapExecutor
from graph.core.ontology import load_ontology
from graph.gap import Findings
from graph.plan import PlanReadError, PlanService
from graph.plan.findings_port import open_findings_about
from graph.plan.reader import LoadedPlan
from graph_plan_support import plan_ontology, plan_rules
from plan_trace_support import CHECKOUT, COMMIT, PAYMENTS, PROJECT, STRIPE, ShopSpecGraph, offline, shop_plan, task

LOGIC, DATA = "TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::WEB_SHOP::DATA_CHECKOUT"


def service(plan: LoadedPlan | None = None, *, findings: Any = None, graph: ShopSpecGraph | None = None) -> PlanService:
    loaded = plan or shop_plan()

    async def load(_project_id: str) -> LoadedPlan:
        return loaded

    ontology = plan_ontology()
    return PlanService(
        offline(), ontology, plan_rules(ontology), graph=graph or ShopSpecGraph(), load=load, findings=findings
    )


def findings_library() -> tuple[Findings, MemoryGapExecutor]:
    memory = MemoryGapExecutor()
    for spec_id, label in (
        ("FEAT::CHECKOUT", "Feature"),
        ("REQ::FR::CHECKOUT", "FunctionalRequirement"),
        ("MOD::STRIPE", "Module"),
    ):
        memory.spec(spec_id, label)
    return Findings(memory, load_ontology(), clock=Clock()), memory


async def test_the_story_brief_merges_its_tasks_in_topological_order_with_contracts_of_outside_prerequisites() -> None:
    brief = await service().story_brief(PROJECT, CHECKOUT)

    assert brief.area is not None
    assert (brief.story.story_id, brief.story.progress, brief.area.area_id) == (
        CHECKOUT,
        "in_progress",
        "AREA::WEB_SHOP",
    )
    assert [item.task.task_id for item in brief.tasks] == [DATA, LOGIC]  # LOGIC waits for DATA
    assert brief.anchor is not None and brief.anchor["spec_id"] == "FEAT::CHECKOUT"
    assert [node.spec_id for node in brief.implements] == [
        "FEAT::CHECKOUT",
        "REQ::FR::CHECKOUT",
        "REQ::Q::CHECKOUT_SPEED",
    ]
    assert [(c.task_id, c.commit) for c in brief.contracts] == [("TASK::PAYMENTS::CONTRACT", COMMIT)]
    assert brief.spec_changed is False and brief.missing == () and brief.findings == ()
    assert {neighbour.of for neighbour in brief.neighbours} <= {node.spec_id for node in brief.implements}
    assert "claim_token" not in brief.model_dump_json()


async def test_the_brief_limits_neighbours_and_reports_missing_and_changed_nodes() -> None:
    plan = shop_plan(tasks=(task(LOGIC, CHECKOUT, ("FEAT::CHECKOUT", "REQ::FR::GONE")),))
    assert plan.current is not None
    unconsumed = LoadedPlan(
        plan.current.model_copy(update={"plan": plan.current.plan.model_copy(update={"consumed_build_ids": ()})})
    )

    brief = await service(unconsumed).story_brief(PROJECT, CHECKOUT, neighbours=1)

    assert len(brief.neighbours) == 1
    assert brief.missing == ("REQ::FR::GONE",) and brief.spec_changed is True


@pytest.mark.parametrize("story_id", ["STORY::X::NONE"])
async def test_an_unknown_or_retired_story_has_no_brief(story_id: str) -> None:
    for plan_service, asked in ((service(), story_id), (service(shop_plan(retired=(CHECKOUT,))), CHECKOUT)):
        with pytest.raises(PlanReadError) as caught:
            await plan_service.story_brief(PROJECT, asked)
        assert caught.value.code == "story_not_found"


async def test_a_brief_shows_the_open_findings_about_its_nodes_blocking_first_and_only_open_ones() -> None:
    # Scenario: a note, a blocking question (newer), a resolved gap about the story's nodes and a finding about
    # another story.
    library, _ = findings_library()

    async def add(kind: str, text: str, about: list[str], **more: Any) -> Any:
        return await library.add(PROJECT, created_by="u1", kind=kind, text=text, about=about, **more)

    await add("note", "an old note", ["FEAT::CHECKOUT"])
    await add("question", "which currency?", ["REQ::FR::CHECKOUT"], story_id=CHECKOUT, blocking=True)
    closed = (await add("gap", "closed gap", ["FEAT::CHECKOUT"])).finding
    await library.resolve(PROJECT, closed.finding_id, by="lead")
    await add("gap", "other story", ["MOD::STRIPE"])

    brief = await service(findings=library).story_brief(PROJECT, CHECKOUT)

    assert [(f.kind, f.text, f.blocking) for f in brief.findings] == [
        ("question", "which currency?", True),
        ("note", "an old note", False),
    ]
    assert brief.findings[0].story_id == CHECKOUT and brief.findings[0].created_by == "u1"
    stripe = await service(findings=library).story_brief(PROJECT, STRIPE)
    assert [f.text for f in stripe.findings] == ["other story"]


async def test_the_preview_shows_the_open_findings_about_the_nodes_of_its_open_tasks() -> None:
    library, _ = findings_library()
    await library.add(PROJECT, created_by="u1", kind="gap", text="gap about the feature", about=["FEAT::CHECKOUT"])
    await library.add(PROJECT, created_by="u1", kind="gap", text="gap about stripe", about=["MOD::STRIPE"])

    preview = await service(findings=library).story_preview(PROJECT, CHECKOUT)
    without_port = await service().story_preview(PROJECT, CHECKOUT)

    assert [item.text for item in preview.findings] == ["gap about the feature"]
    assert without_port.findings == () and preview.model_copy(update={"findings": ()}) == without_port


async def test_a_preview_of_a_story_with_no_open_task_has_no_findings_query() -> None:
    done = tuple(
        t.model_copy(update={"status": "completed", "commit": COMMIT})
        for t in shop_plan().current.tasks
        if t.story_id == PAYMENTS
    )
    library, memory = findings_library()
    calls = memory.calls.copy()

    preview = await service(shop_plan(tasks=done), findings=library).story_preview(PROJECT, PAYMENTS)

    assert preview.findings == () and memory.calls == calls


async def test_the_open_findings_of_more_than_200_ids_are_read_in_chunks_blocking_first_newest_first() -> None:
    # Scenario: 450 ids make three filters of at most 200 ids; a finding about ids of two chunks appears once; the
    # merge puts blocking findings first, then the newest.
    asked: list[int] = []
    moment = datetime(2026, 1, 1, tzinfo=UTC)

    def item(finding_id: str, day: int, *, blocking: bool = False) -> SimpleNamespace:
        return SimpleNamespace(
            finding_id=finding_id,
            kind="gap",
            text=finding_id,
            blocking=blocking,
            about_spec_ids=("A",),
            story_id=None,
            created_by="u1",
            created_at=moment + timedelta(days=day),
            orphaned=False,
        )

    pages = [
        [item("old", 1), item("shared", 5)],
        [item("shared", 5), item("blocking", 0, blocking=True)],
        [item("new", 9)],
    ]

    class Port:
        async def findings(self, _project_id: str, *, spec_ids: Sequence[str], limit: int) -> Any:
            asked.append(len(spec_ids))
            assert len(spec_ids) <= limit == 200
            return SimpleNamespace(findings=pages[len(asked) - 1])

    found = await open_findings_about(cast(Any, Port()), PROJECT, [f"SPEC::{number}" for number in range(450)])

    assert asked == [200, 200, 50]
    assert [finding.finding_id for finding in found] == ["blocking", "new", "shared", "old"]
