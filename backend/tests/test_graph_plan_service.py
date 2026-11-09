"""``PlanService``: the task lists, a feature's tasks, the path to a task and the brief's lookup, each from one read of
the plan (``plan_reads_support.shop_plan`` in memory, the spec graph of ``ShopGraph``)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from graph.plan import PlanReadError, PlanService
from graph.plan.reader import LoadedPlan
from graph_plan_support import plan_ontology, plan_rules
from graph_support import OfflineExecutor
from plan_reads_support import OPEN_TASK_IDS, READY, ShopGraph, shop_plan


def _service(*, has_plan: bool = True) -> tuple[PlanService, list[str]]:
    loads: list[str] = []
    loaded = shop_plan(has_plan=has_plan)

    async def load(project_id: str) -> LoadedPlan:
        loads.append(project_id)
        return loaded

    ontology = plan_ontology()
    return PlanService(OfflineExecutor(), ontology, plan_rules(ontology), graph=ShopGraph(), load=load), loads


def _ids(items: Any) -> list[str]:
    return [item.task_id for item in items]


async def test_list_tasks_keeps_ready_tasks_by_default_and_never_lists_history():
    service, loads = _service()

    ready = await service.list_tasks("shop")
    everything = await service.list_tasks("shop", ready_only=False)

    assert _ids(ready) == list(READY) and all(item.ready for item in ready)
    assert _ids(everything) == list(OPEN_TASK_IDS)
    first = everything[1]
    assert (first.area.title, first.story.title, first.waits_for) == ("Orders", "Place an order", ("TASK::ORDERS::A",))
    assert loads == ["shop", "shop"]  # one plan read per call


async def test_list_tasks_filters_by_status_area_and_story():
    service, _ = _service()

    done = await service.list_tasks("shop", ready_only=False, status="completed")
    pay = await service.list_tasks("shop", area_id="AREA::PAY")
    place = await service.list_tasks("shop", ready_only=False, story_id="STORY::ORDERS::PLACE")

    assert _ids(done) == ["TASK::ORDERS::A"]
    assert _ids(pay) == ["TASK::PAY::E"]
    assert _ids(place) == ["TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C"]


async def test_feature_tasks_lists_the_anchored_story_and_the_tasks_implementing_its_scope():
    service, loads = _service()

    found = await service.feature_tasks("shop", "FEAT::PLACE")
    by_alias = await service.feature_tasks("shop", "FEAT::ORDER")

    assert found.feature.spec_id == "FEAT::PLACE" and found.feature.name == "Place an order"
    assert _ids(found.items) == ["TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C", "TASK::PAY::E"]
    assert [item.ready for item in found.items] == [False, True, False, True]
    assert by_alias == found
    assert loads == ["shop", "shop"]


@pytest.mark.parametrize("feature_id", ["REQ::PAY_ON_PLACE", "FEAT::NONE"])
async def test_feature_tasks_refuses_a_node_that_is_no_feature(feature_id: str):
    service, _ = _service()

    with pytest.raises(PlanReadError) as raised:
        await service.feature_tasks("shop", feature_id)

    assert raised.value.code == "feature_not_found"


async def test_task_path_lists_the_prerequisites_in_build_order_then_the_task():
    service, _ = _service()

    path = await service.task_path("shop", "TASK::ORDERS::C")
    alone = await service.task_path("shop", "TASK::PAY::E")

    assert _ids(path.items) == ["TASK::ORDERS::A", "TASK::ORDERS::B", "TASK::ORDERS::C"]
    assert path.task_id == "TASK::ORDERS::C" and path.completed == 1
    assert [item.status for item in path.items] == ["completed", "pending", "pending"]
    assert _ids(alone.items) == ["TASK::PAY::E"] and alone.completed == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda service: service.task_path("shop", "TASK::ORDERS::OLD"),
        lambda service: service.task_path("shop", "TASK::NONE"),
        lambda service: service.task_brief("shop", "TASK::ORDERS::OLD"),
        lambda service: service.task_brief("shop", "TASK::NONE"),
    ],
)
async def test_an_unknown_or_superseded_task_is_not_found(call: Callable[[PlanService], Awaitable[Any]]):
    service, _ = _service()

    with pytest.raises(PlanReadError) as raised:
        await call(service)

    assert raised.value.code == "task_not_found"


@pytest.mark.parametrize(
    "call",
    [
        lambda service: service.list_tasks("shop"),
        lambda service: service.feature_tasks("shop", "FEAT::PLACE"),
        lambda service: service.task_path("shop", "TASK::ORDERS::C"),
        lambda service: service.task_brief("shop", "TASK::ORDERS::C"),
    ],
)
async def test_every_read_answers_no_plan_before_the_first_plan(call: Callable[[PlanService], Awaitable[Any]]):
    service, _ = _service(has_plan=False)

    with pytest.raises(PlanReadError) as raised:
        await call(service)

    assert raised.value.code == "no_plan"
