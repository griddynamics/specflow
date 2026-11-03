"""Reuse keys change with ids, revisions, edges, membership and planning options, never with text."""

from __future__ import annotations

from graph.plan.models import MeaningRevisions, PlanningPart, Profile, SpecEdgeView
from graph.plan.reuse import describe_key, project_key, tasks_key
from graph.plan.view import PlanView
from graph_plan_support import shop_view


def _tasks_key(view: PlanView, part: PlanningPart, **options: int) -> str:
    return tasks_key(
        view,
        "shop",
        part,
        setup_tasks=["TASK::PROJECT::WORKSPACE"],
        profile=Profile(),
        task_size=options.get("task_size", 5),
        planner_members=options.get("planner_members", 40),
    )


def _with_revision(view: PlanView, spec_id: str, build_id: str) -> PlanView:
    revisions = MeaningRevisions(nodes={**view.snapshot.revisions.nodes, spec_id: build_id})
    snapshot = view.snapshot.model_copy(update={"revisions": revisions})
    return PlanView(snapshot, view.partition, view.rules)


def _part(view: PlanView, area_id: str) -> PlanningPart:
    return next(part for part in view.partition.parts if part.area_id == area_id)


async def test_a_member_revision_changes_only_its_own_area_and_part_keys() -> None:
    view = await shop_view()
    changed = _with_revision(view, "REQ::FR::PLACE_ORDER", "build-9")

    assert describe_key(changed, "shop", "AREA::ORDERS_SERVICE") != describe_key(view, "shop", "AREA::ORDERS_SERVICE")
    assert describe_key(changed, "shop", "AREA::WEB_SHOP") == describe_key(view, "shop", "AREA::WEB_SHOP")
    orders, web = _part(view, "AREA::ORDERS_SERVICE"), _part(view, "AREA::WEB_SHOP")
    assert _tasks_key(changed, orders) != _tasks_key(view, orders)
    assert _tasks_key(changed, web) == _tasks_key(view, web)


async def test_rewording_changes_no_key() -> None:
    view = await shop_view()
    nodes = tuple(
        node.model_copy(update={"name": node.name + " (reworded)", "statements": ()})
        for node in view.snapshot.plannable
    )
    reworded = PlanView(view.snapshot.model_copy(update={"plannable": nodes}), view.partition, view.rules)

    assert describe_key(reworded, "shop", "AREA::ORDERS_SERVICE") == describe_key(view, "shop", "AREA::ORDERS_SERVICE")
    part = _part(view, "AREA::ORDERS_SERVICE")
    assert _tasks_key(reworded, part) == _tasks_key(view, part)


async def test_a_touching_edge_changes_the_keys_of_its_area() -> None:
    view = await shop_view()
    edge = SpecEdgeView(
        relation="RELATES_TO",
        source="REQ::FR::PLACE_ORDER",
        target="REQ::FR::CHECKOUT",
        basis="inferred",
        edge_key="REQ_FR_PLACE_ORDER|RELATES_TO|REQ_FR_CHECKOUT",
    )
    linked = PlanView(
        view.snapshot.model_copy(update={"edges": (*view.snapshot.edges, edge)}), view.partition, view.rules
    )

    part = _part(view, "AREA::ORDERS_SERVICE")
    assert _tasks_key(linked, part) != _tasks_key(view, part)
    assert describe_key(linked, "shop", "AREA::PAYMENTS_SERVICE") == describe_key(
        view, "shop", "AREA::PAYMENTS_SERVICE"
    )


async def test_membership_and_planning_options_change_the_tasks_key() -> None:
    view = await shop_view()
    part = _part(view, "AREA::ORDERS_SERVICE")
    smaller = part.model_copy(update={"members": part.members[1:]})

    assert _tasks_key(view, smaller) != _tasks_key(view, part)
    assert _tasks_key(view, part, task_size=3) != _tasks_key(view, part)
    assert _tasks_key(view, part, planner_members=10) != _tasks_key(view, part)


async def test_the_project_key_follows_its_inputs_and_the_project_id() -> None:
    view = await shop_view()

    first = project_key(view, "shop", ["CON::EU_REGION"], ())
    assert project_key(view, "other", ["CON::EU_REGION"], ()) != first
    assert project_key(_with_revision(view, "CON::EU_REGION", "build-9"), "shop", ["CON::EU_REGION"], ()) != first
    assert project_key(_with_revision(view, "REQ::FR::CHECKOUT", "build-9"), "shop", ["CON::EU_REGION"], ()) == first
