"""Shared core of the graph library: Neo4j access, ontology, identity, owned writes, reads, LLM gateway.

Public names are re-exported here as their modules land; submodules import from ``graph.core``.
"""

from __future__ import annotations

from graph.core.config import CALL_CLASSES, CallBudget, CallClass, LLMConfig, Neo4jConfig
from graph.core.db import Executor, ScopedTransaction, check, connect, require_version, server_version
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
    NodePage,
    Provenance,
    Statement,
)
from graph.core.schema import ensure_schema
from graph.core.writer import NodeRef, OwnedWriter

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
    "NodePage",
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
