"""Which graph store the live graph suites run against, and the probe for it.

One switch selects store and probe for every live graph suite::

    SPECFLOW_TEST_GRAPH_STORE=neo4j|arcadedb|postgres|tigergraph

``SPECFLOW_TEST_NEO4J=1`` stays an alias of ``neo4j`` (the switch wins when both are set). A store whose
probe does not exist yet skips the live suites with a reason that names it; a milestone adds its probe
module to ``PROBES``. The connection comes from the names the settings layer uses
(``GRAPH_STORE_URI``, ``GRAPH_STORE_USER``, ``GRAPH_STORE_PASSWORD``, ``GRAPH_STORE_NAME``); for Neo4j
the ``NEO4J_URI``, ``NEO4J_USER``, ``NEO4J_PASSWORD`` and ``NEO4J_DATABASE`` names are the fallback.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Final

import pytest
from pydantic import SecretStr

from graph.stores.config import BACKENDS, StoreConfig
from graph_probes.base import GraphProbe
from graph_probes.neo4j import Neo4jProbe
from graph_probes.postgres import PostgresProbe

SWITCH: Final = "SPECFLOW_TEST_GRAPH_STORE"
ALIAS: Final = "SPECFLOW_TEST_NEO4J"
DEFAULT_NEO4J_URI: Final = "bolt://localhost:7687"

# Backend -> its probe class. Absent means: no live probe yet (the suites skip). A milestone imports its
# probe module above and adds one row.
PROBES: Final[dict[str, Callable[[StoreConfig], GraphProbe]]] = {
    "neo4j": Neo4jProbe,
    "postgres": PostgresProbe,
}


def selected_backend() -> str | None:
    """The backend the live suites target; None when the live graph tier is not switched on.

    An unknown value of the switch raises ``ValueError`` (a typo must not skip silently).
    """

    value = os.environ.get(SWITCH, "").strip().lower()
    if value:
        if value not in BACKENDS:
            raise ValueError(f"{SWITCH}={value!r}: expected one of {', '.join(BACKENDS)}")
        return value
    return "neo4j" if os.environ.get(ALIAS) == "1" else None


def skip_reason() -> str | None:
    """Why the live graph suites do not run here, None when they do."""

    backend = selected_backend()
    if backend is None:
        return f"set {SWITCH}=neo4j (or {ALIAS}=1) to run the live graph tests"
    if backend not in PROBES:
        return f"no live probe for graph store {backend!r} yet ({SWITCH}={backend}); its milestone adds it"
    return None


def requires_live_graph(only: str | None = None) -> pytest.MarkDecorator:
    """Skip unless the switch selects a store that has a probe (and, with ``only``, that store); use as
    ``pytestmark``."""

    reason = skip_reason()
    if reason is None and only is not None and selected_backend() != only:
        reason = f"this live test is for the {only} store only (the switch selects {selected_backend()})"
    return pytest.mark.skipif(reason is not None, reason=reason or "")


def live_graph_enabled() -> bool:
    return skip_reason() is None


def store_config_from_env(backend: str | None = None) -> StoreConfig:
    """The ``StoreConfig`` of the selected store from ``GRAPH_STORE_*`` (``NEO4J_*`` as fallback for Neo4j)."""

    backend = backend or selected_backend()
    if backend is None:
        raise RuntimeError(skip_reason())
    legacy = backend == "neo4j"

    def pick(name: str, fallback: str, default: str | None = None) -> str | None:
        value = os.environ.get(f"GRAPH_STORE_{name}") or (os.environ.get(fallback) if legacy else None)
        return value or default

    uri = pick("URI", "NEO4J_URI", DEFAULT_NEO4J_URI if legacy else None)
    if uri is None:
        raise RuntimeError(f"set GRAPH_STORE_URI (and USER, PASSWORD, NAME) for the {backend} live tests")
    return StoreConfig.model_validate(
        {
            "backend": backend,
            "uri": uri,
            "user": pick("USER", "NEO4J_USER", "neo4j" if legacy else None) or "",
            "password": SecretStr(pick("PASSWORD", "NEO4J_PASSWORD", "") or ""),
            "name": pick("NAME", "NEO4J_DATABASE"),
        }
    )


def open_probe(config: StoreConfig | None = None) -> GraphProbe:
    """The probe of the selected store (or of ``config.backend``), connected lazily."""

    config = config or store_config_from_env()
    if config.backend not in PROBES:
        raise RuntimeError(f"no live probe for graph store {config.backend!r}")
    return PROBES[config.backend](config)
