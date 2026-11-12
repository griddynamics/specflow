"""``PlanService.node_plan``, ``plan_states``, ``story_nodes`` and ``held_stories`` (the plan's side of ``get_node``,
``similar``, ``search_spec`` and its ``near``), and the ``state`` and ``progress`` of the story brief."""

from __future__ import annotations

import pytest

from graph.plan import PlanReadError, PlanService
from graph.plan.reader import LoadedPlan
from graph_plan_support import plan_ontology, plan_rules
from graph_trace_support import shop_graph
from plan_trace_support import CHECKOUT, COMMIT, PAYMENTS, PROJECT, STRIPE, ShopSpecGraph, offline, shop_plan, task

LOGIC, DATA = "TASK::WEB_SHOP::LOGIC_CHECKOUT", "TASK::WEB_SHOP::DATA_CHECKOUT"


def service(plan: LoadedPlan | None = None) -> PlanService:
    loaded = plan or shop_plan()

    async def load(_project_id: str) -> LoadedPlan:
        return loaded

    ontology = plan_ontology()
    return PlanService(offline(), ontology, plan_rules(ontology), graph=ShopSpecGraph(), load=load)


async def test_a_node_shows_its_open_and_completed_tasks_and_the_state_they_make() -> None:
    plan = await service().node_plan(PROJECT, "FEAT::CHECKOUT")

    assert plan is not None
    assert plan.plan_state == "open"  # one completed, one pending
    assert [(t.task_id, t.status, t.commit, t.claimed_by) for t in plan.tasks] == [
        (DATA, "completed", COMMIT, None),
        (LOGIC, "pending", None, None),
    ]


async def test_in_progress_tasks_name_their_holder_and_closed_history_is_left_out() -> None:
    held = await service().node_plan(PROJECT, "MOD::STRIPE")
    superseded = await service().node_plan(PROJECT, "MOD::WEB_SHOP")  # only a superseded task implements it

    assert held is not None and held.plan_state == "in_progress"
    assert sorted(t.claimed_by or "" for t in held.tasks) == ["u2", "u3"]
    assert superseded is not None and (superseded.plan_state, superseded.tasks) == ("unplanned", ())


async def test_aliases_count_as_the_node_and_a_project_without_a_plan_has_none() -> None:
    aliased = await service().node_plan(PROJECT, "FEAT::CHECKOUT_NEW", ("FEAT::CHECKOUT",))
    empty = LoadedPlan(shop_plan().current.model_copy(update={"plan": None}))

    assert aliased is not None and len(aliased.tasks) == 2
    assert await service(empty).node_plan(PROJECT, "FEAT::CHECKOUT") is None


async def test_story_nodes_are_the_anchor_and_the_implemented_nodes_and_unknown_stories_raise() -> None:
    assert await service().story_nodes(PROJECT, CHECKOUT) == (
        "FEAT::CHECKOUT",
        "REQ::FR::CHECKOUT",
        "REQ::Q::CHECKOUT_SPEED",
    )
    with pytest.raises(PlanReadError) as unknown:
        await service().story_nodes(PROJECT, "STORY::NOPE")
    assert unknown.value.code == "story_not_found"
    empty = LoadedPlan(shop_plan().current.model_copy(update={"plan": None}))
    with pytest.raises(PlanReadError) as none:
        await service(empty).story_nodes(PROJECT, CHECKOUT)
    assert none.value.code == "no_plan"


async def test_story_nodes_are_current_ids_when_a_task_names_an_old_id() -> None:
    graph = ShopSpecGraph(shop_graph().alias("REQ::FR::CHECKOUT", "REQ::FR::PAY_AND_GO"))
    loaded = shop_plan(tasks=(task(LOGIC, CHECKOUT, ("REQ::FR::PAY_AND_GO",)),))

    async def load(_project_id: str) -> LoadedPlan:
        return loaded

    ontology = plan_ontology()
    plans = PlanService(offline(), ontology, plan_rules(ontology), graph=graph, load=load)

    nodes = await plans.story_nodes(PROJECT, CHECKOUT)

    assert "REQ::FR::CHECKOUT" in nodes and "REQ::FR::PAY_AND_GO" not in nodes  # where search_spec near starts


async def test_the_story_brief_carries_the_story_state_and_each_task_progress() -> None:
    plan = shop_plan(
        tasks=(
            task(DATA, CHECKOUT, ("FEAT::CHECKOUT",), "in_progress", claimed_by="u1", progress="todo"),
            task(LOGIC, CHECKOUT, ("FEAT::CHECKOUT",)),
        )
    )

    brief = await service(plan).story_brief(PROJECT, CHECKOUT)

    assert brief.state == "in_progress"
    assert {item.task.task_id: item.progress for item in brief.tasks} == {DATA: "todo", LOGIC: None}


async def test_plan_states_give_each_node_its_state_from_one_plan_and_none_without_a_plan() -> None:
    states = await service().plan_states(
        PROJECT, {"FEAT::CHECKOUT": (), "MOD::STRIPE": (), "FEAT::NEW": ("FEAT::CHECKOUT",), "FEAT::GIFT_CARDS": ()}
    )
    empty = LoadedPlan(shop_plan().current.model_copy(update={"plan": None}))

    assert states == {
        "FEAT::CHECKOUT": "open",
        "MOD::STRIPE": "in_progress",
        "FEAT::NEW": "open",
        "FEAT::GIFT_CARDS": "unplanned",
    }
    assert await service(empty).plan_states(PROJECT, {"FEAT::CHECKOUT": ()}) is None


def held_plan(holders: dict[str, str], *, retired: tuple[str, ...] = ()) -> LoadedPlan:
    """The shop plan with story claims: story id to its holder."""

    loaded = shop_plan(retired=retired)
    stories = tuple(
        story.model_copy(update={"claimed_by": holders.get(story.story_id)}) for story in loaded.current.stories
    )
    return LoadedPlan(loaded.current.model_copy(update={"stories": stories}))


async def test_held_stories_name_the_callers_stories_with_the_given_nodes_they_implement_first() -> None:
    plan = held_plan({CHECKOUT: "u1", STRIPE: "u1", PAYMENTS: "u2"})
    nodes = {"REQ::FR::CHECKOUT": (), "FEAT::CHECKOUT_NEW": ("FEAT::CHECKOUT",), "MOD::NOPE": ()}

    held = await service(plan).held_stories(PROJECT, "u1", nodes)

    assert [(story.story_id, story.title, story.spec_ids) for story in held] == [
        (CHECKOUT, "Checkout", ("REQ::FR::CHECKOUT", "FEAT::CHECKOUT_NEW")),  # an alias counts as the node
        (STRIPE, "Stripe integration", ()),  # held, but none of the nodes: still named, after the matches
    ]


async def test_held_stories_leave_out_other_holders_retired_stories_and_closed_tasks() -> None:
    other = await service(held_plan({CHECKOUT: "u2"})).held_stories(PROJECT, "u1", {"FEAT::CHECKOUT": ()})
    retired = await service(held_plan({CHECKOUT: "u1"}, retired=(CHECKOUT,))).held_stories(
        PROJECT, "u1", {"FEAT::CHECKOUT": ()}
    )
    closed = await service(held_plan({CHECKOUT: "u1"})).held_stories(PROJECT, "u1", {"MOD::WEB_SHOP": ()})
    empty = LoadedPlan(held_plan({CHECKOUT: "u1"}).current.model_copy(update={"plan": None}))

    assert other == () and retired == ()
    assert [story.spec_ids for story in closed] == [()]  # only a superseded task implements MOD::WEB_SHOP
    assert await service(empty).held_stories(PROJECT, "u1", {"FEAT::CHECKOUT": ()}) == ()
