"""The slim runtime of the MCP role: Postgres engine, graph store, ontology, one ``GraphReader`` and the plan reads
(``PlanReads``: the plan library's reader and the read methods of its task lifecycle; no claim or state change)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

from api.mcp.http import required
from db import session as db_session
from graph import core as graph_core
from graph.core import GraphReader, Ontology, OntologyError
from graph.plan import PlanReader, TaskLifecycle, load_plan_rules
from graph.stores import build_store
from health import Readiness, database_probe, graph_probe
from settings import RoleSettings

FEATURE_TYPE = "feature"  # the node type list_features lists


@dataclass(frozen=True)
class PlanReads:
    """The plan reads of the plan tools: ``reader`` (plan, areas, stories, tasks) and ``lifecycle``, whose
    ``ready_tasks`` and ``task_brief`` only are used here."""

    reader: PlanReader
    lifecycle: TaskLifecycle


@dataclass
class McpRuntime:
    """Everything a tool or probe needs; tests build it from fakes (no ORM or store types here)."""

    engine: Any
    session_factory: Any
    store: Any
    ontology: Ontology
    reader: GraphReader
    readiness: Readiness
    close: Callable[[], Awaitable[None]]
    plan: PlanReads | None = None

    async def aclose(self) -> None:
        await asyncio.shield(self.close())


RuntimeFactory = Callable[[RoleSettings], Awaitable[McpRuntime]]


def request_runtime(request: Request) -> McpRuntime:
    """The runtime built by the lifespan (``app.state.runtime``)."""

    runtime: McpRuntime = request.app.state.runtime
    return runtime


async def build_runtime(settings: RoleSettings) -> McpRuntime:
    """Open the engine and the graph store (both lazy), load the ontology and the plan rules; an ontology without
    a ``feature`` type, or plan rules that do not fit it, refuse the start. Closes what it opened when it fails."""

    graph_settings = required(settings.graph_store, "the mcp role requires graph store settings")
    async with AsyncExitStack() as stack:
        engine = db_session.build_engine(settings.database)
        stack.push_async_callback(engine.dispose)
        store = build_store(graph_settings.to_store_config())
        stack.push_async_callback(store.close)
        ontology = await asyncio.to_thread(graph_core.load_ontology)
        if FEATURE_TYPE not in ontology.node_types:
            raise OntologyError(["the ontology has no feature type"])
        rules = await asyncio.to_thread(load_plan_rules, ontology=ontology)
        plan = PlanReads(PlanReader(store, ontology, rules), TaskLifecycle(store, ontology, rules))
        return McpRuntime(
            engine=engine,
            session_factory=db_session.build_session_factory(engine),
            store=store,
            ontology=ontology,
            reader=GraphReader(store, ontology),
            readiness=Readiness({"database": database_probe(engine), "graph": graph_probe(store)}),
            plan=plan,
            close=stack.pop_all().aclose,  # last: everything above is open
        )
