"""Store-neutral read views the live suites share: rows computed from a probe's nodes and edges.

Each view returns what a query of the same name used to return (dict rows keyed as the tests read
them), so an assertion reads the same on every store. Nothing here is store-specific: it is Python over
``GraphProbe.nodes`` and ``GraphProbe.edges``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from graph_probes.base import GraphProbe, ProbeEdge

SPEC = "SpecNode"
LIVE_STATUSES = ("active", "skipped")


async def domain_edges(probe: GraphProbe, project_id: str) -> list[dict[str, Any]]:
    """Every edge between two spec nodes whose source is in the project, as rows of ``relation``,
    ``source`` and ``target`` (the end's domain label), ``group_id``, ``uuid``, ``edge_key``, ``basis``
    and ``fragments`` (``source_fragment_ids``)."""

    return [
        {
            "relation": e.type,
            "source": e.source.kind,
            "target": e.target.kind,
            "group_id": e.get("group_id"),
            "uuid": e.get("uuid"),
            "edge_key": e.get("edge_key"),
            "basis": e.get("basis"),
            "fragments": e.get("source_fragment_ids"),
        }
        for e in await spec_edges(probe, project_id)
    ]


async def spec_edges(probe: GraphProbe, project_id: str) -> list[ProbeEdge]:
    """Edges from a spec node of the project to a spec node."""

    return [
        e
        for e in await probe.edges(project_id)
        if SPEC in e.source.labels and SPEC in e.target.labels and e.source.get("group_id") == project_id
    ]


async def identity_counts(probe: GraphProbe, project_id: str) -> dict[str, int]:
    """Spec nodes of the project and how many distinct spec ids, uuids and identity keys they carry."""

    nodes = await probe.nodes(project_id, SPEC)
    return {
        "nodes": len(nodes),
        "spec_ids": len({n.get("spec_id") for n in nodes if n.get("spec_id") is not None}),
        "uuids": len({n.get("uuid") for n in nodes if n.get("uuid") is not None}),
        "identity_keys": len({n.get("identity_key") for n in nodes if n.get("identity_key") is not None}),
    }


async def ungrounded_spec_ids(
    probe: GraphProbe, project_id: str, statuses: Sequence[str] = LIVE_STATUSES
) -> list[dict[str, Any]]:
    """Spec nodes of the project with no ``REFERENCED_BY`` edge to a fragment of the project whose
    status is one of ``statuses``, as ``{"spec_id": ...}`` rows."""

    grounded = {
        e.source.id
        for e in await probe.edges(project_id, "REFERENCED_BY")
        if e.target.get("group_id") == project_id and e.target.get("status") in statuses
    }
    return [{"spec_id": n.get("spec_id")} for n in await probe.nodes(project_id, SPEC) if n.id not in grounded]


async def fragment_ids(probe: GraphProbe, project_id: str, *, not_status: str | None = None) -> set[str]:
    """Ids of the project's fragments (those whose status is not ``not_status`` when given)."""

    return {
        f.get("fragment_id")
        for f in await probe.nodes(project_id, "Fragment")
        if not_status is None or (f.get("status") is not None and f.get("status") != not_status)
    }
