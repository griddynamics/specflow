"""Neo4j version gate: the kernel version through ``dbms.components()`` and the supported minimum."""

from __future__ import annotations

import re
from typing import Any, Final

from neo4j.exceptions import DriverError, Neo4jError

from graph.core.errors import ConfigurationError
from graph.core.queries import SERVER_COMPONENTS
from graph.stores._bolt.cypher import render_cypher
from graph.stores._bolt.executor import _map_error

MINIMUM_VERSION: Final = (5, 26)
KERNEL_COMPONENT: Final = "Neo4j Kernel"
_VERSION: Final = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


async def server_version(driver: Any, database: str | None = None) -> tuple[int, int, int]:
    """The Neo4j kernel version as ``(major, minor, patch)``, read through ``dbms.components()``."""

    try:
        result = await driver.execute_query(render_cypher(SERVER_COMPONENTS), database_=database, routing_="r")
    except (Neo4jError, DriverError, OSError) as error:
        raise _map_error(error, SERVER_COMPONENTS.name, write=False) from None
    for record in result.records:
        if record["name"] == KERNEL_COMPONENT and record["versions"]:
            match = _VERSION.match(str(record["versions"][0]))
            if match:
                major, minor, patch = match.groups()
                return int(major), int(minor), int(patch or 0)
    raise ConfigurationError("neo4j_version_unknown")


async def require_version(
    driver: Any, minimum: tuple[int, ...] = MINIMUM_VERSION, database: str | None = None
) -> tuple[int, int, int]:
    """Return the server version; raise ``ConfigurationError`` when it is older than ``minimum``."""

    found = await server_version(driver, database)
    if found < tuple(minimum):
        raise ConfigurationError(
            "neo4j_version_unsupported",
            found=".".join(map(str, found)),
            minimum=".".join(map(str, minimum)),
        )
    return found
