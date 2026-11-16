"""Gap runs: ``run_gap`` and ``estimate_gap`` (docs/graph/gap/api.md "Gap runs", architecture.md "Run steps",
detection.md "Run modes and scope", "Limits", "Run metrics"; FR-GRUN-02 to FR-GRUN-08).

A run executes ``RUN_STEPS`` in order. Each step writes its artifact to the run store (``ctx.store``, a
``GapRunStore``) and marks itself ``done`` before the next starts; every step reads what it needs from the artifacts
of the steps before it, so a fresh run and a resumed one take the same path. A run interrupted by a shutdown or a store
error resumes at its first unfinished step when the host calls ``run_gap`` again with the same run id (a run stopped
after ``detect`` does not compute its seeds again; the graph snapshot is read again, FR-GRUN-07).

Modes: ``update`` (the default after a build) scopes investigation to new and changed sites (sites whose input hash
has a cached verdict are skipped) up to ``limits.sites_update_run``; ``update`` without a finished earlier run behaves
as ``full``; ``full`` investigates up to ``limits.sites_full_run``; ``site`` skips ``scope`` and ``close`` and runs the
other steps for the named sites only. Sites the last finished run deferred come first. Limits are the rules' limits
with the project's overrides (``GapSettings.effective_limits``).

A model failure never fails a run: a section whose relevance call failed stays ``mixed``, a site whose investigation
failed is ``deferred`` (first in the next run), and a run whose every investigation failed ends ``degraded``. Invalid
rules or settings raise ``ConfigurationError``. A ``ConfigurationError`` or ``ConformanceError`` raised by a step
finishes the run ``failed`` (so it never blocks later runs) and is raised again; other store errors pass through and
leave the run resumable.

Slice 1 (session 05): ``close`` is a no-op (session 09), ``investigate`` uses the ``SeedInvestigator`` stub when the
host passes it, ``check`` validates minimally and ``route`` routes every gap ``ask`` without options (session 06).
"""

from __future__ import annotations

import math
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast, get_args

from graph.core.errors import ConfigurationError, ConformanceError
from graph.core.llm.gateway import estimate_tokens
from graph.core.store import as_executor
from graph.gap.context import GapContext
from graph.gap.gaps import StoredGap, read_gaps
from graph.gap.models import (
    GapEstimate,
    GapReport,
    GapRunResult,
    InvestigationResult,
    NoiseReport,
    NoteRow,
    RunRow,
    VerdictRow,
    Witness,
)
from graph.gap.names import GATEWAY_ROLES, RunMode
from graph.gap.run_store import GapRunRecord, GapRunStore
from graph.gap.settings import GapLimits
from graph.gap.steps.check import check_result
from graph.gap.steps.detect import DetectOutcome, Seed, detect
from graph.gap.steps.investigate import SEED_STUB_VERSION, investigate_sites
from graph.gap.steps.record import RecordRun, SiteDecision, counted, outcome_counts, record_sites
from graph.gap.steps.relevance import (
    BATCH_SECTIONS,
    GATEWAY_PROMPT,
    MIXED,
    SECTION_CHARS,
    RelevanceOutcome,
    SectionRelevance,
    classify_sections,
    cue_pattern,
    noise_report,
)
from graph.gap.steps.route import route_report
from graph.gap.steps.scope import scope
from graph.gap.steps.sites import ExistingGap, Site, SiteRun, SitesOutcome, form_sites
from graph.gap.steps.snapshot import GraphSnapshot, read_snapshot
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

StepName = Literal["scope", "close", "relevance", "detect", "sites", "investigate", "check", "route", "record"]
RUN_STEPS: Final[tuple[StepName, ...]] = get_args(StepName)
RUN_MODES: Final[tuple[str, ...]] = get_args(RunMode)
ESTIMATE_MODES: Final = ("update", "full")
RELEVANCE_ROLE: Final = "gap_relevance"
INVESTIGATE_ROLE: Final = "gap_investigate"
UNCERTAIN: Final = "obligation"
"""The last signal of a section the rules could not decide (``classify_sections``)."""
RUN_ID_PREFIX: Final = "gaprun-"

Payload = dict[str, Any]


def _now() -> datetime:
    return datetime.now(UTC)


def new_run_id(now: datetime) -> str:
    """``gaprun-<UTC instant>-<6 hex>``: sortable, usable as a folder name."""

    return f"{RUN_ID_PREFIX}{now.astimezone(UTC):%Y%m%dT%H%M%S%f}-{secrets.token_hex(3)}"


def prompt_versions(ctx: GapContext) -> dict[str, str]:
    """The prompt versions of the run's model steps (part of every site's input hash)."""

    version = getattr(ctx.investigator, "prompt_version", None)
    return {RELEVANCE_ROLE: GATEWAY_PROMPT, INVESTIGATE_ROLE: str(version or SEED_STUB_VERSION)}


# Artifacts: seeds, sites and relevance as JSON payloads


def seed_payload(seed: Seed) -> Payload:
    return {
        "detector": seed.detector,
        "kind": seed.kind,
        "family": seed.family,
        "strength": seed.strength,
        "about": list(seed.about),
        "witness": [item.model_dump(mode="json", exclude_none=True) for item in seed.witness],
        "context": list(seed.context),
        "relevance": seed.relevance,
        "rule": seed.rule,
    }


def seed_from(payload: Mapping[str, Any]) -> Seed:
    return Seed(
        detector=payload["detector"],
        kind=payload["kind"],
        family=payload["family"],
        strength=payload["strength"],
        about=tuple(payload["about"]),
        witness=tuple(Witness.model_validate(item) for item in payload["witness"]),
        context=tuple(payload["context"]),
        relevance=payload["relevance"],
        rule=payload["rule"],
    )


def site_payload(site: Site) -> Payload:
    return {
        "site_id": site.site_id,
        "seeds": [seed_payload(seed) for seed in site.seeds],
        "about": list(site.about),
        "context": list(site.context),
        "input_hash": site.input_hash,
        "rank_score": site.rank_score,
        "reach": site.reach,
        "state": site.state,
        "gap_id": site.gap_id,
        "verdict": None if site.verdict is None else site.verdict.model_dump(mode="json"),
    }


def site_from(payload: Mapping[str, Any]) -> Site:
    verdict = payload.get("verdict")
    return Site(
        site_id=payload["site_id"],
        seeds=tuple(seed_from(item) for item in payload["seeds"]),
        about=tuple(payload["about"]),
        context=tuple(payload["context"]),
        input_hash=payload["input_hash"],
        rank_score=float(payload["rank_score"]),
        reach=int(payload["reach"]),
        state=payload["state"],
        gap_id=payload.get("gap_id"),
        verdict=None if verdict is None else VerdictRow.model_validate(verdict),
    )


def relevance_payload(outcome: RelevanceOutcome) -> Payload:
    return {
        "sections": [
            {
                "lineage": item.section.lineage,
                "heading_path": list(item.section.heading_path),
                "relevance": item.relevance,
                "by": item.by,
                "signals": list(item.signals),
            }
            for item in outcome.sections.values()
        ],
        "model_calls": outcome.model_calls,
        "failed_calls": outcome.failed_calls,
        "tokens": outcome.tokens,
    }


def relevance_from(payload: Mapping[str, Any], snapshot: GraphSnapshot, ctx: GapContext) -> RelevanceOutcome:
    """The relevance outcome of an artifact over the sections of ``snapshot`` (sections no longer there are left
    out; a new section reads as ``spec``)."""

    sections = {section.ident: section for section in snapshot.sections}
    decided: dict[tuple[str, tuple[str, ...]], SectionRelevance] = {}
    for item in payload["sections"]:
        section = sections.get((item["lineage"], tuple(item["heading_path"])))
        if section is not None:
            decided[section.ident] = SectionRelevance(section, item["relevance"], item["by"], tuple(item["signals"]))
    return RelevanceOutcome(
        sections=decided,
        model_calls=int(payload["model_calls"]),
        failed_calls=int(payload["failed_calls"]),
        tokens=int(payload["tokens"]),
        cue_pattern=cue_pattern(ctx.rules.relevance.obligation_cues),
    )


def detection_payload(detection: DetectOutcome, noise: NoiseReport) -> Payload:
    return {
        "seeds": [seed_payload(seed) for seed in detection.seeds],
        "notes": [note.model_dump(mode="json") for note in detection.notes],
        "removed": [
            {"lineage": lineage, "heading_path": list(path), "spec_ids": sorted(ids)}
            for (lineage, path), ids in sorted(detection.removed.items())
        ],
        "by_detector": {code: dict(found) for code, found in detection.by_detector.items()},
        "failures": dict(detection.failures),
        "noise": noise.model_dump(mode="json"),
    }


def detection_from(payload: Mapping[str, Any]) -> DetectOutcome:
    return DetectOutcome(
        seeds=tuple(seed_from(item) for item in payload["seeds"]),
        notes=tuple(NoteRow.model_validate(item) for item in payload["notes"]),
        removed={
            (item["lineage"], tuple(item["heading_path"])): frozenset(item["spec_ids"]) for item in payload["removed"]
        },
        by_detector=payload["by_detector"],
        failures=payload["failures"],
    )


# The run


@dataclass
class _Run:
    """One run in progress: its record, the effective mode and limits, and what it read so far."""

    ctx: GapContext
    store: GapRunStore
    record: GapRunRecord
    limits: GapLimits

    def __post_init__(self) -> None:
        self.executor = as_executor(self.ctx.executor, None)
        self.artifacts: dict[str, Mapping[str, Any]] = {}
        self.snapshot_read: GraphSnapshot | None = None
        self.versions = prompt_versions(self.ctx)
        self.steps: dict[StepName, Callable[[], Awaitable[Payload]]] = {
            "scope": self._scope,
            "close": self._close,
            "relevance": self._relevance,
            "detect": self._detect,
            "sites": self._sites,
            "investigate": self._investigate,
            "check": self._check,
            "route": self._route,
            "record": self._record,
        }

    @property
    def project_id(self) -> str:
        return self.ctx.project_id

    @property
    def run_id(self) -> str:
        return self.record.run_id

    @property
    def mode(self) -> RunMode:
        return self.record.mode

    # Plumbing

    async def snapshot(self) -> GraphSnapshot:
        if self.snapshot_read is None:
            self.snapshot_read = await read_snapshot(self.ctx.reader, self.project_id, self.ctx.ontology)
        return self.snapshot_read

    async def gaps(self) -> tuple[StoredGap, ...]:
        return await read_gaps(self.executor, self.project_id, self.ctx.ontology)

    async def artifact(self, step: StepName) -> Mapping[str, Any]:
        found = self.artifacts.get(step)
        if found is None:
            found = await self.store.get_artifact(self.project_id, self.run_id, step)
            if found is None:
                raise ConfigurationError("gap_run_artifact_missing", run_id=self.run_id, step=step)
            self.artifacts[step] = found
        return found

    async def relevance(self) -> RelevanceOutcome:
        return relevance_from(await self.artifact("relevance"), await self.snapshot(), self.ctx)

    async def detection(self) -> DetectOutcome:
        return detection_from(await self.artifact("detect"))

    async def sites(self) -> list[Site]:
        return [site_from(item) for item in (await self.artifact("sites"))["sites"]]

    async def investigated(self) -> list[Site]:
        return [site for site in await self.sites() if site.state == "investigate"]

    async def execute(self, index: int, step: StepName) -> None:
        if self.record.progress.get(step) != "done":
            await self.store.put_progress(self.project_id, self.run_id, step, "running")
            try:
                payload = await self.steps[step]()
            except Exception:
                await self.store.put_progress(self.project_id, self.run_id, step, "failed")
                log_event(
                    logger,
                    WARNING,
                    "graph.gap.run.step_failed",
                    project_id=self.project_id,
                    run_id=self.run_id,
                    step=step,
                )
                raise
            await self.store.put_artifact(self.project_id, self.run_id, step, payload)
            self.artifacts[step] = payload
            await self.store.put_progress(self.project_id, self.run_id, step, "done")
            log_event(
                logger, INFO, "graph.gap.run.step_done", project_id=self.project_id, run_id=self.run_id, step=step
            )
        if self.ctx.on_progress is not None:
            self.ctx.on_progress(step, index + 1, len(RUN_STEPS))

    # Steps

    async def _scope(self) -> Payload:
        if self.mode == "site":
            return {"skipped": True}
        outcome = await scope(
            self.ctx.executor,
            self.ctx.ontology,
            self.project_id,
            self.ctx.changes,
            await self.gaps(),
            run_id=self.run_id,
        )
        return {"skipped": False, **outcome.payload()}

    async def _close(self) -> Payload:
        # Session 09 implements the close check (lifecycle.md "Close check"); the step exists so runs resume the same.
        return {"skipped": self.mode == "site", "implemented": False}

    async def _relevance(self) -> Payload:
        outcome = await classify_sections(
            await self.snapshot(),
            project_id=self.project_id,
            rules=self.ctx.rules,
            records=self.ctx.records,
            gateway=self.ctx.gateway,
        )
        return relevance_payload(outcome)

    async def _detect(self) -> Payload:
        relevance = await self.relevance()
        detection = detect(
            await self.snapshot(),
            rules=self.ctx.rules,
            relevance=relevance,
            build=self.ctx.build,
            project_id=self.project_id,
        )
        return detection_payload(detection, noise_report(relevance, detection.removed))

    async def _sites(self) -> Payload:
        previous = await self.store.latest_run(self.project_id, finished_only=True)
        last = None if previous is None else await self.store.get_result(self.project_id, previous.run_id)
        existing = [
            ExistingGap(gap.gap_id, gap.signature, gap.status, gap.about_ids, gap.origin)
            for gap in await self.gaps()
            if gap.signature
        ]
        outcome = await form_sites(
            await self.snapshot(),
            await self.detection(),
            SiteRun(
                project_id=self.project_id,
                run_id=self.run_id,
                mode=self.mode,
                rules=self.ctx.rules,
                limits=self.limits,
                records=self.ctx.records,
                reader=self.ctx.reader,
                plan=self.ctx.plan,
                existing=existing,
                deferred_first=() if last is None else last.deferred_sites,
                site_ids=self.record.site_ids,
                prompt_versions=self.versions,
                use_cache=not self.ctx.settings.ignore_cache,
            ),
        )
        return {"sites": [site_payload(site) for site in outcome.sites], "counts": dict(outcome.counts)}

    async def _investigate(self) -> Payload:
        results = await investigate_sites(
            await self.investigated(),
            self.ctx.investigator,
            project_id=self.project_id,
            run_id=self.run_id,
            mode=self.mode,
            limits=self.limits,
        )
        return {"results": {site_id: result.model_dump(mode="json") for site_id, result in results.items()}}

    async def _check(self) -> Payload:
        results = (await self.artifact("investigate"))["results"]
        snapshot = await self.snapshot()
        checked: Payload = {}
        for site in await self.investigated():
            stored = results.get(site.site_id)
            result = (
                InvestigationResult(error="no_result") if stored is None else InvestigationResult.model_validate(stored)
            )
            found = check_result(result, snapshot, seeds=len(site.seeds))
            checked[site.site_id] = {
                "outcome": found.outcome,
                "report": None if found.report is None else found.report.model_dump(mode="json"),
                "warnings": list(found.warnings),
            }
        return {"checked": checked}

    async def _route(self) -> Payload:
        checked = (await self.artifact("check"))["checked"]
        reports = {
            site_id: GapReport.model_validate(item["report"])
            for site_id, item in checked.items()
            if item["outcome"] == "gap" and item["report"] is not None
        }
        planned: frozenset[str] | None = None
        if self.ctx.plan is not None and reports:
            ids = sorted({spec_id for report in reports.values() for spec_id in (*report.about, *report.impacts)})
            tasks = await self.ctx.plan.tasks_for(self.project_id, ids)
            planned = frozenset(spec_id for spec_id, found in tasks.items() if found)
        snapshot = await self.snapshot()
        routed: Payload = {}
        for site_id, report in reports.items():
            decision = route_report(report, snapshot, planned=planned)
            routed[site_id] = {"severity": decision.severity, "route": decision.route}
        return {"routed": routed}

    async def _record(self) -> Payload:
        checked = (await self.artifact("check"))["checked"]
        routed = (await self.artifact("route"))["routed"]
        decisions: list[SiteDecision] = []
        for site in await self.investigated():
            item = checked[site.site_id]
            decided = routed.get(site.site_id, {})
            decisions.append(
                SiteDecision(
                    site=site,
                    outcome=item["outcome"],
                    report=None if item["report"] is None else GapReport.model_validate(item["report"]),
                    severity=decided.get("severity"),
                    route=decided.get("route"),
                )
            )
        outcome = await record_sites(
            RecordRun(
                source=self.ctx.executor,
                executor=self.executor,
                ontology=self.ctx.ontology,
                records=self.ctx.records,
                project_id=self.project_id,
                run_id=self.run_id,
                now=_now,
                model=self.versions[INVESTIGATE_ROLE],
                prompt_version=self.versions[INVESTIGATE_ROLE],
            ),
            await self.snapshot(),
            await self.gaps(),
            decisions,
        )
        return outcome.payload()

    # The result

    async def result(self) -> GapRunResult:
        scoped, relevance, detection = (
            await self.artifact("scope"),
            await self.artifact("relevance"),
            await self.artifact("detect"),
        )
        sites, investigated = await self.artifact("sites"), await self.artifact("investigate")
        checked, recorded = (await self.artifact("check"))["checked"], await self.artifact("record")
        results = [InvestigationResult.model_validate(item) for item in investigated["results"].values()]
        failed = sum(1 for result in results if result.report is None or result.error is not None)
        outcomes = recorded["outcomes"]
        deferred = sorted(
            {item["site_id"] for item in sites["sites"] if item["state"] == "deferred"}
            | {site_id for site_id, value in outcomes.items() if value[0] == "deferred"}
        )
        cost = _cost(relevance, results)
        noise = NoiseReport.model_validate(detection["noise"])
        site_counts = dict(sites["counts"].get("sites", {}))
        counts: Payload = {
            "scope": _scope_counts(scoped),
            "sections_by_relevance": _sections_by_relevance(relevance),
            "seeds_by_detector": sites["counts"].get("seeds_by_detector", {}),
            "detector_failures": sites["counts"].get("detector_failures", {}),
            "sites": {
                "formed": site_counts.get("formed", 0),
                "investigated": len(results),
                "skipped": site_counts.get("skipped", 0),
                "deferred": len(deferred),
            },
            "outcomes": outcome_counts(outcomes),
            "gaps_new": counted(recorded["gaps_new"]),
            "gaps_updated": counted(recorded["gaps_updated"]),
            "routes": {"ask": recorded["routes"].get("ask", 0), "ai": recorded["routes"].get("ai", 0)},
            "notes": int(sites["counts"].get("notes", 0)) + int(recorded["notes"]),
            "model_calls": {role: value["calls"] for role, value in cost.items()},
            "tokens": {role: value["tokens"] for role, value in cost.items()},
            "noise_nodes": len({spec_id for section in noise.sections for spec_id in section.removed}),
        }
        warnings = sorted(
            {f"detector_failed:{code}" for code in detection["failures"]}
            | {code for item in checked.values() for code in item["warnings"]}
        )
        return GapRunResult(
            run_id=self.run_id,
            mode=self.mode,
            build_id=self.record.build_id,
            status="degraded" if results and failed == len(results) else "completed",
            counts=counts,
            cost=cost,
            noise=noise,
            deferred_sites=tuple(deferred),
            warnings=tuple(warnings),
        )


def _scope_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    """Builds, aliases, touched ids and moved relations of the scope step (nothing for a ``site`` run)."""

    if payload.get("skipped"):
        return {}
    return {
        "builds": int(payload["builds"]),
        "aliases": len(payload["aliases"]),
        "touched": len(payload["touched"]),
        "moved": int(payload["moved"]),
    }


def _sections_by_relevance(payload: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    found: dict[str, Counter[str]] = {}
    for item in payload["sections"]:
        found.setdefault(item["relevance"], Counter())[item["by"]] += 1
    return {relevance: dict(sorted(by.items())) for relevance, by in sorted(found.items())}


def _cost(relevance: Mapping[str, Any], results: Sequence[InvestigationResult]) -> dict[str, dict[str, int]]:
    """Calls and tokens per gateway role: the relevance step's calls and the investigations' (judge and edits calls
    come with session 06)."""

    cost: dict[str, dict[str, int]] = {role: {"calls": 0, "tokens": 0} for role in GATEWAY_ROLES}
    cost[RELEVANCE_ROLE] = {"calls": int(relevance["model_calls"]), "tokens": int(relevance["tokens"])}
    cost[INVESTIGATE_ROLE] = {
        "calls": sum(result.model_calls for result in results),
        "tokens": sum(result.tokens for result in results),
    }
    return cost


def _store(ctx: GapContext) -> GapRunStore:
    if ctx.store is None:
        raise ConfigurationError("gap_run_store_missing")
    return cast(GapRunStore, ctx.store)


async def _open(
    ctx: GapContext, store: GapRunStore, mode: str, site_ids: Sequence[str], limits: GapLimits
) -> tuple[_Run, GapRunResult | None]:
    """The run to execute (new or resumed), or a finished run and its stored result."""

    project_id = ctx.project_id
    if ctx.run_id is not None:
        found = await store.get_run(project_id, ctx.run_id)
        if found is not None:
            stored = await store.get_result(project_id, found.run_id) if found.finished else None
            if found.finished and stored is None:
                raise ConfigurationError("gap_run_result_missing", run_id=found.run_id)
            return _Run(ctx, store, found, limits), stored
    else:
        latest = await store.latest_run(project_id)
        if latest is not None and not latest.finished:
            raise ConfigurationError("unfinished_gap_run", run_id=latest.run_id)
    now = _now()
    previous = await store.latest_run(project_id, finished_only=True)
    effective = cast(RunMode, "full" if mode == "update" and previous is None else mode)
    build_id = ctx.build.build_id or (ctx.changes[-1].build_id if ctx.changes else None)
    record = GapRunRecord(
        project_id=project_id,
        run_id=ctx.run_id or new_run_id(now),
        mode=effective,
        build_id=build_id,
        started_at=now,
        progress=dict.fromkeys(RUN_STEPS, "pending"),
        site_ids=tuple(site_ids),
    )
    await store.create_run(record)
    run = _Run(ctx, store, record, limits)
    await ctx.records.start_run(
        project_id,
        RunRow(
            run_id=record.run_id,
            build_id=build_id,
            mode=effective,
            status="running",
            started_at=now,
            rules_version=ctx.rules.version,
            prompt_versions=run.versions,
        ),
    )
    log_event(
        logger,
        INFO,
        "graph.gap.run.started",
        project_id=project_id,
        run_id=record.run_id,
        mode=effective,
        requested=mode,
        builds=len(ctx.changes),
    )
    return run, None


async def _fail(ctx: GapContext, store: GapRunStore, run: _Run, code: str) -> None:
    """Finish a run that hit a non-transient error (invalid rules, settings, artifacts or a port's refusal) as
    ``failed``, so it does not block the project's later runs; a transient store error leaves it resumable."""

    result = GapRunResult(
        run_id=run.run_id, mode=run.mode, build_id=run.record.build_id, status="failed", warnings=(code,)
    )
    await ctx.records.finish_run(ctx.project_id, run.run_id, {}, {}, status="failed")
    await store.finish_run(ctx.project_id, result)
    log_event(logger, WARNING, "graph.gap.run.failed", project_id=ctx.project_id, run_id=run.run_id, code=code)


def _check_mode(mode: str, site_ids: Sequence[str], allowed: Sequence[str]) -> None:
    if mode not in allowed:
        raise ConfigurationError("invalid_gap_run_mode", mode=mode)
    if mode == "site" and not site_ids:
        raise ConfigurationError("gap_site_ids_missing")


async def run_gap(
    ctx: GapContext,
    *,
    mode: Literal["update", "full", "site"] = "update",
    site_ids: Sequence[str] = (),
) -> GapRunResult:
    """Run (or resume, when ``ctx.run_id`` names an unfinished run) a gap run; module docstring. A finished run's
    stored result is returned as it is. ``ConfigurationError("unfinished_gap_run")`` names the project's unfinished
    run when ``ctx.run_id`` is None (the host resumes it by passing that id)."""

    _check_mode(mode, site_ids, RUN_MODES)
    store = _store(ctx)
    limits = ctx.settings.effective_limits(ctx.rules.limits)
    run, stored = await _open(ctx, store, mode, site_ids, limits)
    if stored is not None:
        return stored
    try:
        for index, step in enumerate(RUN_STEPS):
            await run.execute(index, step)
        result = await run.result()
    except (ConfigurationError, ConformanceError) as error:
        await _fail(ctx, store, run, error.code)
        raise
    await ctx.records.finish_run(ctx.project_id, run.run_id, result.counts, result.cost, status=result.status)
    await store.finish_run(ctx.project_id, result)
    log_event(
        logger,
        INFO if result.status == "completed" else WARNING,
        "graph.gap.run.finished",
        project_id=ctx.project_id,
        run_id=run.run_id,
        status=result.status,
        investigated=result.counts["sites"]["investigated"],
        gaps_new=result.counts["gaps_new"]["total"],
        deferred=len(result.deferred_sites),
    )
    return result


async def estimate_gap(ctx: GapContext, *, mode: Literal["update", "full"] = "update") -> GapEstimate:
    """What a run would do (api.md "Gap runs"): scope, relevance by the rules only and detection, then the sites; the
    sites to investigate, the cached ones, and the expected calls and tokens per gateway role. No model call, no
    write (FR-GRUN-06). Expected investigation calls are the bound of the loop: ``rounds_per_site`` per site, each of
    at most ``context_tokens``."""

    _check_mode(mode, (), ESTIMATE_MODES)
    limits = ctx.settings.effective_limits(ctx.rules.limits)
    previous = None if ctx.store is None else await _store(ctx).latest_run(ctx.project_id, finished_only=True)
    effective = cast(RunMode, "full" if mode == "update" and previous is None else mode)
    last = None if previous is None else await _store(ctx).get_result(ctx.project_id, previous.run_id)
    executor = as_executor(ctx.executor, None)
    gaps = await read_gaps(executor, ctx.project_id, ctx.ontology)
    await scope(ctx.executor, ctx.ontology, ctx.project_id, ctx.changes, gaps, persist=False)
    snapshot = await read_snapshot(ctx.reader, ctx.project_id, ctx.ontology)
    relevance = await classify_sections(
        snapshot,
        project_id=ctx.project_id,
        rules=ctx.rules,
        records=ctx.records,
        gateway=None,
        use_model=False,
        persist=False,
    )
    detection = detect(snapshot, rules=ctx.rules, relevance=relevance, build=ctx.build, project_id=ctx.project_id)
    sites: SitesOutcome = await form_sites(
        snapshot,
        detection,
        SiteRun(
            project_id=ctx.project_id,
            run_id="estimate",
            mode=effective,
            rules=ctx.rules,
            limits=limits,
            records=ctx.records,
            reader=ctx.reader,
            plan=ctx.plan,
            existing=[
                ExistingGap(gap.gap_id, gap.signature, gap.status, gap.about_ids, gap.origin)
                for gap in gaps
                if gap.signature
            ],
            deferred_first=() if last is None else last.deferred_sites,
            prompt_versions=prompt_versions(ctx),
            use_cache=not ctx.settings.ignore_cache,
            persist=False,
        ),
    )
    uncertain = [
        item
        for item in relevance.sections.values()
        if ctx.rules.relevance.classify_with_model
        and item.by == "rules"
        and item.relevance == MIXED
        and item.signals[-1:] == (UNCERTAIN,)
    ]
    investigate = len(sites.investigate)
    calls: dict[str, int] = dict.fromkeys(GATEWAY_ROLES, 0)
    tokens: dict[str, int] = dict.fromkeys(GATEWAY_ROLES, 0)
    calls[RELEVANCE_ROLE] = math.ceil(len(uncertain) / BATCH_SECTIONS)
    tokens[RELEVANCE_ROLE] = sum(estimate_tokens(item.section.text[:SECTION_CHARS]) for item in uncertain)
    calls[INVESTIGATE_ROLE] = investigate * limits.rounds_per_site
    tokens[INVESTIGATE_ROLE] = calls[INVESTIGATE_ROLE] * limits.context_tokens
    log_event(
        logger,
        INFO,
        "graph.gap.run.estimated",
        project_id=ctx.project_id,
        mode=effective,
        sites=investigate,
        cached=len(sites.skipped),
    )
    return GapEstimate(sites=investigate, cached=len(sites.skipped), calls=calls, tokens=tokens)
