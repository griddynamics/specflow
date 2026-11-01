"""Shared core of the graph library: the store port, ontology, identity, owned writes, reads, LLM gateway.

Public names are re-exported here as their modules land; submodules import from ``graph.core``.
"""

from __future__ import annotations

import importlib
from typing import Any

from graph.core.config import CALL_CLASSES, CallBudget, CallClass, LLMConfig, Neo4jConfig
from graph.core.errors import (
    ConfigurationError,
    ConformanceError,
    CorpusError,
    GraphError,
    GraphWriteError,
    InfrastructureUnavailableError,
    LLMError,
    LLMFatalError,
    OntologyError,
    OwnershipError,
)
from graph.core.files import FileSpecIdRegistry
from graph.core.identity import (
    NAMESPACE,
    SpecIdRegistry,
    SpecIdRow,
    allocate_spec_id,
    edge_uuid,
    identity_key,
    node_uuid,
    normalise_id,
    normalise_surface,
    slug,
    validate_project_id,
)
from graph.core.llm import (
    Evidence,
    Gateway,
    JudgeFailure,
    JudgeRunner,
    UsageSnapshot,
    Verdict,
    meets_floor,
    quotes_valid,
    untrusted,
)
from graph.core.ontology import Ontology, load_ontology
from graph.core.queries import Query
from graph.core.reader import (
    EvidenceQuote,
    FragmentHit,
    GraphDocument,
    GraphEdge,
    GraphFragment,
    GraphNode,
    GraphReader,
    ImpactResult,
    NodeHit,
    Provenance,
    Statement,
)
from graph.core.schema import ensure_schema
from graph.core.store import Executor, GraphStore, ScopedTransaction, StoreCapabilities, StoreInfo, check
from graph.core.writer import NodeRef, OwnedWriter

# The Neo4j access of earlier releases stays importable from here, but loads the Neo4j SDK, so it resolves on
# first access (PEP 562): importing ``graph.core`` does not import ``neo4j``.
_LAZY_SHIMS = frozenset({"connect", "require_version", "server_version"})


def __getattr__(name: str) -> Any:
    if name not in _LAZY_SHIMS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module("graph.core.db"), name)
    globals()[name] = value
    return value


__all__ = [
    "CALL_CLASSES",
    "NAMESPACE",
    "CallBudget",
    "CallClass",
    "ConfigurationError",
    "ConformanceError",
    "CorpusError",
    "Evidence",
    "EvidenceQuote",
    "Executor",
    "FileSpecIdRegistry",
    "FragmentHit",
    "Gateway",
    "GraphDocument",
    "GraphEdge",
    "GraphError",
    "GraphFragment",
    "GraphNode",
    "GraphReader",
    "GraphStore",
    "GraphWriteError",
    "ImpactResult",
    "InfrastructureUnavailableError",
    "JudgeFailure",
    "JudgeRunner",
    "LLMConfig",
    "LLMError",
    "LLMFatalError",
    "Neo4jConfig",
    "NodeHit",
    "NodeRef",
    "Ontology",
    "OntologyError",
    "OwnedWriter",
    "OwnershipError",
    "Provenance",
    "Query",
    "ScopedTransaction",
    "SpecIdRegistry",
    "SpecIdRow",
    "Statement",
    "StoreCapabilities",
    "StoreInfo",
    "UsageSnapshot",
    "Verdict",
    "allocate_spec_id",
    "check",
    "connect",
    "edge_uuid",
    "ensure_schema",
    "identity_key",
    "load_ontology",
    "meets_floor",
    "node_uuid",
    "normalise_id",
    "normalise_surface",
    "quotes_valid",
    "require_version",
    "server_version",
    "slug",
    "untrusted",
    "validate_project_id",
]
