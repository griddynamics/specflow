"""A throwaway Neo4j testcontainer for the live checks: its image, its start, a seeded driver."""

from __future__ import annotations

import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import docker.errors
import neo4j
import neo4j.exceptions
from testcontainers.core.config import testcontainers_config

from cypher_checks import fixture_graph
from cypher_checks.neo4j_container import LoopbackNeo4jContainer
from cypher_checks.schema import GraphSchema

__all__ = [
    "DEFAULT_IMAGE",
    "IMAGE_ENV",
    "LIVE_ERRORS",
    "ImageError",
    "neo4j_image",
    "seeded_driver",
    "start_container",
]

#: Pin the server your deployment ships; override per run with `IMAGE_ENV` or `--image`.
DEFAULT_IMAGE = "neo4j:2026.08.1-community"
IMAGE_ENV = "CYPHER_CHECKS_NEO4J_IMAGE"
# A throwaway, single-purpose testcontainer credential -- never a real secret.
_CONTAINER_CREDENTIAL = "cypher-checks-0000"
# Neo4j start-up takes about 15 s alone and far longer on a loaded Docker host.
START_TIMEOUT_S = 300


class ImageError(ValueError):
    """The configured image is not a `neo4j:<tag>` image."""


#: Failures that stop the live checks from running: a tool error (exit 2), never a finding.
LIVE_ERRORS: tuple[type[BaseException], ...] = (
    ImageError,
    docker.errors.DockerException,
    neo4j.exceptions.Neo4jError,
    neo4j.exceptions.DriverError,
    OSError,
    TimeoutError,
)


def neo4j_image(explicit: str | None = None) -> str:
    """The image to start: `explicit`, else `IMAGE_ENV`, else `DEFAULT_IMAGE`.

    Anything but a `neo4j:<tag>` image is refused rather than started."""
    image = explicit or os.environ.get(IMAGE_ENV) or DEFAULT_IMAGE
    if not image.startswith("neo4j:"):
        message = f"Neo4j image is {image!r}, expected a 'neo4j:<tag>' image"
        raise ImageError(message)
    return image


def start_container(image: str) -> LoopbackNeo4jContainer:
    """Start one Neo4j container; the caller stops it.

    The caller stops the container itself, so no reaper container is started."""
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    testcontainers_config.max_tries = math.ceil(START_TIMEOUT_S / testcontainers_config.sleep_time)
    container = LoopbackNeo4jContainer(image, password=_CONTAINER_CREDENTIAL).with_name(
        f"cypher-checks-{uuid4().hex}-neo4j"
    )
    container.start()
    return container


def connect(container: LoopbackNeo4jContainer) -> neo4j.Driver:
    """A *sync* driver: CyVer's validators type-check against `neo4j.Driver`."""
    driver = neo4j.GraphDatabase.driver(
        container.get_connection_url(), auth=(container.username, container.password)
    )
    driver.verify_connectivity()
    return driver


@contextmanager
def seeded_driver(schema: GraphSchema, image: str | None = None) -> Iterator[neo4j.Driver]:
    """A driver on a fresh container seeded with the schema's fixture graph; both are closed
    and stopped on exit."""
    container = start_container(neo4j_image(image))
    try:
        driver = connect(container)
        try:
            with driver.session() as session:
                fixture_graph.seed(session, schema)
            yield driver
        finally:
            driver.close()
    finally:
        container.stop()
