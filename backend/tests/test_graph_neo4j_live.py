"""Live checks that only make sense on Neo4j (SPECFLOW_TEST_GRAPH_STORE=neo4j or SPECFLOW_TEST_NEO4J=1)."""

from __future__ import annotations

from graph.stores.neo4j import require_version, server_version
from graph_probes import requires_live_graph
from graph_support import GraphProject

# The graph fixtures (graph_project) live in graph_support.
pytest_plugins = ["graph_support"]

pytestmark = requires_live_graph("neo4j")


async def test_nfr_port_01_server_is_neo4j_community_5_26_or_later(graph_project: GraphProject) -> None:
    project = graph_project
    version = await server_version(project.probe.driver, project.database)
    assert version >= (5, 26)
    assert await require_version(project.probe.driver, database=project.database) == version
    assert await project.probe.server_edition() == "community"
