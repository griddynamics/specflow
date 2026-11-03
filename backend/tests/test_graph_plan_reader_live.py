"""Live plan reads and task briefs on the persisted shop plan."""

from __future__ import annotations

from pathlib import Path

from graph.plan.lifecycle import TaskLifecycle
from graph.plan.models import PlanDag
from graph.plan.reader import PlanReader
from graph_plan_support import plan_ontology, plan_rules, planned_live_shop
from graph_support import Neo4jProject
from specflow_live import requires_live

pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

WORKSPACE = "TASK::PROJECT::WORKSPACE"
TOOLCHAIN = "TASK::PROJECT::TOOLCHAIN"
PLACE = "TASK::ORDERS_SERVICE::LOGIC_PLACE_ORDER"
COMMIT = "cd" * 20


def _reader(neo4j_project: Neo4jProject) -> PlanReader:
    ontology = plan_ontology()
    return PlanReader(neo4j_project.driver, ontology, plan_rules(ontology), database=neo4j_project.database)


def _lifecycle(neo4j_project: Neo4jProject) -> TaskLifecycle:
    ontology = plan_ontology()
    return TaskLifecycle(neo4j_project.driver, ontology, plan_rules(ontology), database=neo4j_project.database)


async def test_plan_areas_and_stories_carry_members_counts_and_progress(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    reader = _reader(neo4j_project)
    life = _lifecycle(neo4j_project)

    plan = await reader.plan(project_id)
    assert plan is not None and plan.consumed_build_ids == ("build-1",)
    orders = next(area for area in await reader.areas(project_id) if area.area_id == "AREA::ORDERS_SERVICE")
    assert "MOD::ORDERS_SERVICE" in orders.members and orders.task_counts["pending"] >= 1
    assert all(story.progress == "pending" for story in await reader.stories(project_id))

    claim = await life.claim_task(project_id, WORKSPACE, claimed_by="agent")
    assert [story.story_id for story in await reader.stories(project_id, status="in_progress")] == [
        "STORY::PROJECT::SETUP"
    ]
    await life.complete_task(project_id, WORKSPACE, claim_token=claim.claim_token, commit=COMMIT)
    assert [story.story_id for story in await reader.stories(project_id, release="MVP")] == [
        "STORY::ORDERS_SERVICE::PLACE_ORDER"
    ]


async def test_a_story_lists_its_members_tasks_and_neighbour_stories(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)

    detail = await _reader(neo4j_project).story(project_id, "STORY::WEB_SHOP::CHECKOUT")

    assert detail is not None
    assert detail.anchor is not None and detail.anchor["spec_id"] == "FEAT::CHECKOUT"
    members = {member.spec_id: member for member in detail.members}
    assert set(members) == {"FEAT::CHECKOUT", "REQ::FR::CHECKOUT", "REQ::Q::CHECKOUT_SPEED"}
    assert all(member.covered for member in members.values())
    assert detail.tasks[0].task_id == "TASK::WEB_SHOP::LOGIC_CHECKOUT"
    assert "STORY::PROJECT::SETUP" in detail.waits_on
    assert "STORY::PROJECT::CLOSING" in detail.waited_by


async def test_story_work_lists_unfinished_prerequisites_first(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)

    work = await _reader(neo4j_project).story_work(project_id, "STORY::WEB_SHOP::CHECKOUT")

    assert work is not None
    order = [entry.task_id for entry in work.entries]
    assert order[:2] == [WORKSPACE, TOOLCHAIN]
    assert order.index("TASK::ORDERS_SERVICE::CONTRACT_ORDERS_API") < order.index("TASK::WEB_SHOP::LOGIC_CHECKOUT")
    assert [entry.task_id for entry in work.entries if entry.ready] == [WORKSPACE]


async def test_the_plan_dag_is_acyclic_grouped_by_story_and_renders_as_mermaid(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    reader = _reader(neo4j_project)

    dag = await reader.plan_dag(project_id)
    mermaid = await reader.plan_dag(project_id, output="mermaid")

    assert isinstance(dag, PlanDag)
    position = {task.task_id: index for index, task in enumerate(dag.tasks)}
    assert all(position[target] < position[source] for source, target in dag.edges)
    assert {story.story_id for story in dag.stories} >= {"STORY::PROJECT::SETUP", "STORY::WEB_SHOP::CHECKOUT"}
    assert isinstance(mermaid, str) and mermaid.startswith("flowchart TD")
    assert mermaid.count("subgraph ") == len(dag.stories)


async def test_tasks_and_tasks_for_spec_nodes(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    reader = _reader(neo4j_project)

    found = await reader.tasks_for(project_id, ["REQ::FR::PLACE_ORDER"])
    task = await reader.task(project_id, TOOLCHAIN)

    assert [item.task_id for item in found] == [PLACE]
    assert task is not None and task.waits_for == (WORKSPACE,) and TOOLCHAIN not in task.waited_by
    assert {item.phase for item in await reader.tasks(project_id, area_id="AREA::PROJECT")} >= {"setup"}
    assert await reader.task(project_id, "TASK::PROJECT::NOPE") is None


async def test_a_task_brief_shows_finished_contracts_spec_evidence_and_placement(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    life = _lifecycle(neo4j_project)
    claim = await life.claim_task(project_id, WORKSPACE, claimed_by="agent")
    await life.complete_task(project_id, WORKSPACE, claim_token=claim.claim_token, commit=COMMIT)

    brief = await life.task_brief(project_id, TOOLCHAIN)
    place = await life.task_brief(project_id, PLACE)
    limited = await life.task_brief(project_id, PLACE, neighbours=0)
    order = await life.task_brief(project_id, "TASK::ORDERS_DATABASE::DATA_ORDER")

    assert [(item.task_id, item.commit) for item in brief.contracts] == [(WORKSPACE, COMMIT)]
    assert brief.profile["conventions"][0]["spec_ids"] == ["CON::EU_REGION"]
    assert brief.spec_changed is False and brief.missing == ()
    assert {node.spec_id for node in place.implements} == {"FEAT::PLACE_ORDER", "REQ::FR::PLACE_ORDER"}
    assert [item.spec_id for item in place.neighbours] == ["MOD::ORDERS_SERVICE"] and limited.neighbours == ()
    placement = {item.spec_id: item for item in place.placement}
    assert placement["REQ::FR::PLACE_ORDER"].rule == 6
    assert placement["REQ::FR::PLACE_ORDER"].via[0]["basis"] == "stated"
    assert place.story is not None and place.story.story_id == "STORY::ORDERS_SERVICE::PLACE_ORDER"
    assert [question.spec_id for question in order.open_questions] == ["OQ::PAYMENT_PROVIDERS"]


async def test_a_brief_flags_a_node_written_by_a_build_the_plan_did_not_consume(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, spec_id: 'REQ::FR::PLACE_ORDER'}) SET n.last_build_id = 'build-7'",
        p=project_id,
    )

    brief = await _lifecycle(neo4j_project).task_brief(project_id, PLACE)

    assert brief.spec_changed is True


async def test_mutually_waiting_stories_collapse_into_one_group(neo4j_project: Neo4jProject, tmp_path: Path) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    # Checkout's logic waits on the Stripe API contract; the Stripe integration task now waits on checkout's
    # quality task: the two stories wait on each other while the tasks stay acyclic.
    await neo4j_project.run(
        "MATCH (a:Task {group_id: $p, task_id: $a}), (b:Task {group_id: $p, task_id: $b}) "
        "CREATE (a)-[:WAITS_FOR {group_id: $p, edge_key: $k, source: 'stitch'}]->(b)",
        p=project_id,
        a="TASK::PAYMENTS_SERVICE::INTEGRATION_STRIPE_API",
        b="TASK::WEB_SHOP::QUALITY_CHECKOUT",
        k="TASK::PAYMENTS_SERVICE::INTEGRATION_STRIPE_API|WAITS_FOR|TASK::WEB_SHOP::QUALITY_CHECKOUT",
    )

    dag = await _reader(neo4j_project).plan_dag(project_id)

    assert isinstance(dag, PlanDag)
    grouped = [group for group in dag.story_groups if "STORY::WEB_SHOP::CHECKOUT" in group]
    assert grouped and "STORY::PAYMENTS_SERVICE::STRIPE_API" in grouped[0]
    assert len(dag.tasks) == len({task.task_id for task in dag.tasks})


async def test_a_story_lists_an_uncovered_member_and_stories_filter_by_area(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    reader = _reader(neo4j_project)
    await neo4j_project.run(
        "MATCH (t:Task {group_id: $p, task_id: 'TASK::WEB_SHOP::QUALITY_CHECKOUT'}) SET t.status = 'retired'",
        p=project_id,
    )

    detail = await reader.story(project_id, "STORY::WEB_SHOP::CHECKOUT")
    stories = await reader.stories(project_id, area_id="AREA::WEB_SHOP")

    assert detail is not None
    members = {member.spec_id: member for member in detail.members}
    assert members["REQ::Q::CHECKOUT_SPEED"].covered is False and members["REQ::Q::CHECKOUT_SPEED"].task_ids == ()
    assert [story.story_id for story in stories] == ["STORY::WEB_SHOP::CHECKOUT"]


async def test_a_brief_carries_evidence_neighbours_releases_and_missing_nodes(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id, _ = await planned_live_shop(neo4j_project, tmp_path)
    life = _lifecycle(neo4j_project)

    place = await life.task_brief(project_id, PLACE)
    export = await life.task_brief(project_id, "TASK::ORDERS_SERVICE::LOGIC_ORDER_HISTORY_EXPORT")
    order = await life.task_brief(project_id, "TASK::ORDERS_DATABASE::DATA_ORDER")

    requirement = next(node for node in export.implements if node.spec_id == "REQ::LOGIC_EXPORT")
    assert requirement.provenance and requirement.provenance[0]["line_start"] == 6
    assert place.neighbours[0].basis == "stated"
    assert place.task.release == ("MVP",) and place.story is not None and place.story.release == "MVP"
    assert {item.source for item in place.done_when} == {"planner", "spec"}
    assert order.task.has_open_questions is True

    await neo4j_project.run(
        "MATCH (n:SpecNode {group_id: $p, spec_id: 'REQ::FR::PLACE_ORDER'}) DETACH DELETE n", p=project_id
    )
    gone = await life.task_brief(project_id, PLACE)
    assert gone.missing == ("REQ::FR::PLACE_ORDER",)
