"""Fault injection on a built graph, through the probe: the faults the verifier must find and repair.

Each fault is a few probe calls on a ``GraphProject`` (no query language); selections are by ``spec_id``
order, so the same build gets the same faults on every store.
"""

from __future__ import annotations

from typing import Any

from graph_probes.base import EdgeSpec, NodeSpec, ProbeNode, select
from graph_support import GraphProject


def by_spec_id(n: ProbeNode) -> tuple[bool, str]:
    """Order by ``spec_id`` as the store sorts it: a node without one last."""

    return (n.get("spec_id") is None, n.get("spec_id") or "")


async def illegal_edge(project: GraphProject, uuid: str = "fault-illegal") -> str:
    """A legal-looking, grounded ``Module PART_OF FunctionalRequirement`` (a pair the ontology forbids)."""

    modules = await project.nodes("Module")
    grounded = [
        e
        for e in await project.edges("REFERENCED_BY")
        if "FunctionalRequirement" in e.source.labels
        and e.source.get("group_id") == project.project_id
        and e.target.get("status") == "active"
    ]
    assert modules and grounded
    module = min(modules, key=lambda m: m["spec_id"])
    ref = min(grounded, key=lambda e: (e.source["spec_id"], e.target["fragment_id"]))
    await project.create_edges(
        [
            EdgeSpec(
                "PART_OF",
                select("Module", uuid=module["uuid"]),
                select("FunctionalRequirement", uuid=ref.source["uuid"]),
                {
                    "group_id": project.project_id,
                    "edge_key": "fault|illegal",
                    "uuid": uuid,
                    "basis": "stated",
                    "source_fragment_ids": [ref.target["fragment_id"]],
                },
            )
        ]
    )
    return f"{module['spec_id']}|PART_OF|{ref.source['spec_id']}"


async def first_node(project: GraphProject, label: str, skip: int = 0) -> dict[str, Any]:
    nodes = sorted(await project.nodes(label), key=by_spec_id)
    assert len(nodes) > skip, (label, skip, len(nodes))
    n = nodes[skip]
    return {
        "uuid": n.get("uuid"),
        "spec_id": n.get("spec_id"),
        "key": n.get("identity_key"),
        "aliases": n.get("spec_id_aliases"),
    }


async def stray_node(project: GraphProject, *, grounded: bool, spec_id: str | None, label: str = "Module") -> None:
    """A spec node no build wrote: grounded in the first active fragment, or not at all."""

    await project.create_nodes(
        [
            NodeSpec(
                ("SpecNode", label),
                {
                    "uuid": "fault-node",
                    "name": "Payment Gateway",
                    "identity_key": "Module|name|payment gateway (fault)",
                    "spec_id": spec_id,
                },
            )
        ]
    )
    active = sorted(
        (f for f in await project.nodes("Fragment") if f.get("status") == "active"), key=lambda f: f["fragment_id"]
    )
    if grounded and active:
        await project.create_edges(
            [
                EdgeSpec(
                    "REFERENCED_BY",
                    select("SpecNode", uuid="fault-node"),
                    select("Fragment", fragment_id=active[0]["fragment_id"]),
                    {"group_id": project.project_id, "line_start": 1, "line_end": 1},
                )
            ]
        )
