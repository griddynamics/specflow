"""The scripted live scenarios whose canonical snapshots are the golden data of the graph stores.

Every scenario builds the shop corpus (``tests/fixtures/graph/shop``) into a fresh project with every
model answer hand-written and every embedding scripted, so the stored graph is the same on every run
and, if a store is right, on every store. A scenario returns the canonical snapshot
(``GraphProbe.canonical_snapshot``) of its project and a small ``meta`` of what the library reported.

- ``build``: the first build of snapshot 1, inference off.
- ``update``: snapshot 1, then updates to 2 and 3, inference off.
- ``infer``: the first build of snapshot 1 with inference on (the judge answers are hand-written).
- ``verify``: snapshot 1 with faults planted afterwards (illegal edge, second label, bad spec id,
  ungrounded edge, ungrounded stray node) and the verifier run on it: the repaired graph.
- ``renamed_ontology``: the chain 1 -> 2 -> 3 with inference on, under a renamed copy of the ontology.

``search_rankings`` is the search side: queries over the ``infer`` graph through ``GraphReader`` with
fulltext, the fused path with the exact vector walk and the fused path through the vector index.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np

from graph.build import steps
from graph.build.models import BuildResult
from graph.build.verify import VerificationReport
from graph.core import GraphReader, Ontology
from graph.core import reader as reader_module
from graph_probes.base import select
from graph_probes.faults import first_node, illegal_edge, stray_node
from graph_support import GraphProject, ScriptedGateway, ShopBuilds, live_llm_config

GatewayFactory = Callable[..., ScriptedGateway]
SCENARIOS: Final = ("build", "update", "infer", "verify", "renamed_ontology")
RANKING_LIMIT: Final = 10
SCORE_DIGITS: Final = 6


@dataclass(frozen=True)
class Outcome:
    """What one scenario leaves: the canonical snapshot and what the library reported."""

    snapshot: dict[str, Any]
    meta: dict[str, Any]
    project_id: str


def _meta(result: BuildResult, **more: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "settled": result.settled,
        "nodes_by_label": dict(sorted(result.metrics.nodes_by_label.items())),
        "edges_by_relation": dict(sorted(result.metrics.edges_by_relation.items())),
        **more,
    }


async def _chain(builds: ShopBuilds, snapshots: Sequence[int]) -> BuildResult:
    result: BuildResult | None = None
    for snapshot in snapshots:
        result, _ = await builds.build(snapshot)
        assert (result.failed_step, result.error_code) == (None, None), (snapshot, result.failed_step)
    assert result is not None
    return result


def _builds(project: GraphProject, factory: GatewayFactory, root: Path, name: str, **options: Any) -> ShopBuilds:
    return ShopBuilds(project, factory, Path(root) / name, project_id=project.new_project(), **options)


async def build(project: GraphProject, factory: GatewayFactory, root: Path) -> Outcome:
    builds = _builds(project, factory, root, "build", inference=False)
    result = await _chain(builds, (1,))
    return Outcome(await project.canonical_snapshot(builds.project_id), _meta(result), builds.project_id)


async def update(project: GraphProject, factory: GatewayFactory, root: Path) -> Outcome:
    builds = _builds(project, factory, root, "update", inference=False)
    result = await _chain(builds, (1, 2, 3))
    return Outcome(await project.canonical_snapshot(builds.project_id), _meta(result), builds.project_id)


async def infer(project: GraphProject, factory: GatewayFactory, root: Path) -> Outcome:
    builds = _builds(project, factory, root, "infer", inference=True)
    result = await _chain(builds, (1,))
    return Outcome(await project.canonical_snapshot(builds.project_id), _meta(result), builds.project_id)


async def verify(project: GraphProject, factory: GatewayFactory, root: Path) -> Outcome:
    builds = _builds(project, factory, root, "verify", inference=False)
    pid = builds.project_id
    result, gateway = await builds.build(1)
    assert (result.failed_step, result.error_code) == (None, None)
    scoped = GraphProject(probe=project.probe, ontology=project.ontology, project_id=pid)
    await illegal_edge(scoped)
    await scoped.update_nodes(
        select("SpecNode", uuid=(await first_node(scoped, "FunctionalRequirement"))["uuid"]),
        add_labels=["Feature"],
    )
    bad = await first_node(scoped, "FunctionalRequirement", skip=1)
    await scoped.update_nodes(select("SpecNode", uuid=bad["uuid"]), {"spec_id": "bad id"})
    stated = sorted(
        (
            e
            for e in await scoped.edges(group_id=pid, basis="stated")
            if "SpecNode" in e.source.labels and "SpecNode" in e.target.labels
        ),
        key=lambda e: e["edge_key"],
    )
    await project.probe.update_edges([stated[0].id], {"source_fragment_ids": []})
    await stray_node(scoped, grounded=False, spec_id="MOD::STRAY")
    ctx = builds.context(1, gateway).with_build_id(result.build_id)
    await steps.verify(ctx)
    payload = await builds.stores.builds.get_artifact(pid, result.build_id, "verify")
    assert payload is not None
    report = VerificationReport.model_validate(payload)
    assert report.ok, report.problems
    return Outcome(
        await project.canonical_snapshot(pid),
        {"found": dict(sorted(report.found.items())), "rounds": report.rounds, "ok": report.ok},
        pid,
    )


async def renamed_ontology(
    project: GraphProject,
    factory: GatewayFactory,
    root: Path,
    *,
    ontology: Ontology,
    rename: Mapping[str, str],
) -> Outcome:
    builds = _builds(project, factory, root, "renamed", ontology=ontology, rename=rename)
    result = await _chain(builds, (1, 2, 3))
    return Outcome(await project.canonical_snapshot(builds.project_id), _meta(result), builds.project_id)


# Search ---------------------------------------------------------------------------------------------

# Queries over the ``infer`` graph. ``near`` lists spec ids the query vector sits close to (cosine given):
# a scripted query vector is built from the stored vectors of those nodes, so the vector side is exact and
# deterministic on every store. A query without ``near`` has no vector hit (fulltext decides).
SEARCH_QUERIES: Final[tuple[dict[str, Any], ...]] = (
    {"text": "export order history", "near": {}},
    {"text": "checkout payment", "near": {}},
    {"text": "audit logging", "near": {}},
    {"text": "basket total quantity", "near": {}},
    {"text": "staff download CSV file", "near": {}},
    {"text": "authentication login", "near": {}},
    {"text": "orders service queue", "near": {}},
    {"text": "payment declined error", "near": {"CAP::AUDIT_LOGGING": 0.9, "FEAT::PAYMENT_FLOW": 0.7}},
    {"text": "export", "near": {"FEAT::ORDER_HISTORY_EXPORT": 0.95, "MOD::ORDERS_SERVICE": 0.8}},
    {"text": "zzqa vector only alpha", "near": {"FEAT::PAYMENT_FLOW": 0.95, "FEAT::BASKET_REVIEW": 0.8}},
    {"text": "zzqb vector only beta", "near": {"CAP::AUTHENTICATION": 0.9, "MOD::CHECKOUT_SERVICE": 0.7}},
    {"text": "zzqc vector only gamma", "near": {"FEAT::ORDER_CONFIRMATION": 0.85}},
)


class _QueryGateway:
    """The gateway side the reader needs: the embedding model and a scripted vector per query text."""

    def __init__(self, vectors: Mapping[str, list[float]]) -> None:
        self.config = live_llm_config()
        self._vectors = vectors

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]


def _query_vector(near: Mapping[str, float], stored: Mapping[str, np.ndarray], seed: int, dim: int) -> list[float]:
    """A unit vector at cosine ``near[id]`` to the stored vector of each id (they are near-orthogonal)."""

    noise = np.random.default_rng(seed).standard_normal(dim)
    anchors: list[tuple[np.ndarray, float]] = []
    for spec_id, cosine in near.items():
        anchors.append((stored[spec_id], cosine))
    basis: list[np.ndarray] = []
    vector = np.zeros(dim)
    for anchor, cosine in anchors:
        direction = anchor.copy()
        for earlier in basis:
            direction -= (direction @ earlier) * earlier
        direction /= np.linalg.norm(direction)
        basis.append(direction)
        vector += cosine * direction
    rest = 1.0 - float(vector @ vector)
    noise -= sum(((noise @ b) * b for b in basis), np.zeros(dim))
    noise /= np.linalg.norm(noise)
    vector += np.sqrt(max(rest, 0.0)) * noise
    return [float(x) for x in vector]


def _hits(hits: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {"spec_id": h.node.spec_id, "matched": h.matched, "score": round(float(h.score), SCORE_DIGITS)} for h in hits
    ]


async def search_rankings(project: GraphProject, project_id: str) -> dict[str, Any]:
    """Rankings of ``SEARCH_QUERIES`` over a built shop project, per mode, for the golden data."""

    await project.probe.await_indexes(60)
    nodes = await project.nodes("SpecNode", project_id=project_id)
    stored = {n["spec_id"]: np.array(n["embedding"], dtype=float) for n in nodes if n.get("embedding")}
    dim = live_llm_config().embedding_dim
    vectors = {
        q["text"]: _query_vector(q["near"], stored, index, dim) if q["near"] else _unrelated(index, dim)
        for index, q in enumerate(SEARCH_QUERIES)
    }
    gateway = _QueryGateway(vectors)
    plain = GraphReader(project.store, project.ontology)
    fused = GraphReader(project.store, project.ontology, gateway)
    out: dict[str, Any] = {"limit": RANKING_LIMIT, "queries": {}}
    original = reader_module.EXACT_VECTOR_MAX
    for q in SEARCH_QUERIES:
        text = q["text"]
        entry: dict[str, Any] = {"near": dict(q["near"]), "modes": ["fulltext", "fused_exact", "fragments"]}
        entry["fulltext"] = _hits(await plain.find_nodes(project_id, text, limit=RANKING_LIMIT))
        entry["fused_exact"] = _hits(await fused.find_nodes(project_id, text, limit=RANKING_LIMIT))
        if q["near"]:
            # The vector index is approximate: past the query's real neighbours it returns arbitrary
            # near-orthogonal nodes (and which ones differs from one index build to the next), so only
            # the hits on the nodes the query sits near are recorded, in their ranking order.
            reader_module.EXACT_VECTOR_MAX = 0
            try:
                found = await fused.find_nodes(project_id, text, limit=RANKING_LIMIT)
            finally:
                reader_module.EXACT_VECTOR_MAX = original
            entry["fused_index"] = _hits([h for h in found if h.node.spec_id in q["near"]])
            entry["modes"].append("fused_index")
        entry["fragments"] = [
            {"fragment_id": h.fragment.fragment_id, "score": round(float(h.score), 4)}
            for h in await plain.find_fragments(project_id, text, limit=RANKING_LIMIT)
        ]
        out["queries"][text] = entry
    return out


def _unrelated(seed: int, dim: int) -> list[float]:
    vector = np.random.default_rng(10_000 + seed).standard_normal(dim)
    return [float(x) for x in vector / np.linalg.norm(vector)]
