"""The reads behind the agent routes: one ``GraphReader`` and ``PlanService``, built on first use.

They sit on the REST runtime's graph store and do not depend on the graph build switches (``Runtime.plans``): an
ontology without a ``feature`` type or bad plan rules fail the call that needs them, never the API start. The plan's
findings port is ``api.gap.findings.OpenGaps``: the open gaps of the runtime's gap runner (``Runtime.gaps``, read on
each call; docs/graph/gap/lifecycle.md "Plan link", FR-GPLN-02).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from api.gap.findings import OpenGaps
from api.runtime import Runtime
from graph import core as graph_core
from graph.core import ConfigurationError, GraphReader, Ontology, OntologyError
from graph.core.llm import Gateway
from graph.plan.rules import load_plan_rules
from graph.plan.service import PlanService

FEATURE_TYPE: Final = "feature"  # the node type list_features lists


@dataclass(frozen=True)
class AgentReads:
    ontology: Ontology
    reader: GraphReader
    plan: PlanService
    open_findings: OpenGaps | None = None  # the plan's findings port (open gaps); None: no finding is read


def _load(graph_store: object, gateway: Gateway | None = None, runtime: Runtime | None = None) -> AgentReads:
    try:
        ontology = graph_core.load_ontology()
        if FEATURE_TYPE not in ontology.node_types:
            raise OntologyError(["the ontology has no feature type"])
        rules = load_plan_rules(ontology=ontology)
    except OSError:  # an unreadable file is a configuration fault, not a database outage (is_database_unreachable)
        raise ConfigurationError("agent_reads_unreadable") from None
    open_findings = OpenGaps(lambda: runtime.gaps if runtime is not None else None)
    return AgentReads(
        ontology=ontology,
        reader=GraphReader(graph_store, ontology, gateway),
        plan=PlanService(graph_store, ontology, rules, findings=open_findings),
        open_findings=open_findings,
    )


async def agent_reads(runtime: Runtime) -> AgentReads:
    """The runtime's reads, loaded on the first call (a failure is not kept: the next call tries again)."""

    if runtime.agent_reads is None:
        build = runtime.settings.graph_build  # the search embeds its query with the build's gateway, when configured
        gateway = Gateway(build.to_llm_config()) if build is not None else None
        try:
            runtime.agent_reads = await asyncio.to_thread(_load, runtime.graph_store, gateway, runtime)
        except Exception:
            if gateway is not None:
                await gateway.aclose()
            raise
        if gateway is not None:
            runtime.cleanups.append(gateway.aclose)
    reads: AgentReads = runtime.agent_reads
    return reads


def findings_port(reads: AgentReads) -> Any:
    """What briefs and ``get_node`` read open findings from: the open gaps port, else none."""

    return reads.open_findings


async def finding_severities(reads: AgentReads, project_id: str, findings: Iterable[Any]) -> dict[str, str]:
    """The severity of each open gap among ``findings`` (FR-GWEB-03); none without the merged port."""

    shown = list(findings)
    if reads.open_findings is None or not shown:
        return {}
    return await reads.open_findings.severities(project_id, shown)
