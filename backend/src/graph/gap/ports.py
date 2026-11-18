"""Ports of ``graph.gap`` (docs/graph/gap/api.md "Ports", architecture.md "Ports and who fills them").

The host fills them: ``GapRecords`` and ``Amendments`` with its relational store (PostgreSQL), ``StoryHolds`` with the
plan service, ``BuildRequests`` with the build runner's queue and ``PlanView`` with the plan reader. ``Investigator``
is filled by graph.gap's structured loop or by a host adapter; ``SiteReads`` is the read menu graph.gap binds to a
project and a run. Options, amendments, runs, seeds, sites, verdicts, section relevance, noise rules, suppressions,
notes, events and assignments are stored only through ``GapRecords`` and ``Amendments``, never as graph nodes
(FR-GOWN-04). Every call takes the project id first, and every implementation filters on it (FR-GOWN-02).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from graph.gap.models import (
    AmendmentRow,
    AssignmentRow,
    EventRow,
    HoldResult,
    InvestigationResult,
    NoiseRuleRow,
    NoteRow,
    OptionRow,
    ReadResult,
    RelevanceRow,
    RunRow,
    SectionKey,
    SeedRow,
    SiteBudget,
    SitePack,
    SiteRow,
    SuppressionRow,
    TaskRef,
    VerdictRow,
)
from graph.gap.names import FinishedRunStatus


@runtime_checkable
class GapRecords(Protocol):
    """The relational rows of graph.gap (host: PostgreSQL)."""

    async def options(self, project_id: str, gap_id: str) -> Sequence[OptionRow]: ...

    async def put_options(self, project_id: str, gap_id: str, rows: Sequence[OptionRow]) -> None: ...

    async def start_run(self, project_id: str, run: RunRow) -> None: ...

    async def finish_run(
        self,
        project_id: str,
        run_id: str,
        counts: Mapping[str, Any],
        cost: Mapping[str, Any],
        *,
        status: FinishedRunStatus = "completed",
    ) -> None: ...

    async def put_seeds(self, project_id: str, run_id: str, seeds: Sequence[SeedRow]) -> None: ...

    async def put_sites(self, project_id: str, run_id: str, sites: Sequence[SiteRow]) -> None: ...

    async def verdict(self, project_id: str, input_hash: str) -> VerdictRow | None: ...

    async def put_verdict(self, project_id: str, row: VerdictRow) -> None: ...

    async def relevance(self, project_id: str, keys: Sequence[SectionKey]) -> Mapping[SectionKey, RelevanceRow]: ...

    async def put_relevance(self, project_id: str, rows: Sequence[RelevanceRow]) -> None: ...

    async def noise_rules(self, project_id: str) -> Sequence[NoiseRuleRow]: ...

    async def put_noise_rule(self, project_id: str, row: NoiseRuleRow) -> None: ...

    async def delete_noise_rule(self, project_id: str, rule_id: str) -> bool: ...

    async def suppressed(self, project_id: str, signatures: Sequence[str]) -> frozenset[str]: ...

    async def suppress(self, project_id: str, row: SuppressionRow) -> None: ...

    async def unsuppress(self, project_id: str, signature: str) -> None: ...

    async def put_notes(self, project_id: str, run_id: str | None, notes: Sequence[NoteRow]) -> None: ...

    async def log(self, project_id: str, events: Sequence[EventRow]) -> None: ...

    async def events(self, project_id: str, gap_id: str) -> Sequence[EventRow]: ...

    async def assignments(self, project_id: str) -> Mapping[str, AssignmentRow]: ...

    async def assign(self, project_id: str, row: AssignmentRow) -> None: ...

    async def unassign(self, project_id: str, gap_id: str) -> None: ...


@runtime_checkable
class Amendments(Protocol):
    """Stored edits applied by the corpus reader (host: PostgreSQL, beside the documents)."""

    async def for_lineage(
        self, project_id: str, lineage: str, *, statuses: Sequence[str] = ("active",)
    ) -> Sequence[AmendmentRow]: ...

    async def for_clarification(self, project_id: str, clr_id: str) -> Sequence[AmendmentRow]: ...

    async def put(self, project_id: str, rows: Sequence[AmendmentRow]) -> None: ...

    async def set_status(
        self,
        project_id: str,
        amendment_ids: Sequence[str],
        status: str,
        *,
        revision_id: str | None = None,
    ) -> None: ...

    async def delete(self, project_id: str, amendment_ids: Sequence[str]) -> None: ...

    async def pending_lineages(self, project_id: str) -> Sequence[str]: ...

    async def with_status(self, project_id: str, statuses: Sequence[str]) -> Sequence[AmendmentRow]: ...


@runtime_checkable
class SiteReads(Protocol):
    """The read menu of one investigation (investigation.md "Read menu"), bound to the project and the run: no read
    takes a project id. A read naming an id that does not resolve returns an empty result with the reason; reads
    never write."""

    async def node(self, spec_id: str) -> ReadResult: ...

    async def evidence(self, spec_id: str) -> ReadResult: ...

    async def neighbours(self, spec_id: str, relations: Sequence[str] = ()) -> ReadResult: ...

    async def trace(self, spec_id: str, direction: Literal["based_on", "affects"], depth: int) -> ReadResult: ...

    async def paths(self, source: str, target: str) -> ReadResult: ...

    async def similar(self, query: str, limit: int) -> ReadResult: ...

    async def find_nodes(self, words: str, labels: Sequence[str] = ()) -> ReadResult: ...

    async def find_fragments(self, words: str) -> ReadResult: ...


@runtime_checkable
class Investigator(Protocol):
    """Reads around one site and returns a report (investigation.md)."""

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult: ...


@runtime_checkable
class StoryHolds(Protocol):
    """The plan's hold and release calls (host: PlanService). ``reason`` is the plan's block reason of the gap's kind
    (``spec_missing``, ``spec_contradiction`` or ``spec_ambiguous``)."""

    async def hold_tasks(
        self, project_id: str, gap_id: str, spec_ids: Sequence[str], *, reason: str = "spec_ambiguous"
    ) -> HoldResult: ...

    async def release_gap(self, project_id: str, gap_id: str) -> Sequence[str]: ...

    async def check_block(
        self,
        project_id: str,
        story_id: str,
        *,
        claimed_by: str,
        claim_token: str,
        spec_ids: Sequence[str],
    ) -> None: ...

    async def block_story(
        self,
        project_id: str,
        story_id: str,
        *,
        claim_token: str,
        reason: str,
        finding_id: str,
    ) -> Any: ...


@runtime_checkable
class BuildRequests(Protocol):
    """Asks the host for an update build; builds stay one at a time per project."""

    async def request_update_build(self, project_id: str, reason: str, *, immediate: bool) -> str: ...


@runtime_checkable
class PlanView(Protocol):
    """What the gap run reads of the plan: tasks per spec node and their status (host: plan reader)."""

    async def tasks_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[TaskRef]]: ...

    async def stories_for(self, project_id: str, spec_ids: Sequence[str]) -> Mapping[str, Sequence[str]]: ...
