"""The slim runtime of the MCP role: Postgres engine, Neo4j driver, ontology, one ``GraphReader``."""

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
from health import Readiness, database_probe, neo4j_probe
from settings import RoleSettings

FEATURE_TYPE = "feature"  # the one node type version 1 tools list


@dataclass
class McpRuntime:
    """Everything a tool or probe needs; tests build it from fakes (no ORM or driver types here)."""

    engine: Any
    session_factory: Any
    driver: Any
    ontology: Ontology
    reader: GraphReader
    readiness: Readiness
    close: Callable[[], Awaitable[None]]

    async def aclose(self) -> None:
        await asyncio.shield(self.close())


RuntimeFactory = Callable[[RoleSettings], Awaitable[McpRuntime]]


def request_runtime(request: Request) -> McpRuntime:
    """The runtime built by the lifespan (``app.state.runtime``)."""

    runtime: McpRuntime = request.app.state.runtime
    return runtime


async def build_runtime(settings: RoleSettings) -> McpRuntime:
    """Open the engine and driver (both lazy) and load the ontology; an ontology without
    a ``feature`` type refuses the start. Closes what it opened when it fails."""

    neo4j = required(settings.neo4j, "the mcp role requires neo4j settings")
    async with AsyncExitStack() as stack:
        engine = db_session.build_engine(settings.database)
        stack.push_async_callback(engine.dispose)
        driver = graph_core.connect(neo4j.to_graph_config())
        stack.push_async_callback(driver.close)
        ontology = await asyncio.to_thread(graph_core.load_ontology)
        if FEATURE_TYPE not in ontology.node_types:
            raise OntologyError(["the ontology has no feature type"])
        return McpRuntime(
            engine=engine,
            session_factory=db_session.build_session_factory(engine),
            driver=driver,
            ontology=ontology,
            reader=GraphReader(driver, ontology, database=neo4j.database),
            readiness=Readiness({"database": database_probe(engine), "neo4j": neo4j_probe(driver, neo4j.database)}),
            close=stack.pop_all().aclose,  # last: everything above is open
        )
