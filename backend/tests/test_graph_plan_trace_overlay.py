"""``PlanService.trace``: the graph trace of ``trace.md``'s worked example with the plan's state and open findings on
every node, a project without a plan, a story as the seed, and the checks of the call."""

from __future__ import annotations

from typing import Any

import pytest

from gap_support import Clock, MemoryGapExecutor
from graph.core.errors import ConfigurationError
from graph.core.ontology import load_ontology
from graph.gap import Findings
from graph.plan import PlanReadError, PlanService
from graph.plan.reader import LoadedPlan
from graph_plan_support import plan_ontology, plan_rules
from graph_trace_support import shop_graph
from plan_trace_support import CHECKOUT, COMMIT, PAYMENTS, PROJECT, STRIPE, ShopSpecGraph, offline, shop_plan, task

SEEDS = ["FEAT::CARD_PAYMENT", "REQ::FR::CARD_PAYMENT"]


def service(plan: LoadedPlan | None = None, *, findings: Any = None, graph: ShopSpecGraph | None = None) -> PlanService:
    loaded = plan or shop_plan()

    async def load(_project_id: str) -> LoadedPlan:
        return loaded

    ontology = plan_ontology()
    return PlanService(
        offline(), ontology, plan_rules(ontology), graph=graph or ShopSpecGraph(), load=load, findings=findings
    )


def by_id(items: Any) -> dict[str, Any]:
    return {item.spec_id: item for item in items}


async def test_affects_of_the_worked_example_carries_plan_state_tasks_and_stories() -> None:
    # Scenario: trace.md "Worked example", direction affects depth 2, on a plan whose tasks implement those nodes.
    trace = await service().trace(PROJECT, spec_ids=SEEDS, direction="affects", depth=2)

    assert trace.sources == tuple(SEEDS) and trace.unknown == () and not trace.truncated
    nodes = by_id(trace.nodes)
    assert list(nodes) == [
        "FEAT::CHECKOUT",
        "MOD::STRIPE",
        "MOD::WEB_SHOP",
        "REQ::FR::CHECKOUT",
        "REQ::Q::CHECKOUT_SPEED",
    ]
    assert {spec_id: node.plan_state for spec_id, node in nodes.items()} == {
        "FEAT::CHECKOUT": "open",  # one completed task and one pending
        "MOD::STRIPE": "in_progress",
        "MOD::WEB_SHOP": "unplanned",  # its only task is superseded history
        "REQ::FR::CHECKOUT": "open",
        "REQ::Q::CHECKOUT_SPEED": "open",
    }
    checkout = nodes["FEAT::CHECKOUT"]
    assert (checkout.hops, checkout.parent, checkout.relation, checkout.step) == (
        1,
        "FEAT::CARD_PAYMENT",
        "DEPENDS_ON",
        "backward",
    )
    assert [(t.task_id, t.status, t.claimed_by, t.commit) for t in checkout.tasks] == [
        ("TASK::WEB_SHOP::DATA_CHECKOUT", "completed", None, COMMIT),  # a completed task shows its commit, not a claim
        ("TASK::WEB_SHOP::LOGIC_CHECKOUT", "pending", None, None),
    ]
    assert [(t.task_id, t.claimed_by, t.commit) for t in nodes["MOD::STRIPE"].tasks] == [
        ("TASK::STRIPE::SETUP", "u2", None),  # an in-progress task shows its holder, not a commit
        ("TASK::STRIPE::WIRE", "u3", None),
    ]
    assert nodes["MOD::WEB_SHOP"].tasks == ()
    assert [(c.spec_id, c.plan_state) for c in trace.context] == [("MOD::PAYMENTS_SERVICE", "completed")]
    assert trace.summary == {"completed": 0, "in_progress": 1, "open": 3, "unplanned": 1}
    assert [(s.story_id, s.title, s.progress, s.holders) for s in trace.stories] == [
        (PAYMENTS, "Card payment", "blocked", ()),  # stories come in id order
        (STRIPE, "Stripe integration", "in_progress", ("u2", "u3")),
        (CHECKOUT, "Checkout", "in_progress", ()),
    ]
    assert "claim_token" not in trace.model_dump_json()


async def test_based_on_of_the_worked_example_reports_completed_nodes() -> None:
    trace = await service().trace(PROJECT, spec_ids=SEEDS, direction="based_on", depth=3)

    nodes = by_id(trace.nodes)
    assert list(nodes) == ["IFC::STRIPE_API", "IFC::UNSHARED", "MOD::STRIPE", "REQ::INT::CALL_STRIPE", "CON::STRIPE"]
    assert {spec_id: node.plan_state for spec_id, node in nodes.items()} == {
        "IFC::STRIPE_API": "completed",
        "IFC::UNSHARED": "open",  # blocked counts as open
        "MOD::STRIPE": "in_progress",
        "REQ::INT::CALL_STRIPE": "unplanned",
        "CON::STRIPE": "open",
    }
    assert all(node.via_owner for node in trace.nodes)
    assert trace.summary == {"completed": 1, "in_progress": 1, "open": 2, "unplanned": 1}


async def test_the_overlay_adds_open_finding_counts_from_the_findings_port_context_nodes_included() -> None:
    # Scenario: the real Findings library behind the port; two open findings about FEAT::CHECKOUT, one about the module
    # that owns the seeds, a resolved one about MOD::STRIPE and a finding about a merged-away id.
    memory = MemoryGapExecutor()
    for spec_id, label in (
        ("FEAT::CHECKOUT", "Feature"),
        ("MOD::STRIPE", "Module"),
        ("MOD::PAYMENTS_SERVICE", "Module"),
    ):
        memory.spec(spec_id, label)
    findings = Findings(memory, load_ontology(), clock=Clock())

    async def add(text: str, about: list[str], by: str = "u1") -> Any:
        return await findings.add(PROJECT, created_by=by, kind="gap", text=text, about=about)

    await add("one", ["FEAT::CHECKOUT"])
    await add("two", ["FEAT::CHECKOUT", "MOD::STRIPE"], by="u2")
    await add("three", ["MOD::PAYMENTS_SERVICE"])
    closed = (await add("four", ["MOD::STRIPE"])).finding
    await findings.resolve(PROJECT, closed.finding_id, by="lead")

    trace = await service(findings=findings).trace(PROJECT, spec_ids=SEEDS, direction="affects", depth=2)

    assert {spec_id: node.findings for spec_id, node in by_id(trace.nodes).items()} == {
        "FEAT::CHECKOUT": 2,
        "MOD::STRIPE": 1,
        "MOD::WEB_SHOP": 0,
        "REQ::FR::CHECKOUT": 0,
        "REQ::Q::CHECKOUT_SPEED": 0,
    }
    assert [(c.spec_id, c.findings) for c in trace.context] == [("MOD::PAYMENTS_SERVICE", 1)]


async def test_without_a_findings_port_every_count_is_zero() -> None:
    trace = await service().trace(PROJECT, spec_ids=SEEDS, direction="affects", depth=1)

    assert {node.findings for node in trace.nodes} == {0}


async def test_a_project_without_a_plan_still_traces_with_null_plan_fields() -> None:
    trace = await service(
        LoadedPlan(shop_plan().current.model_copy(update={"plan": None, "tasks": (), "stories": ()}))
    ).trace(PROJECT, spec_ids=SEEDS, direction="affects", depth=2)

    assert len(trace.nodes) == 5 and trace.stories == () and trace.summary is None
    assert {node.plan_state for node in trace.nodes} == {None} and {node.tasks for node in trace.nodes} == {()}


async def test_a_story_traces_from_its_anchor_and_the_nodes_its_tasks_implement() -> None:
    # Scenario: story CHECKOUT is anchored on FEAT::CHECKOUT and implements it and two requirements; its own story is
    # left out of ``stories``.
    trace = await service().trace(PROJECT, story_id=CHECKOUT, direction="based_on", depth=1)

    assert trace.sources == ("FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED")
    assert not trace.seeds_truncated
    assert CHECKOUT not in {story.story_id for story in trace.stories}


async def test_a_story_with_more_nodes_than_one_walk_takes_is_cut_to_fifty_seeds() -> None:
    graph = shop_graph()
    ids = tuple(f"REQ::FR::BULK_{n:02d}" for n in range(60))
    for spec_id in ids:
        graph.node(spec_id, "FunctionalRequirement")
    plan = shop_plan(tasks=(task("TASK::WEB_SHOP::BULK", CHECKOUT, ids),))

    trace = await service(plan, graph=ShopSpecGraph(graph)).trace(
        PROJECT, story_id=CHECKOUT, direction="affects", depth=1
    )

    assert len(trace.sources) == 50 and trace.seeds_truncated
    assert trace.sources[0] == "FEAT::CHECKOUT"  # the anchor comes first


async def test_open_questions_of_the_seeds_and_reached_nodes_come_with_the_trace() -> None:
    trace = await service().trace(PROJECT, spec_ids=["ENT::ORDER"], direction="affects", depth=1)

    assert [(q.spec_id, q.about) for q in trace.open_questions] == [("OQ::PAYMENT_PROVIDERS", "ENT::ORDER")]
    assert trace.open_questions[0].statement == "Which payment providers are supported?"


async def test_ids_that_resolve_to_no_node_are_listed_as_unknown() -> None:
    trace = await service().trace(PROJECT, spec_ids=["FEAT::CARD_PAYMENT", "FEAT::GONE"], direction="affects", depth=1)

    assert trace.unknown == ("FEAT::GONE",) and trace.nodes


async def test_a_trace_from_an_alias_resolves_tasks_through_merged_ids() -> None:
    # Scenario: the plan's task implements FEAT::OLD_CHECKOUT, which a build merged into FEAT::CHECKOUT.
    graph = shop_graph().alias("FEAT::CHECKOUT", "FEAT::OLD_CHECKOUT")
    plan = shop_plan(tasks=(task("TASK::WEB_SHOP::LEGACY", CHECKOUT, ("FEAT::OLD_CHECKOUT",)),))

    trace = await service(plan, graph=ShopSpecGraph(graph)).trace(PROJECT, spec_ids=SEEDS, direction="affects", depth=1)

    assert [t.task_id for t in by_id(trace.nodes)["FEAT::CHECKOUT"].tasks] == ["TASK::WEB_SHOP::LEGACY"]


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({}, "invalid_arguments"),
        ({"story_id": CHECKOUT, "spec_ids": SEEDS}, "invalid_arguments"),
    ],
)
async def test_exactly_one_seed_argument_is_required(arguments: dict[str, Any], code: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        await service().trace(PROJECT, direction="affects", **arguments)

    assert caught.value.code == code


async def test_story_errors_name_a_missing_plan_and_an_unknown_or_retired_story() -> None:
    empty = LoadedPlan(shop_plan().current.model_copy(update={"plan": None}))
    cases = [
        (service(empty), CHECKOUT, "no_plan"),
        (service(), "STORY::X::NONE", "story_not_found"),
        (service(shop_plan(retired=(CHECKOUT,))), CHECKOUT, "story_not_found"),
    ]
    for plan_service, story_id, code in cases:
        with pytest.raises(PlanReadError) as caught:
            await plan_service.trace(PROJECT, story_id=story_id, direction="affects")
        assert caught.value.code == code
