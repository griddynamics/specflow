"""Live plan updates after graph changes a build makes: a merge then a change, removal and return, a moved node.

Each scenario changes the spec graph in the store the way a build would and passes the build's change report,
then plans the update with the generic planners (they answer from each request, whatever the graph).
"""

from __future__ import annotations

from pathlib import Path

from graph.plan.files import FilePlanStore, open_plan_store
from graph.plan.models import BuildChangeReport, ChangedSpecEdge, ChangedSpecNode, CurrentPlan, PlanResult
from graph.plan.run import StepFunctions, run_with
from graph.plan.snapshot import read_current_plan
from graph_plan_support import generic_planners, live_context, plan_ontology, plan_rules, shop_graph, write_spec_graph
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, Match, ProbeNode, select
from graph_support import GraphProject

pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph()

COMMIT = "34" * 20
EXPORT_NODES = ("FEAT::ORDER_HISTORY_EXPORT", "REQ::FR::ORDER_HISTORY_EXPORT", "REQ::LOGIC_EXPORT")
# The key property that names a spec or plan node within its project.
NODE_KEYS = ("spec_id", "task_id", "story_id", "area_id", "plan_id")


async def _plan(
    project: GraphProject, project_id: str, store: FilePlanStore, build_id: str, *reports: BuildChangeReport
) -> PlanResult:
    rules = plan_rules()
    context = live_context(
        project, project_id, store, generic_planners(rules), build_id=build_id, changes=reports, rules=rules
    )
    return await run_with(context, StepFunctions())


async def _first_plan(project: GraphProject, tmp_path: Path) -> tuple[str, FilePlanStore]:
    project_id = project.new_project()
    await write_spec_graph(project, project_id, shop_graph(), plan_ontology())
    store = open_plan_store(tmp_path)
    result = await _plan(project, project_id, store, "build-1")
    assert result.status == "succeeded" and result.checks is not None and result.checks.ok
    return project_id, store


async def _current(project: GraphProject, project_id: str) -> CurrentPlan:
    return await read_current_plan(project.executor, project_id)


async def _complete_implementing(project: GraphProject, project_id: str, spec_id: str) -> list[str]:
    """Mark every task implementing ``spec_id`` completed (an agent's finished work); return their ids."""

    tasks = await project.nodes("Task", project_id=project_id)
    done = sorted(task["task_id"] for task in tasks if spec_id in (task.get("implements") or ()))
    for task_id in done:
        await project.update_nodes(
            select("Task", task_id=task_id), {"status": "completed", "commit": COMMIT}, project_id=project_id
        )
    return done


def _match(node: ProbeNode) -> Match:
    """The one node of its project ``node`` is, selected by its label and key."""

    key = next(name for name in NODE_KEYS if node.get(name) is not None)
    return select(node.kind, **{key: node[key]})


async def _move_plan_links(project: GraphProject, project_id: str, old: str, survivor: str) -> None:
    """Move the plan links of spec node ``old`` (incoming ``IMPLEMENTS``, outgoing ``IN_AREA``) to
    ``survivor`` with their properties, old keys included, as a build's merge does."""

    moved: list[EdgeSpec] = []
    gone: list[str] = []
    to_survivor = select("SpecNode", spec_id=survivor)
    for edge in await project.edges("IMPLEMENTS", project_id=project_id):
        if edge.target.get("spec_id") == old:
            moved.append(EdgeSpec("IMPLEMENTS", _match(edge.source), to_survivor, edge.props))
            gone.append(edge.id)
    for edge in await project.edges("IN_AREA", project_id=project_id):
        if edge.source.get("spec_id") == old:
            moved.append(EdgeSpec("IN_AREA", to_survivor, _match(edge.target), edge.props))
            gone.append(edge.id)
    assert moved, f"{old} has no plan link to move"
    await project.create_edges(moved, project_id=project_id)
    assert await project.probe.delete_edges(gone) == len(gone)


def _ok(result: PlanResult) -> None:
    assert result.status == "succeeded", result.failure
    assert result.checks is not None and result.checks.ok, result.checks.problems


async def test_a_merge_then_a_change_redoes_the_work_once_and_keeps_one_membership(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, store = await _first_plan(graph_project, tmp_path)
    done = await _complete_implementing(graph_project, project_id, "REQ::BR::ORDER_LIMIT")
    assert done
    # The build merges REQ::BR::ORDER_LIMIT into BR::RELEASE: plan relationships move to the survivor with
    # their old keys, the survivor records the old id as an alias, and the old node goes.
    await _move_plan_links(graph_project, project_id, "REQ::BR::ORDER_LIMIT", "BR::RELEASE")
    old = select("SpecNode", spec_id="REQ::BR::ORDER_LIMIT")
    assert await graph_project.delete_nodes(old, project_id=project_id) == 1
    await graph_project.update_nodes(
        select("SpecNode", spec_id="BR::RELEASE"),
        {"spec_id_aliases": ["REQ::BR::ORDER_LIMIT"], "last_build_id": "build-3"},
        project_id=project_id,
    )
    merged = BuildChangeReport(
        build_id="build-2",
        previous_build_id="build-1",
        nodes=(
            ChangedSpecNode(spec_id="BR::RELEASE", classes=("merged",), previous_spec_ids=("REQ::BR::ORDER_LIMIT",)),
        ),
    )
    changed = BuildChangeReport(
        build_id="build-3",
        previous_build_id="build-2",
        nodes=(ChangedSpecNode(spec_id="BR::RELEASE", classes=("changed_meaning",)),),
    )

    result = await _plan(graph_project, project_id, store, "build-3", merged, changed)

    _ok(result)
    superseded = {item.task_id: item for item in result.changes.tasks_superseded}
    assert set(done) <= set(superseded)
    assert all(superseded[task_id].successor == f"{task_id}_R2" for task_id in done)
    memberships = [link for link in (await _current(graph_project, project_id)).links if link.type == "IN_AREA"]
    assert [link.target for link in memberships if link.source == "BR::RELEASE"] == ["AREA::ORDERS_SERVICE"]


async def test_removed_work_is_retired_and_revived_under_its_id_when_it_returns(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, store = await _first_plan(graph_project, tmp_path)
    before = await _current(graph_project, project_id)
    export_tasks = {task.task_id for task in before.tasks if set(task.implements) & set(EXPORT_NODES)}
    assert export_tasks
    for spec_id in EXPORT_NODES:
        assert await graph_project.delete_nodes(select("SpecNode", spec_id=spec_id), project_id=project_id) == 1
    removed = BuildChangeReport(
        build_id="build-2",
        previous_build_id="build-1",
        nodes=tuple(ChangedSpecNode(spec_id=spec_id, classes=("removed",)) for spec_id in EXPORT_NODES),
    )

    gone = await _plan(graph_project, project_id, store, "build-2", removed)

    _ok(gone)
    assert export_tasks <= set(gone.changes.tasks_retired)
    assert "STORY::ORDERS_SERVICE::ORDER_HISTORY_EXPORT" in gone.changes.stories_retired

    await write_spec_graph(graph_project, project_id, shop_graph(), plan_ontology())
    returned = BuildChangeReport(
        build_id="build-3",
        previous_build_id="build-2",
        nodes=tuple(ChangedSpecNode(spec_id=spec_id, classes=("added",)) for spec_id in EXPORT_NODES),
    )

    back = await _plan(graph_project, project_id, store, "build-3", returned)

    _ok(back)
    assert export_tasks <= set(back.changes.tasks_reopened)
    assert "STORY::ORDERS_SERVICE::ORDER_HISTORY_EXPORT" in back.changes.stories_reactivated
    statuses = {task.task_id: task.status for task in (await _current(graph_project, project_id)).tasks}
    assert {statuses[task_id] for task_id in export_tasks} == {"pending"}


async def test_an_orphan_attached_by_inference_moves_area_without_redoing_finished_work(
    graph_project: GraphProject, tmp_path: Path
) -> None:
    project_id, store = await _first_plan(graph_project, tmp_path)
    done = await _complete_implementing(graph_project, project_id, "REQ::FR::ORPHAN")
    assert done
    await graph_project.create_edges(
        [
            EdgeSpec(
                "PART_OF",
                select("SpecNode", spec_id="REQ::FR::ORPHAN"),
                select("SpecNode", spec_id="MOD::WEB_SHOP"),
                {
                    "group_id": project_id,
                    "edge_key": "REQ_FR_ORPHAN|PART_OF|MOD_WEB_SHOP",
                    "basis": "inferred",
                    "inference_rule": "orphan_membership",
                    "confidence": 0.91,
                },
            )
        ],
        project_id=project_id,
    )
    attached = BuildChangeReport(
        build_id="build-2",
        previous_build_id="build-1",
        edges=(
            ChangedSpecEdge(
                source="REQ::FR::ORPHAN", relation="PART_OF", target="MOD::WEB_SHOP", change="added", basis="inferred"
            ),
        ),
    )

    result = await _plan(graph_project, project_id, store, "build-2", attached)

    _ok(result)
    assert result.changes.tasks_superseded == ()
    assert set(done) <= set(result.changes.tasks_kept)
    memberships = [link for link in (await _current(graph_project, project_id)).links if link.type == "IN_AREA"]
    assert [link.target for link in memberships if link.source == "REQ::FR::ORPHAN"] == ["AREA::WEB_SHOP"]
    placed = next(link for link in memberships if link.source == "REQ::FR::ORPHAN")
    assert dict(placed.properties)["rule"] == 6
    tasks = (await _current(graph_project, project_id)).tasks
    moved = [task for task in tasks if "REQ::FR::ORPHAN" in task.implements and task.status == "pending"]
    assert [(task.area_id, task.story_id) for task in moved] == [("AREA::WEB_SHOP", "STORY::WEB_SHOP::CORE")]
