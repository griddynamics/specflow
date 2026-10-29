"""Host-reachable Neo4j testcontainer for the live Cypher checks."""

from __future__ import annotations

from typing import override

from testcontainers.community.neo4j import Neo4jContainer


class LoopbackNeo4jContainer(Neo4jContainer):
    """Keep host-run checks independent of ambient Docker host overrides.

    The runner executes on the host, where a Compose-only hostname such as ``neo4j`` that
    Testcontainers may derive does not resolve, though the Bolt port is published.
    """

    @override
    def get_connection_url(self) -> str:
        return f"bolt://127.0.0.1:{self.get_exposed_port(self.port)}"


__all__ = ["LoopbackNeo4jContainer"]
