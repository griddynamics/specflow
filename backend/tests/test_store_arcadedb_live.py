"""Live tests of ArcadeDB-only behaviour: how verify and repair address nodes and edges that carry no uuid.

A label write moves a record on ArcadeDB (its element id changes, and its edges get new ones), so a repair
keyed by an element id read earlier misses its target. The adapter addresses a node by uuid and an edge by
uuid, else by type and edge key; an element id is the last resort. These tests seed the odd shapes through the
probe and run the library's own verify and repair queries.
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np
import pytest

from graph.build import queries as build_queries
from graph.core import NodeRef, OwnedWriter
from graph_probes import requires_live_graph
from graph_probes.base import EdgeSpec, NodeSpec, select
from graph_support import GraphProject

# The graph fixtures (graph_project) live in graph_support.
pytest_plugins = ["graph_support"]
pytestmark = requires_live_graph(only="arcadedb")

LABELS = ["Module", "Feature", "FunctionalRequirement"]
RELATIONS = ["PART_OF"]


async def verified_nodes(project: GraphProject) -> dict[str, dict[str, Any]]:
    rows = await project.store.executor().read(
        build_queries.VERIFY_SPEC_NODES, project.project_id, labels=LABELS, attributes=[]
    )
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
    assert loose and loose not in {"u-keep", "MOD::NOUUID"}
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


async def test_a_keyed_node_changes_label_in_one_transaction_and_keeps_its_keys_and_relationships(
    graph_project: GraphProject,
) -> None:
    """A node written by the library (uuid and spec_key) changes label without a second record move: ArcadeDB
    refuses two moves of one record in one transaction, so the repair copies the node and moves its edges."""

    project, pid = graph_project, graph_project.project_id
    writer = OwnedWriter(project.store, project.ontology, "build", pid)
    for label, key in (("BusinessRule", "a"), ("Feature", "b"), ("Module", "m")):
        spec_id = f"{project.ontology.id_prefix(label)}{key.upper()}"
        await writer.upsert_node(label, f"u-{key}", {"name": key, "identity_key": f"k-{key}", "spec_id": spec_id})
    await writer.upsert_relationship("PART_OF", NodeRef("BusinessRule", "u-a"), NodeRef("Module", "u-m"), "e1", {})
    await project.create_edges(
        [EdgeSpec("RELATES_TO", select("Feature", uuid="u-b"), select("BusinessRule", uuid="u-a"), {"edge_key": "e2"})]
    )
    rows = [
        {"id": "u-a", "label": "FunctionalRequirement", "remove": ["BusinessRule"], "label_disagreement": None},
        {"id": "u-b", "label": "FunctionalRequirement", "remove": ["Feature"], "label_disagreement": "was a feature"},
    ]
    async with project.store.executor().tx(pid) as tx:
        moved = await tx.run(build_queries.REPAIR_SET_LABELS, rows=rows)
    assert moved == [{"written": 2}]
    nodes = {n["uuid"]: n for n in await project.nodes("SpecNode")}
    assert set(nodes) == {"u-a", "u-b", "u-m"}  # no copy left under its temporary uuid
    assert sorted(nodes["u-a"].labels) == sorted(nodes["u-b"].labels) == ["FunctionalRequirement", "SpecNode"]
    assert nodes["u-a"]["spec_id"] == "REQ::BR::A" and nodes["u-b"]["label_disagreement"] == "was a feature"
    found = await project.reader().node(pid, "REQ::BR::A")  # the spec_id lookup still finds it
    assert found is not None and found.uuid == "u-a" and found.label == "FunctionalRequirement"
    edges = {(e.type, e.source["uuid"], e.target["uuid"], e.get("edge_key")) for e in await project.edges()}
    assert edges == {("PART_OF", "u-a", "u-m", "e1"), ("RELATES_TO", "u-b", "u-a", "e2")}
    assert set(await verified_nodes(project)) == {"REQ::BR::A", "FEAT::B", "MOD::M"}


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


# Scale: a library-size persist batch carrying vectors, and inference over many candidates (D1, D2).
BATCH_ROWS = int(os.environ.get("ARCADEDB_LIVE_SCALE_ROWS", "500"))
DIMENSION = int(os.environ.get("GRAPH_EMBEDDING_DIM", "3072"))
TIED_ROWS = 6  # the first rows share one vector, so their scores tie and only spec_id, then uuid, order them
TOP = 5


def scale_rows() -> list[dict[str, Any]]:
    """``BATCH_ROWS`` create rows of ``Feature`` nodes with two ``DIMENSION``-float vectors each (what the library
    writes: ``embedding`` and ``name_embedding`` inside ``props``)."""

    generator = np.random.default_rng(7)
    vectors = generator.standard_normal((BATCH_ROWS, DIMENSION)).astype(np.float32)
    vectors[:TIED_ROWS] = vectors[0]
    names = generator.standard_normal((BATCH_ROWS, DIMENSION)).astype(np.float32)
    rows = []
    for index in range(BATCH_ROWS):
        uuid = f"u-{index:04d}"
        props = {
            "uuid": uuid,
            "spec_id": f"FEAT::T{TIED_ROWS - index}"
            if index < TIED_ROWS
            else f"FEAT::{index:04d}",  # ties: reversed ids
            "name": f"feature {index}",
            "embedding": vectors[index].tolist(),
            "name_embedding": names[index].tolist(),
        }
        rows.append({"uuid": uuid, "label": "Feature", "props": props})
    return rows


def expected_top(rows: list[dict[str, Any]], limit: int) -> dict[str, list[tuple[str, float]]]:
    """The canonical answer: per subject the ``limit`` best other nodes by (1 + cosine) / 2, ties by spec_id, uuid."""

    matrix = np.array([row["props"]["embedding"] for row in rows], dtype=np.float64)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    scores = (1 + matrix @ matrix.T) / 2
    answer = {}
    for index, row in enumerate(rows):
        ranked = sorted(
            (-round(float(scores[index, other]), 7), rows[other]["props"]["spec_id"], rows[other]["uuid"])
            for other in range(len(rows))
            if other != index
        )
        answer[row["uuid"]] = [(uuid, -score) for score, _spec, uuid in ranked[:limit]]
    return answer


async def test_a_library_size_batch_of_vector_rows_is_written_and_similar_targets_are_found(
    graph_project: GraphProject,
) -> None:
    project, pid = graph_project, graph_project.project_id
    executor = project.store.executor()
    rows = scale_rows()
    async with executor.tx(pid) as tx:  # one explicit transaction, as persist does: the chunks share it
        written = await tx.run(build_queries.CREATE_SPEC_NODES, rows=rows, build_id="b-scale")
    assert written == [{"written": len(rows)}]
    stored = await project.probe.nodes(pid, "Feature")
    assert len(stored) == len(rows)

    query = build_queries.infer_similar_targets("PART_OF", symmetric=False)
    found = await executor.read(
        query,
        pid,
        uuids=[row["uuid"] for row in rows],
        target_labels=["Feature"],
        excluded=[],
        bases=["stated", "implied"],
        limit=TOP,
    )
    assert [row["uuid"] for row in found] == [row["uuid"] for row in rows]  # one row per subject, by uuid
    expected = expected_top(rows, TOP)
    for row in found:
        got = [(item["uuid"], round(item["score"], 5)) for item in row["candidates"]]
        assert [uuid for uuid, _ in got] == [uuid for uuid, _ in expected[row["uuid"]]], row["uuid"]
        assert [score for _, score in got] == pytest.approx([score for _, score in expected[row["uuid"]]], abs=1e-4)
