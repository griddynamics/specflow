"""Gap runs on the shop gap corpus for offline tests: a ``GapContext`` over the fixture reader (with trace), the
in-memory graph executor holding the corpus's spec nodes and fragments, the relational fakes and a run store
(docs/graph/gap/architecture.md "Run steps").

``GapWorld`` keeps every store of one project, so consecutive runs (an update after a full run, a resumed run) share
them; ``world.context(...)`` builds the context of one run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from gap_acceptance_support import RecordedGapGateway, gap_corpus_graph
from gap_detection_support import PROJECT, TracingReader, build_facts
from gap_port_fakes import MemoryAmendments, MemoryGapRecords
from gap_support import MemoryGapExecutor
from graph.core import Ontology, load_ontology
from graph.gap import GapContext, MemoryGapRunStore, SeedInvestigator
from graph.gap.models import BuildFacts, GapChangeReport, InvestigationResult, SiteBudget, SitePack
from graph.gap.ports import Investigator, PlanView, SiteReads
from graph.gap.rules import GapRules, load_gap_rules
from graph.gap.settings import GapSettings


class CountingInvestigator:
    """Wraps an ``Investigator`` and records the site of every call."""

    def __init__(self, inner: Investigator | None = None) -> None:
        self.inner = inner or SeedInvestigator()
        self.sites: list[str] = []
        self.prompt_version = getattr(self.inner, "prompt_version", "counting")

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult:
        self.sites.append(pack.site_id)
        return await self.inner.investigate(pack, reads, budget)


async def executor_for(reader: Any, project_id: str = PROJECT) -> MemoryGapExecutor:
    """An in-memory graph executor holding the spec nodes and active fragments ``reader`` reads."""

    executor = MemoryGapExecutor(project_id)
    for node in await reader.nodes(project_id):
        documents = sorted({item.document_id for item in node.provenance if item.document_id})
        executor.spec(node.spec_id, node.label, name=node.name, documents=documents)
    for fragment in await reader.fragments(project_id):
        executor.fragment(fragment.fragment_id, fragment.document_id)
    return executor


@dataclass
class GapWorld:
    """The stores of one project's gap runs."""

    reader: Any
    executor: Any  # MemoryGapExecutor offline, a live store in live tests
    ontology: Ontology
    rules: GapRules
    records: MemoryGapRecords = field(default_factory=MemoryGapRecords)
    amendments: MemoryAmendments = field(default_factory=MemoryAmendments)
    store: MemoryGapRunStore = field(default_factory=MemoryGapRunStore)
    gateway: RecordedGapGateway = field(default_factory=RecordedGapGateway)
    investigator: CountingInvestigator = field(default_factory=CountingInvestigator)
    plan: PlanView | None = None
    project_id: str = PROJECT

    def context(
        self,
        *,
        run_id: str | None = None,
        changes: Sequence[GapChangeReport] = (),
        settings: GapSettings | None = None,
        build: BuildFacts | None = None,
        on_progress: Any = None,
    ) -> GapContext:
        return GapContext(
            project_id=self.project_id,
            executor=self.executor,
            reader=self.reader,
            gateway=self.gateway,
            ontology=self.ontology,
            rules=self.rules,
            records=self.records,
            amendments=self.amendments,
            investigator=self.investigator,
            settings=settings or GapSettings(),
            changes=tuple(changes),
            build=build if build is not None else build_facts(),
            plan=self.plan,
            store=self.store,
            run_id=run_id,
            on_progress=on_progress,
        )

    def gap_nodes(self) -> dict[str, dict[str, Any]]:
        return self.executor.gaps()

    def about(self, gap_id: str) -> set[str]:
        """The spec ids a gap is ``ABOUT`` (by edge key)."""

        return {key.split("|")[-1] for key in self.executor.edge_keys("ABOUT") if key.startswith(f"{gap_id}|ABOUT|")}


async def corpus_world(reader: Any = None, *, plan: PlanView | None = None, project_id: str = PROJECT) -> GapWorld:
    """A world over the shop corpus graph (or ``reader``)."""

    ontology = load_ontology()
    source = reader or TracingReader(gap_corpus_graph().reader, ontology)
    return GapWorld(
        reader=source,
        executor=await executor_for(source, project_id),
        ontology=ontology,
        rules=load_gap_rules(ontology=ontology),
        plan=plan,
        project_id=project_id,
    )
