"""The context of a gap run (docs/graph/gap/api.md "Models"): the stores, the gateway, the rules and the ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from graph.core import Ontology
from graph.gap.models import BuildFacts, GapChangeReport
from graph.gap.ports import Amendments, GapRecords, Investigator, PlanView, StoryHolds
from graph.gap.rules import GapRules
from graph.gap.settings import GapSettings

ProgressCallback = Callable[[str, int, int], None]
"""Called with a step name, the items of that step done and their total, while a long step runs: ``investigate``
reports its sites (the ones recorded before the first site it takes, then after each site is recorded)."""


@dataclass(frozen=True)
class GapContext:
    """Everything a gap run needs (api.md "Models"). ``executor``, ``reader`` and ``gateway`` are the core store
    executor, ``GraphReader`` and LLM gateway; ``store`` is the run artifact store used to resume a run; ``build`` holds
    the dedup refusals and cycle cuts of the build the run follows; ``holds`` (the plan's hold and release calls) holds
    the tasks of open blocker gaps and releases the tasks of the gaps a run ends (none without a plan)."""

    project_id: str
    executor: Any
    reader: Any
    gateway: Any
    ontology: Ontology
    rules: GapRules
    records: GapRecords
    amendments: Amendments
    investigator: Investigator
    settings: GapSettings = field(default_factory=GapSettings)
    changes: tuple[GapChangeReport, ...] = ()
    build: BuildFacts = field(default_factory=BuildFacts)
    plan: PlanView | None = None
    holds: StoryHolds | None = None
    store: Any = None
    run_id: str | None = None
    on_progress: ProgressCallback | None = None
