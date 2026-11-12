"""The reads behind the agent routes: one ``GraphReader``, ``PlanService`` and findings store, built on first use.

They sit on the REST runtime's graph store and do not depend on the graph build switches (``Runtime.plans``): an
ontology without a ``feature`` type or bad plan rules fail the call that needs them, never the API start.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final

from api.runtime import Runtime
from graph import core as graph_core
from graph.core import ConfigurationError, GraphReader, Ontology, OntologyError
from graph.core.llm import Gateway
from graph.gap import Findings
from graph.plan.rules import load_plan_rules
from graph.plan.service import PlanService

FEATURE_TYPE: Final = "feature"  # the node type list_features lists


@dataclass(frozen=True)
class AgentReads:
    ontology: Ontology
    reader: GraphReader
    plan: PlanService
    findings: Findings | None = None  # None: no findings are read, lists stay empty


def _load(graph_store: object, gateway: Gateway | None = None) -> AgentReads:
    try:
        ontology = graph_core.load_ontology()
        if FEATURE_TYPE not in ontology.node_types:
            raise OntologyError(["the ontology has no feature type"])
        rules = load_plan_rules(ontology=ontology)
    except OSError:  # an unreadable file is a configuration fault, not a database outage (is_database_unreachable)
        raise ConfigurationError("agent_reads_unreadable") from None
    findings = Findings(graph_store, ontology)
    return AgentReads(
        ontology=ontology,
        reader=GraphReader(graph_store, ontology, gateway),
        plan=PlanService(graph_store, ontology, rules, findings=findings),
        findings=findings,
    )


async def agent_reads(runtime: Runtime) -> AgentReads:
    """The runtime's reads, loaded on the first call (a failure is not kept: the next call tries again)."""

    if runtime.agent_reads is None:
        build = runtime.settings.graph_build  # the search embeds its query with the build's gateway, when configured
        gateway = Gateway(build.to_llm_config()) if build is not None else None
        try:
            runtime.agent_reads = await asyncio.to_thread(_load, runtime.graph_store, gateway)
        except Exception:
            if gateway is not None:
                await gateway.aclose()
            raise
        if gateway is not None:
            runtime.cleanups.append(gateway.aclose)
    reads: AgentReads = runtime.agent_reads
    return reads
