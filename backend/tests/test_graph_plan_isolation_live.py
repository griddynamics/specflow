"""Plans of two projects stay apart, and plan writes never touch spec, document or fragment properties."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from graph.plan.files import open_plan_store
from graph.plan.lifecycle import TaskLifecycle
from graph.plan.run import StepFunctions, run_with
from graph_plan_support import (
    live_context,
    plan_ontology,
    plan_rules,
    planned_live_shop,
    script_plan,
    shop_answers,
    shop_graph,
    shop_view,
    write_spec_graph,
)
from graph_support import Neo4jProject, ScriptedGateway
from specflow_live import requires_live

pytest_plugins = ["graph_support"]

pytestmark = requires_live("NEO4J")

WORKSPACE = "TASK::PROJECT::WORKSPACE"


async def _owned(project: Neo4jProject, project_id: str) -> list[Any]:
    """Every spec node, document and fragment of a project with all of its properties."""

    rows = await project.run(
        "MATCH (n {group_id: $p}) WHERE n:SpecNode OR n:Document OR n:Fragment "
        "RETURN labels(n) AS labels, properties(n) AS properties ORDER BY elementId(n)",
        p=project_id,
    )
    return [(sorted(row["labels"]), dict(row["properties"])) for row in rows]


async def test_two_plans_stay_apart_and_spec_properties_never_change(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    first, _ = await planned_live_shop(neo4j_project, tmp_path / "a")
    second, _ = await planned_live_shop(neo4j_project, tmp_path / "b")
    before = await _owned(neo4j_project, second)
    worked = await _owned(neo4j_project, first)
    ontology = plan_ontology()
    life = TaskLifecycle(neo4j_project.driver, ontology, plan_rules(ontology), database=neo4j_project.database)

    claim = await life.claim_task(first, WORKSPACE, claimed_by="agent")
    await life.complete_task(first, WORKSPACE, claim_token=claim.claim_token, commit="ab" * 20)

    assert [task.task_id for task in await life.ready_tasks(second)] == [WORKSPACE]
    assert [task.task_id for task in await life.ready_tasks(first)] == ["TASK::PROJECT::TOOLCHAIN"]
    assert await _owned(neo4j_project, second) == before
    assert await _owned(neo4j_project, first) == worked
    plan_labels = await neo4j_project.run(
        "MATCH (n {group_id: $p}) WHERE n:Plan OR n:WorkArea OR n:Story OR n:Task RETURN DISTINCT labels(n) AS labels",
        p=first,
    )
    assert all("SpecNode" not in row["labels"] for row in plan_labels)
    crossing = await neo4j_project.run(
        "MATCH (a)-[r]->(b) WHERE a.group_id = $p AND b.group_id <> $p RETURN count(r) AS crossing", p=first
    )
    assert crossing[0]["crossing"] == 0


async def test_a_plan_run_leaves_every_spec_document_and_fragment_property_as_it_was(
    neo4j_project: Neo4jProject, tmp_path: Path
) -> None:
    project_id = neo4j_project.new_project()
    await write_spec_graph(neo4j_project, project_id, shop_graph(), plan_ontology())
    before = await _owned(neo4j_project, project_id)

    gateway = script_plan(ScriptedGateway(), shop_answers(await shop_view()))
    result = await run_with(
        live_context(neo4j_project, project_id, open_plan_store(tmp_path), gateway), StepFunctions()
    )

    assert result.status == "succeeded"
    assert await _owned(neo4j_project, project_id) == before
