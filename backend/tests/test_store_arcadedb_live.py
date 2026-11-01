"""Live tests of ArcadeDB-only behaviour: how verify and repair address nodes and edges that carry no uuid.

A label write moves a record on ArcadeDB (its element id changes, and its edges get new ones), so a repair
keyed by an element id read earlier misses its target. The adapter addresses a node by uuid and an edge by
uuid, else by type and edge key; an element id is the last resort. These tests seed the odd shapes through the
probe and run the library's own verify and repair queries.
"""

from __future__ import annotations

from typing import Any

from graph.build import queries as build_queries
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, NodeSpec, select
from graph_support import GraphProject

# The graph fixtures (graph_project) live in graph_support.
pytest_plugins = ["graph_support"]
pytestmark = requires_live_graph(only="arcadedb")

LABELS = ["Module", "Feature", "FunctionalRequirement"]
RELATIONS = ["PART_OF"]


async def verified_nodes(project: GraphProject) -> dict[str, dict[str, Any]]:
    rows = await project.store.executor().read(build_queries.VERIFY_SPEC_NODES, project.project_id, labels=LABELS)
    return {row["spec_id"]: row for row in rows}


async def verified_edges(project: GraphProject) -> list[dict[str, Any]]:
    return await project.store.executor().read(
        build_queries.VERIFY_DOMAIN_EDGES, project.project_id, relations=RELATIONS
    )


async def test_a_node_without_a_uuid_is_verified_and_repaired_by_its_element_id(graph_project: GraphProject) -> None:
    project, pid = graph_project, graph_project.project_id
    await project.probe.create_nodes(
        pid,
        [
            NodeSpec(("SpecNode", "Module"), {"spec_id": "MOD::NOUUID", "name": "n"}),
            NodeSpec(("SpecNode", "Module"), {"uuid": "u-keep", "spec_id": "MOD::KEEP", "name": "k"}),
        ],
    )
    nodes = await verified_nodes(project)
    assert nodes["MOD::KEEP"]["id"] == "u-keep"  # a node with a uuid is addressed by it
    loose = nodes["MOD::NOUUID"]["id"]
    assert loose and loose != "u-keep" and loose != "MOD::NOUUID"
    async with project.store.executor().tx(pid) as tx:
        written = await tx.run(
            build_queries.REPAIR_SET_SPEC_IDS, rows=[{"id": loose, "spec_id": "MOD::FIXED", "aliases": None}]
        )
    assert written == [{"written": 1}]
    assert (await project.probe.one_node(pid, "Module", name="n"))["spec_id"] == "MOD::FIXED"
    assert (await project.probe.one_node(pid, "Module", name="k"))["spec_id"] == "MOD::KEEP"  # nothing else touched


async def test_repairs_never_touch_a_node_of_another_project_with_the_same_uuid(graph_project: GraphProject) -> None:
    project, pid, other = graph_project, graph_project.project_id, graph_project.new_project()
    for target in (pid, other):
        await project.probe.create_nodes(
            target, [NodeSpec(("SpecNode", "Module"), {"uuid": "u-same", "spec_id": "MOD::A", "name": "a"})]
        )
    async with project.store.executor().tx(pid) as tx:
        written = await tx.run(
            build_queries.REPAIR_SET_SPEC_IDS, rows=[{"id": "u-same", "spec_id": "MOD::B", "aliases": None}]
        )
    assert written == [{"written": 1}]
    assert (await project.probe.one_node(pid, "Module"))["spec_id"] == "MOD::B"
    assert (await project.probe.one_node(other, "Module"))["spec_id"] == "MOD::A"


async def seed_edge_without_uuid(project: GraphProject, props: dict[str, Any]) -> None:
    pid = project.project_id
    await project.probe.create_nodes(
        pid,
        [
            NodeSpec(
                ("SpecNode", "FunctionalRequirement", "Feature"), {"uuid": "u-feat", "spec_id": "FEAT::F", "name": "f"}
            ),
            NodeSpec(("SpecNode", "Module"), {"uuid": "u-mod", "spec_id": "MOD::M", "name": "m"}),
        ],
    )
    await project.probe.create_edges(
        pid,
        [
            EdgeSpec(
                "PART_OF",
                select("FunctionalRequirement", uuid="u-feat"),
                select("Module", uuid="u-mod"),
                {"group_id": pid, **props},
            )
        ],
    )


async def test_an_edge_without_a_uuid_is_repaired_by_type_and_edge_key_after_its_end_changed_label(
    graph_project: GraphProject,
) -> None:
    project, pid = graph_project, graph_project.project_id
    await seed_edge_without_uuid(project, {"edge_key": "k1", "source_fragment_ids": ["x"]})
    (edge,) = await verified_edges(project)
    assert edge["id"] == "PART_OF|k1"  # not an element id, which the label write below makes stale
    assert edge["source"] == "u-feat" and edge["target"] == "u-mod"
    async with project.store.executor().tx(pid) as tx:
        moved = await tx.run(
            build_queries.REPAIR_SET_LABELS,
            rows=[
                {"id": "u-feat", "label": "FunctionalRequirement", "remove": ["Feature"], "label_disagreement": None}
            ],
        )
        fixed = await tx.run(
            build_queries.REPAIR_EDGE_EVIDENCE,
            rows=[{"id": edge["id"], "fragment_ids": ["a", "b"]}],
            relations=RELATIONS,
        )
    assert moved == [{"written": 1}] and fixed == [{"written": 1}]
    (after,) = await project.probe.edges(pid, "PART_OF")
    assert after["source_fragment_ids"] == ["a", "b"] and after.source.kind == "FunctionalRequirement"


async def test_an_edge_without_uuid_or_edge_key_falls_back_to_its_element_id(graph_project: GraphProject) -> None:
    project, pid = graph_project, graph_project.project_id
    await seed_edge_without_uuid(project, {"source_fragment_ids": ["x"]})
    (edge,) = await verified_edges(project)
    assert edge["id"].startswith("#")  # a record id: valid until a label write moves an end
    async with project.store.executor().tx(pid) as tx:
        fixed = await tx.run(
            build_queries.REPAIR_EDGE_EVIDENCE, rows=[{"id": edge["id"], "fragment_ids": ["z"]}], relations=RELATIONS
        )
    assert fixed == [{"written": 1}]
