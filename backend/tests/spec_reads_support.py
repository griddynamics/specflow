"""The rig of the agent reads of the spec graph (``get_story_brief``, ``trace``, ``get_node``, ``search_spec``,
``similar``, ``get_evidence``, ``path``, ``expand_brief``): the API app over fakes, with the shop fixture
(``fixtures/graph/plan/shop.yaml``) as the spec graph, a small plan whose tasks implement its nodes, the findings
library over an in-memory store, and the project's members by name.

The rig adds to the fixture what only these reads look at: stored vectors of a few nodes (``VECTORS``, one of another
embedding model), statements (``STMTS``) and a second node on the quality line (``CHECKOUT_SPEED_LINE``). Its reader's
gateway names the embedding model and fails every embed call, so search stays text-only and similar/expand compare
the stored vectors.

Boundaries faked, as in ``specflow_api_agent_support``: Postgres (``FakeRepo``) and the graph store: ``ShopExecutor``
answers the reader's queries over the fixture, so the REAL ``GraphReader`` (``hops``, ``find_nodes``, ``node``,
``neighbours``) and the REAL ``PlanService`` and ``Findings`` run. Every query is also rendered by the Cypher and the
PostgreSQL dialects, so a template either store lacks fails the test.
"""

from __future__ import annotations

import copy
import dataclasses
import itertools
import math
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
import yaml

from api.agent_reads import AgentReads
from gap_support import Clock, MemoryGapExecutor
from graph.core import GraphReader, LLMError
from graph.core.queries import Query
from graph.gap import Findings
from graph.plan import PlanService
from graph.plan.models import CurrentTask
from graph.plan.reader import LoadedPlan
from graph.stores._bolt.cypher import binds_project_id, render_cypher
from graph.stores.postgres.dialect import PostgresDialect
from graph_plan_support import plan_ontology, plan_rules
from graph_support import OfflineExecutor
from graph_trace_support import TraceExecutor, TraceGraph, shop_graph
from plan_trace_support import TASKS, ShopSpecGraph, shop_plan
from specflow_api_agent_support import ALPHA, AgentRig, serve_agent

SHOP: Final = Path(__file__).resolve().parent / "fixtures" / "graph" / "plan" / "shop.yaml"
CHECKOUT: Final = "STORY::WEB_SHOP::CHECKOUT"
STRIPE_STORY: Final = "STORY::STRIPE::CORE"
BEN_ID: Final = uuid.UUID("00000000-0000-4000-8000-0000000000be")
LEFT_ID: Final = uuid.UUID("00000000-0000-4000-8000-0000000000a0")  # held a task, no member of the project any more
SPEC_NODE: Final = "SpecNode"
_POSTGRES: Final = PostgresDialect("graph")
EMBEDDING_MODEL: Final = "test/embedding"
OTHER_MODEL: Final = "other/embedding"
# Stored search vectors (spec_id -> model, vector): the Checkout story's FEAT::CHECKOUT and REQ::FR::CHECKOUT have one,
# REQ::Q::CHECKOUT_SPEED has none. FEAT::CARD_PAYMENT reads alike but has an edge to FEAT::CHECKOUT.
VECTORS: Final[dict[str, tuple[str, tuple[float, ...]]]] = {
    "FEAT::CHECKOUT": (EMBEDDING_MODEL, (1.0, 0.0, 0.0)),
    "REQ::FR::CHECKOUT": (EMBEDDING_MODEL, (0.9, 0.1, 0.0)),
    "FEAT::CHECKOUT_REVIEW": (EMBEDDING_MODEL, (0.95, 0.3, 0.0)),
    "FEAT::CARD_PAYMENT": (EMBEDDING_MODEL, (0.8, 0.6, 0.0)),
    "REQ::FR::CARD_PAYMENT": (EMBEDDING_MODEL, (0.6, 0.8, 0.0)),
    "MOD::WEB_SHOP": (EMBEDDING_MODEL, (0.0, 0.0, 1.0)),
    "ENT::ORDER": (EMBEDDING_MODEL, (-1.0, 0.0, 0.0)),
    "FEAT::GIFT_CARDS": (OTHER_MODEL, (1.0, 0.0, 0.0)),
}
STMTS: Final = {
    "FEAT::CARD_PAYMENT": "Customers pay by card when they check out.",
    "FEAT::CHECKOUT_REVIEW": " ".join(["Customers review the order before they pay."] * 45),  # ~500 tokens
    "MOD::PAYMENTS_SERVICE": "Takes card payments through Stripe.",
}
CHECKOUT_SPEED_LINE: Final = {"document_id": "shop", "path": "docs/shop.md", "fragment_id": "shop.quality"}


class VectorGateway:
    """A gateway that names the embedding model of the stored vectors; embedding a query fails (text-only search)."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(embedding_model=EMBEDDING_MODEL)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        del texts
        raise LLMError("exhausted")


def vector_score(first: Sequence[float], second: Sequence[float]) -> float:
    """``(1 + cos) / 2``, as both stores score."""

    norm = math.sqrt(sum(x * x for x in first)) * math.sqrt(sum(y * y for y in second))
    return (1 + sum(x * y for x, y in zip(first, second, strict=True)) / norm) / 2


def load_shop() -> dict[str, Any]:
    return yaml.safe_load(SHOP.read_text(encoding="utf-8"))


class ShopExecutor:
    """An ``Executor`` answering the reader's queries over the shop fixture; ``fail`` makes every read raise."""

    def __init__(self, fail: Exception | None = None) -> None:
        data = load_shop()
        self.nodes: dict[str, dict[str, Any]] = {node["spec_id"]: node for node in data["nodes"]}
        for spec_id, stmt in STMTS.items():
            self.nodes[spec_id]["attributes"] = {**(self.nodes[spec_id].get("attributes") or {}), "stmt": stmt}
        self.nodes["REQ::Q::CHECKOUT_SPEED"]["provenance"] = [{**CHECKOUT_SPEED_LINE, "line_start": 7, "line_end": 7}]
        self.vectors = dict(VECTORS)
        self.fragments: list[dict[str, Any]] = data["fragments"]
        self.edges: list[dict[str, Any]] = data["edges"]
        self.trace_graph: TraceGraph = shop_graph()
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def read(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        assert binds_project_id(render_cypher(query))
        _POSTGRES.plan(query, {**params, "project_id": project_id})
        if self.fail is not None:
            raise self.fail
        self.calls.append((query.name, params))
        handler = getattr(self, f"_{query.name}", None)
        assert handler is not None, f"unscripted query {query.name}"
        return copy.deepcopy(handler(**params))

    async def write(self, query: Query, project_id: str, /, **params: Any) -> list[dict[str, Any]]:
        del project_id, params
        raise AssertionError(f"a read never writes ({query.name})")

    def tx(self, project_id: str) -> Any:
        del project_id
        raise AssertionError("a read never opens a transaction")

    # rows

    def _row(self, node: dict[str, Any]) -> dict[str, Any]:
        props = {
            "spec_id": node["spec_id"],
            "uuid": node["uuid"],
            "name": node["name"],
            "last_build_id": node.get("last_build_id"),
            "spec_id_aliases": node.get("aliases", []),
            "statements": node.get("statements", []),
            **(node.get("attributes") or {}),
        }
        provenance = [{k: v for k, v in item.items() if k != "quote"} for item in node.get("provenance", [])]
        return {"labels": [SPEC_NODE, node["label"]], "props": props, "provenance": provenance}

    def _by_spec_id(self, spec_id: str) -> list[dict[str, Any]]:
        return [self._row(self.nodes[spec_id])] if spec_id in self.nodes else []

    def _read_node_by_spec_id(self, spec_id: str, **_: Any) -> list[dict[str, Any]]:
        return self._by_spec_id(spec_id)

    def _read_node_by_alias(self, spec_id: str, **_: Any) -> list[dict[str, Any]]:
        return [self._row(node) for node in self.nodes.values() if spec_id in node.get("aliases", [])]

    def _read_nodes_by_uuid(self, uuids: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        found = [node for node in self.nodes.values() if node["uuid"] in uuids]
        return [self._row(node) for node in sorted(found, key=lambda item: item["spec_id"])]

    def _read_nodes(self, labels: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        found = [node for node in self.nodes.values() if node["label"] in labels]
        return [self._row(node) for node in sorted(found, key=lambda item: item["spec_id"])]

    def _read_owner_values(self, **_: Any) -> list[dict[str, Any]]:
        return []

    def _read_edges(self, relations: Sequence[str], *, include_inferred: bool, **_: Any) -> list[dict[str, Any]]:
        rows = [self._edge_row(edge) for edge in self.edges if edge["relation"] in relations]
        return [row for row in rows if include_inferred or row["props"].get("basis") != "inferred"]

    def _read_neighbours(self, spec_id: str, relations: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        return [
            self._edge_row(edge)
            for edge in self.edges
            if edge["relation"] in relations and spec_id in (edge["source"], edge["target"])
        ]

    @staticmethod
    def _edge_row(edge: dict[str, Any]) -> dict[str, Any]:
        props = {key: edge[key] for key in ("basis", "edge_key") if key in edge}
        return {"relation": edge["relation"], "source": edge["source"], "target": edge["target"], "props": props}

    def _read_nodes_by_ids(self, spec_ids: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        return [
            {"uuid": node["uuid"], "spec_id": node["spec_id"], "keys": [], "spec_ids": [node["spec_id"]]}
            for node in self.nodes.values()
            if node["spec_id"] in spec_ids
        ]

    def _search_nodes_fulltext(
        self, query: str, skip: int, page: int, labels: Sequence[str], **_: Any
    ) -> list[dict[str, Any]]:
        terms = [term.lower() for term in query.replace('"', " ").split() if term.upper() not in {"OR", "AND"}]
        scored = []
        for node in (n for n in self.nodes.values() if n["label"] in labels):
            words = node["name"].lower().split()
            score = sum(1 for term in terms if term in words)
            if score:
                scored.append({"uuid": node["uuid"], "spec_id": node["spec_id"], "score": float(score)})
        scored.sort(key=lambda hit: (-hit["score"], hit["spec_id"]))
        window = scored[skip : skip + page]
        return [{"fetched": len(window), "hits": window}]

    # the stored vectors of similar and expand, the document lines of get_evidence

    def _vectors_of(self, model: str, labels: Sequence[str]) -> list[tuple[dict[str, Any], tuple[float, ...]]]:
        return [
            (self.nodes[spec_id], vector)
            for spec_id, (made_by, vector) in sorted(self.vectors.items())
            if made_by == model and self.nodes[spec_id]["label"] in labels
        ]

    def _count_search_vectors(self, model: str, labels: Sequence[str], **_: Any) -> list[dict[str, Any]]:
        stored = [spec_id for spec_id in self.vectors if self.nodes[spec_id]["label"] in labels]
        return [{"matching": len(self._vectors_of(model, labels)), "stored": len(stored)}]

    def _search_nodes_vector_exact(
        self, vector: Sequence[float], model: str, limit: int, labels: Sequence[str], **_: Any
    ) -> list[dict[str, Any]]:
        scored = [
            {"uuid": node["uuid"], "spec_id": node["spec_id"], "score": vector_score(vector, stored)}
            for node, stored in self._vectors_of(model, labels)
        ]
        scored.sort(key=lambda hit: (-hit["score"], hit["spec_id"]))
        return scored[:limit]

    def _read_node_vectors(self, uuids: Sequence[str], model: str) -> list[dict[str, Any]]:
        return [
            {"uuid": node["uuid"], "spec_id": node["spec_id"], "embedding": list(vector)}
            for node, vector in self._vectors_of(model, [node["label"] for node in self.nodes.values()])
            if node["uuid"] in uuids
        ]

    def _read_fragments(self, document_id: str | None) -> list[dict[str, Any]]:
        return [{"props": item} for item in self.fragments if document_id in (None, item["document_id"])]

    def _read_nodes_for_lines(
        self, document_id: str, line_start: int, line_end: int, labels: Sequence[str], **_: Any
    ) -> list[dict[str, Any]]:
        found = [
            node
            for node in self.nodes.values()
            if node["label"] in labels
            and any(
                item.get("document_id") == document_id
                and item["line_start"] <= line_end
                and item["line_end"] >= line_start
                for item in node.get("provenance", [])
            )
        ]
        return [self._row(node) for node in sorted(found, key=lambda item: item["spec_id"])]

    # the trace queries are answered by the trace fixture's executor over the same graph

    def _read_node_uuids_by_spec_id(self, spec_ids: Sequence[str], labels: Sequence[str]) -> list[dict[str, Any]]:
        return [
            {"uuid": node["uuid"], "spec_id": spec_id}
            for spec_id, node in sorted(self.trace_graph.nodes.items())
            if node["label"] in labels and (spec_id in spec_ids or set(node["aliases"]) & set(spec_ids))
        ]

    def _read_trace_hop(self, **params: Any) -> list[dict[str, Any]]:
        return TraceExecutor(self.trace_graph)._read_trace_hop(**params)


def reader_of(executor: ShopExecutor) -> GraphReader:
    return GraphReader(executor, plan_ontology(), VectorGateway())


def spec_plan(holder: uuid.UUID, other: uuid.UUID) -> LoadedPlan:
    """The trace fixture's plan, its in-progress Stripe tasks held by ``holder`` and ``other``."""

    held = {"TASK::STRIPE::SETUP": holder, "TASK::STRIPE::WIRE": other}
    tasks: tuple[CurrentTask, ...] = tuple(
        task.model_copy(update={"claimed_by": str(held[task.task_id])}) if task.task_id in held else task
        for task in TASKS
    )
    return shop_plan(tasks=tasks)


@dataclass
class SpecRig:
    rig: AgentRig
    executor: ShopExecutor
    memory: MemoryGapExecutor
    findings: Findings
    alice: uuid.UUID
    ben: uuid.UUID

    async def get(self, path: str, **params: Any) -> Any:
        return await self.rig.get(path, **params)


async def add_findings(findings: Findings, alice: uuid.UUID, ben: uuid.UUID) -> None:
    """One open finding by alice on the Checkout story's node, a blocking one by ben, one by a member who left."""

    await findings.add(
        ALPHA,
        created_by=str(alice),
        kind="question",
        text="Which currencies does checkout accept?",
        about=["FEAT::CHECKOUT"],
    )
    await findings.add(
        ALPHA,
        created_by=str(ben),
        kind="conflict",
        text="The spec says card payment is optional and required.",
        about=["FEAT::CHECKOUT", "FEAT::CARD_PAYMENT", "MOD::STRIPE"],
        story_id=CHECKOUT,
        blocking=True,
    )
    await findings.add(
        ALPHA, created_by=str(LEFT_ID), kind="note", text="Stripe retries are limited.", about=["MOD::STRIPE"]
    )


@asynccontextmanager
async def serve_spec(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    scenario: str = "ok",
    has_plan: bool = True,
    fail: Exception | None = None,
) -> AsyncIterator[SpecRig]:
    """The API over the shop graph. ``scenario`` is ``serve_agent``'s; ``fail`` makes the graph reads raise."""

    async with serve_agent(monkeypatch, env, scenario=scenario, has_plan=has_plan) as rig:
        repo = rig.repo
        alice = rig.users["alice"].id
        repo.users["user:alice@example.com"].display_name = "Alice"
        ben = await repo.upsert_user(
            None, SimpleNamespace(subject="user:ben@example.com", email="ben@x.test", name="Ben")
        )
        repo.members.add((uuid.UUID(ALPHA), ben.id))
        executor = ShopExecutor(fail)
        ontology = plan_ontology()
        memory = MemoryGapExecutor(ALPHA)
        for spec_id, node in executor.nodes.items():
            memory.spec(spec_id, node["label"], node.get("aliases", []))
        counter = itertools.count(1)  # the ids of the seeded findings are the same in every run
        monkeypatch.setattr("graph.gap.findings.new_finding_id", lambda: f"FND::{next(counter):012d}")
        findings = Findings(memory, ontology, clock=Clock())
        await add_findings(findings, alice, ben.id)
        memory.calls.clear()  # what the routes read is counted from here
        loaded = (
            spec_plan(alice, ben.id) if has_plan else LoadedPlan(shop_plan().current.model_copy(update={"plan": None}))
        )

        async def load(_project_id: str) -> LoadedPlan:
            return loaded

        plan = PlanService(
            OfflineExecutor(),
            ontology,
            plan_rules(ontology),
            graph=ShopSpecGraph(),
            load=load,
            findings=findings,
        )
        rig.runtime.agent_reads = AgentReads(
            ontology=ontology, reader=reader_of(executor), plan=plan, findings=findings
        )
        yield SpecRig(rig, executor, memory, findings, alice, ben.id)


class Raising:
    """Stands in for the reader or the plan library: every method raises ``error`` (an outage or a bug below the
    route), whatever route calls it first."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def __getattr__(self, name: str) -> Any:
        async def raise_it(*_: Any, **__: Any) -> Any:
            raise self._error

        return raise_it


class Recording:
    """Delegates to ``inner`` and keeps the first argument (the project id) of every call in ``seen``."""

    def __init__(self, inner: Any, seen: list[Any]) -> None:
        self._inner = inner
        self._seen = seen

    def __getattr__(self, name: str) -> Any:
        found = getattr(self._inner, name)
        if not callable(found):
            return found

        def call(*args: Any, **kwargs: Any) -> Any:
            self._seen.append(args[0] if args else None)
            return found(*args, **kwargs)

        return call


def failing_reads(spec: SpecRig, error: Exception) -> None:
    """Every library call of the routes raises ``error`` from here on."""

    reads = spec.rig.runtime.agent_reads
    assert reads is not None
    spec.rig.runtime.agent_reads = dataclasses.replace(reads, reader=Raising(error), plan=Raising(error))


def recording_reads(spec: SpecRig) -> list[Any]:
    """The project id of every reader, plan and findings call of the routes from here on."""

    reads = spec.rig.runtime.agent_reads
    assert reads is not None
    seen: list[Any] = []
    spec.rig.runtime.agent_reads = dataclasses.replace(
        reads,
        reader=Recording(reads.reader, seen),
        plan=Recording(reads.plan, seen),
        findings=Recording(reads.findings, seen),
    )
    return seen


def with_plan(spec: SpecRig, tasks: tuple[CurrentTask, ...], graph: TraceGraph | None = None) -> None:
    """The routes read a plan of ``tasks`` over ``graph`` (the shop graph plus nodes: also given to the reader)."""

    if graph is not None:
        spec.executor.trace_graph = graph
        for spec_id, node in graph.nodes.items():
            spec.executor.nodes.setdefault(
                spec_id, {"spec_id": spec_id, "uuid": node["uuid"], "label": node["label"], "name": node["name"]}
            )
        spec.executor.edges = [
            {"relation": e["relation"], "source": e["source"], "target": e["target"], **e["props"]} for e in graph.edges
        ]
    loaded = shop_plan(tasks=tasks)

    async def load(_project_id: str) -> LoadedPlan:
        return loaded

    reads = spec.rig.runtime.agent_reads
    assert reads is not None
    ontology = plan_ontology()
    plans = PlanService(
        OfflineExecutor(),
        ontology,
        plan_rules(ontology),
        graph=ShopSpecGraph(graph),
        load=load,
        findings=spec.findings,
    )
    spec.rig.runtime.agent_reads = dataclasses.replace(reads, plan=plans)
