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
from graph_probes import requires_live_graph
from graph_support import GraphProject, ScriptedGateway

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

WORKSPACE = "TASK::PROJECT::WORKSPACE"
SPEC_LABELS = frozenset({"SpecNode", "Document", "Fragment"})
PLAN_LABELS = frozenset({"Plan", "WorkArea", "Story", "Task"})


async def _owned(project: GraphProject, project_id: str) -> list[Any]:
    """Every spec node, document and fragment of a project with all of its properties (in the probe's order)."""

    nodes = await project.nodes(project_id=project_id)
    return [(sorted(node.labels), dict(node.props)) for node in nodes if SPEC_LABELS.intersection(node.labels)]


async def test_two_plans_stay_apart_and_spec_properties_never_change(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    first, _ = await planned_live_shop(graph_project, tmp_path / "a")
    second, _ = await planned_live_shop(graph_project, tmp_path / "b")
    before = await _owned(graph_project, second)
    worked = await _owned(graph_project, first)
    ontology = plan_ontology()
    life = TaskLifecycle(graph_project.store, ontology, plan_rules(ontology))

    claim = await life.claim_task(first, WORKSPACE, claimed_by="agent")
    await life.complete_task(first, WORKSPACE, claim_token=claim.claim_token, commit="ab" * 20)

    assert [task.task_id for task in await life.ready_tasks(second)] == [WORKSPACE]
    assert [task.task_id for task in await life.ready_tasks(first)] == ["TASK::PROJECT::TOOLCHAIN"]
    assert await _owned(graph_project, second) == before
    assert await _owned(graph_project, first) == worked
    plan_nodes = [node for node in await graph_project.nodes(project_id=first) if PLAN_LABELS.intersection(node.labels)]
    assert plan_nodes and all("SpecNode" not in node.labels for node in plan_nodes)
    crossing = [
        edge
        for edge in await graph_project.edges(project_id=first)
        if (edge.source.get("group_id") == first) != (edge.target.get("group_id") == first)
    ]
    assert crossing == []


async def test_a_plan_run_leaves_every_spec_document_and_fragment_property_as_it_was(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id = graph_project.new_project()
    await write_spec_graph(graph_project, project_id, shop_graph(), plan_ontology())
    before = await _owned(graph_project, project_id)

    gateway = script_plan(ScriptedGateway(), shop_answers(await shop_view()))
    result = await run_with(
        live_context(graph_project, project_id, open_plan_store(tmp_path), gateway), StepFunctions()
    )

    assert result.status == "succeeded"
    assert await _owned(graph_project, project_id) == before
