"""Gap runs: ``run_gap`` and ``estimate_gap`` (docs/graph/gap/api.md "Gap runs", architecture.md "Run steps",
detection.md "Run modes and scope", "Limits", "Run metrics"; FR-GRUN-02 to FR-GRUN-08).

A run executes ``RUN_STEPS`` in order. Each step writes its artifact to the run store (``ctx.store``, a
``GapRunStore``) and marks itself ``done`` before the next starts; every step reads what it needs from the artifacts
of the steps before it, so a fresh run and a resumed one take the same path. A run interrupted by a shutdown or a store
error resumes at its first unfinished step, and in ``investigate`` at its first site not recorded, when the host calls
``run_gap`` again with the same run id (a run stopped after ``detect`` does not compute its seeds again; the graph
snapshot is read again, FR-GRUN-07). A run an earlier version left unfinished, with the steps ``check``, ``route`` and
``record``, investigates its sites again.

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

Investigation (investigation.md): ``investigate`` takes the sites one by one and finishes each before the next starts,
so its gaps are in the graph (and in the inbox) while the run goes on: it builds the site's context pack and read menu
and calls ``ctx.investigator`` (the structured loop ``LoopInvestigator`` by default); ``check`` validates the report,
finds duplicates (of the gaps in the graph, the ones this run recorded included) and runs the second check of a
pointer or an AI answer; ``route`` decides severity and route (the gates, and the valve of AI answers counting over the
run); ``record`` writes the gap, its options, a pointer clarification or an AI answer (a ``pending`` clarification of
mode ``ai`` with its amendment rows: staged like a person's answer until "Resolve gaps"); then the run releases the
tasks of the gaps the site ended, holds the tasks of the blockers it touched and turns the sections of a ``not_spec``
report ``mixed`` (FR-GREL-07). The site's artifact (``site-<hex>``) is stored once it is routed (``routed``), before its
first write (``recording``: the route, the instant of its writes and the gaps its record reads) and once it is recorded
(``recorded``): a resumed run records a routed site's stored report, writes a ``recording`` site again over its stored
gaps from its stored instant on (``RecordRun.replay``: a graph write that landed is not repeated, a person's change
since stands, notes and events are appended once) and leaves a recorded site alone. ``finish`` lists
the nodes of ``not_spec`` reports in the run's noise report and holds the tasks of every open blocker gap once more.
``close`` is the close check of applied answers, obsolete gaps and stale witnesses (``steps.close``, lifecycle.md
"Close check"); the follow-up gaps it opens get a site in the same run.
"""

from __future__ import annotations

import itertools
import math
import secrets
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast, get_args

from graph.core.errors import ConfigurationError, ConformanceError
from graph.core.llm.gateway import estimate_tokens
from graph.core.store import as_executor
from graph.gap.context import GapContext
from graph.gap.gaps import StoredGap, gap_from_payload, gap_payload, read_gaps
from graph.gap.holds import aliases_of, hold_blockers, release
from graph.gap.models import (
    GapEstimate,
    GapReport,
    GapRunResult,
    HoldResult,
    InvestigationResult,
    NoiseReport,
    NoiseSection,
    NoteRow,
    RunRow,
    VerdictRow,
    Witness,
)
from graph.gap.names import GATEWAY_ROLES, RunMode
from graph.gap.prompts import AI_ANSWER_PROMPT, INVESTIGATE_PROMPT, JUDGE_PROMPT, RELEVANCE_PROMPT
from graph.gap.run_store import GapRunRecord, GapRunStore
from graph.gap.settings import GapLimits
from graph.gap.steps.check import Checked, Checker, CheckRun
from graph.gap.steps.close import CloseRun, close_gaps
from graph.gap.steps.detect import DetectOutcome, Seed, detect
from graph.gap.steps.investigate import Investigated, InvestigateRun, investigate_site
from graph.gap.steps.record import (
    RecordRun,
    SiteDecision,
    counted,
    merged,
    outcome_counts,
    reads_of,
    record_sites,
    report_signature,
)
from graph.gap.steps.relevance import (
    BATCH_SECTIONS,
    MIXED,
    SECTION_CHARS,
    RelevanceOutcome,
    SectionRelevance,
    classify_sections,
    cue_pattern,
    mark_not_spec,
    noise_report,
)
from graph.gap.steps.route import Routed, RouteInputs, Valve, route_report, routed_payload, touched
from graph.gap.steps.scope import scope
from graph.gap.steps.sites import RAISED_ORIGINS, SITE_PREFIX, ExistingGap, Site, SiteRun, SitesOutcome, form_sites
from graph.gap.steps.snapshot import GraphSnapshot, read_snapshot
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

StepName = Literal["scope", "close", "relevance", "detect", "sites", "investigate", "finish"]
RUN_STEPS: Final[tuple[StepName, ...]] = get_args(StepName)
SITE_ARTIFACT: Final = "site-"
"""A site's artifact is ``site-`` and the hex of its site id."""
ROUTED: Final = "routed"
"""A site's artifact state once it is investigated, checked and routed: a resumed run records this report."""
RECORDING: Final = "recording"
"""A site's artifact state before its first write: a resumed run writes the same again (``RecordRun.replay``)."""
RECORDED: Final = "recorded"
"""A site's artifact state once its gaps and rows are written: a resumed run leaves the site alone."""
RUN_MODES: Final[tuple[str, ...]] = get_args(RunMode)
ESTIMATE_MODES: Final = ("update", "full")
RELEVANCE_ROLE: Final = "gap_relevance"
INVESTIGATE_ROLE: Final = "gap_investigate"
JUDGE_ROLE: Final = "gap_judge"
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
    return {
        RELEVANCE_ROLE: RELEVANCE_PROMPT,
        INVESTIGATE_ROLE: str(version or INVESTIGATE_PROMPT),
        JUDGE_ROLE: f"{JUDGE_PROMPT}+{AI_ANSWER_PROMPT}",
    }


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
            "finish": self._finish,
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

    async def execute(self, step: StepName) -> None:
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

    def report_items(self, step: StepName, done: int, total: int) -> None:
        """Tell ``ctx.on_progress`` (when set) that ``done`` of ``total`` items of ``step`` are done."""

        if self.ctx.on_progress is not None:
            self.ctx.on_progress(step, done, total)

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
        if self.mode == "site":
            return {"skipped": True}
        aliases = await self._aliases()
        outcome = await close_gaps(
            CloseRun(
                source=self.ctx.executor,
                executor=self.executor,
                ontology=self.ctx.ontology,
                rules=self.ctx.rules,
                records=self.ctx.records,
                amendments=self.ctx.amendments,
                project_id=self.project_id,
                run_id=self.run_id,
                now=_now,
                build_id=self.record.build_id,
                build=self.ctx.build,
            ),
            await self.snapshot(),
            await self.gaps(),
        )
        payload = outcome.payload()
        ended = [*outcome.resolved, *outcome.obsolete]  # FR-GPLN-04: their held tasks return to pending
        payload["released"] = await self._release(ended, aliases)
        return payload

    async def _aliases(self) -> dict[str, tuple[str, ...]]:
        """The aliases of the project's gaps before a step that may delete some (read first: a deleted gap's
        ``FND::`` alias must still be released)."""

        if self.ctx.holds is None:
            return {}
        return {gap.gap_id: aliases_of(gap.props) for gap in await self.gaps()}

    async def _release(self, gap_ids: Sequence[str], aliases: Mapping[str, Sequence[str]]) -> list[str]:
        """Release the tasks the ended gaps hold (by id and by alias); the task ids released."""

        if self.ctx.holds is None or not gap_ids:
            return []
        released: list[str] = []
        for gap_id in dict.fromkeys(gap_ids):
            released += await release(self.ctx.holds, self.project_id, gap_id, aliases.get(gap_id, ()))
        return sorted(set(released))

    async def follow_ups(self) -> dict[str, str]:
        """The follow-up gaps this run's close step opened, to the gaps they continue (each gets a site here)."""

        closed = await self.artifact("close")
        return dict(closed.get("follow_ups", {}))

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
        existing = [existing_gap(gap) for gap in await self.gaps() if gap.signature]
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
                follow_ups=await self.follow_ups(),
                prompt_versions=self.versions,
                use_cache=not self.ctx.settings.ignore_cache,
            ),
        )
        return {"sites": [site_payload(site) for site in outcome.sites], "counts": dict(outcome.counts)}

    async def _investigate(self) -> Payload:
        """Each site in turn: investigated, checked, routed and recorded before the next site starts (module
        docstring); a resumed run goes on from each site's stored state (``routed``, ``recording``, ``recorded``)."""

        sites, earlier = await self.investigated(), await self.gaps()
        snapshot = await self.snapshot()
        valve = Valve(self.limits.ai_answers_per_run, self.limits.ai_answers_max_node_share, len(snapshot.nodes))
        found: dict[str, Payload] = {}
        for site in sites:
            stored = await self.store.get_artifact(self.project_id, self.run_id, site_artifact(site.site_id))
            if stored is not None:
                found[site.site_id] = dict(stored)
                _count_ai(valve, stored)
        done = sum(1 for item in found.values() if item["state"] == RECORDED)
        self.report_items("investigate", done, len(sites))
        for site in sites:
            payload = found.get(site.site_id) or await self._decide(site, earlier, valve)
            if payload["state"] != RECORDED:
                payload = await self._record_site(site, payload)
                done += 1
                self.report_items("investigate", done, len(sites))
            found[site.site_id] = payload
        results = [Investigated.from_payload(item["investigated"]).result for item in found.values()]
        log_event(
            logger,
            INFO,
            "graph.gap.investigate.finished",
            project_id=self.project_id,
            run_id=self.run_id,
            sites=len(sites),
            failed=sum(1 for result in results if result.report is None),
            calls=sum(result.model_calls for result in results),
        )
        return {"sites": found, "recorded": merged(item["recorded"] for item in found.values())}

    async def _decide(self, site: Site, earlier: Sequence[StoredGap], valve: Valve) -> Payload:
        """One site investigated (its pack lists the gaps of ``earlier``, read when the step started), checked against
        the gaps in the graph now (the ones this run recorded included) and routed; stored as ``routed``."""

        snapshot = await self.snapshot()
        investigated = await investigate_site(
            site,
            self.ctx.investigator,
            snapshot,
            InvestigateRun(
                project_id=self.project_id,
                run_id=self.run_id,
                mode=self.mode,
                limits=self.limits,
                reader=self.ctx.reader,
                gaps=earlier,
                plan=self.ctx.plan,
            ),
        )
        gaps = await self.gaps()
        routing = self.ctx.rules.routing
        checker = Checker(
            CheckRun(
                project_id=self.project_id,
                run_id=self.run_id,
                snapshot=snapshot,
                limits=self.limits,
                records=self.ctx.records,
                gateway=self.ctx.gateway,
                reader=self.ctx.reader,
                gaps=gaps,
                min_confidence=routing.gates.min_confidence,
                signature=report_signature,
                gates=routing.gates,
                ai_kinds=routing.ai_kinds,
                ai_answers=self.ctx.settings.ai_answers,
            )
        )
        checked = await checker.check(site, investigated)
        routed = await self._route(site, checked, gaps, valve)
        payload: Payload = {
            "state": ROUTED,
            "investigated": investigated.payload(),
            "checked": checked.payload(),
            "usage": {"calls": checker.usage.calls, "tokens": checker.usage.tokens},
            "routed": None if routed is None else dict(routed_payload(routed)),
        }
        await self.store.put_artifact(self.project_id, self.run_id, site_artifact(site.site_id), payload)
        return payload

    async def _route(self, site: Site, checked: Checked, gaps: Sequence[StoredGap], valve: Valve) -> Routed | None:
        """The severity and route of a checked gap (``route_report``) through the run's valve; None for a site with no
        gap to route."""

        report = checked.report
        if checked.outcome != "gap" or report is None:
            return None
        planned: frozenset[str] | None = None
        if self.ctx.plan is not None:
            ids = sorted(touched(report))
            tasks = await self.ctx.plan.tasks_for(self.project_id, ids) if ids else {}
            planned = frozenset(spec_id for spec_id, found in tasks.items() if found)
        routing = self.ctx.rules.routing
        inputs = RouteInputs(
            planned=planned, ai_answers=self.ctx.settings.ai_answers, ai_kinds=routing.ai_kinds, gates=routing.gates
        )
        known = {gap.signature for gap in gaps if gap.signature}
        new = site.gap_id is None and report_signature(site, report) not in known
        routed = route_report(
            report,
            await self.snapshot(),
            inputs,
            pointer=checked.pointer,
            judge=checked.ai_judge,
            judge_failed=checked.ai_judge_failed,
            new=new and checked.new,
            origin=_origin(site),
            seed_kinds=tuple(seed.kind for seed in site.seeds),
        )
        return valve.admit(routed, touched(report))

    async def _record_site(self, site: Site, payload: Payload) -> Payload:
        """Write a routed site and settle what it changed: the tasks its ended gaps held are released, the blockers it
        touched hold their tasks again and its ``not_spec`` sections turn ``mixed``. Before the first write the site's
        artifact turns ``recording`` with the decision, the instant and the gaps the write reads (``reads_of``), so a
        resumed run writes the same again over them; then ``recorded``."""

        if payload["state"] == ROUTED:
            item = Checked.from_payload(payload["checked"])
            decided = self._still_ai(dict(payload["routed"] or {}), item)
            reads = reads_of(await self.gaps(), _decision(site, item, decided))
            payload = {
                **payload,
                "state": RECORDING,
                "decided": decided,
                "instant": _now().isoformat(),
                "reads": [gap_payload(gap) for gap in reads],
            }
            await self.store.put_artifact(self.project_id, self.run_id, site_artifact(site.site_id), payload)
            replay = False
        else:
            replay = True
        decision = _decision(site, Checked.from_payload(payload["checked"]), payload["decided"])
        reads = tuple(gap_from_payload(item) for item in payload["reads"])
        instant = datetime.fromisoformat(payload["instant"])
        outcome = await record_sites(
            RecordRun(
                source=self.ctx.executor,
                executor=self.executor,
                ontology=self.ctx.ontology,
                records=self.ctx.records,
                project_id=self.project_id,
                run_id=self.run_id,
                now=_ticking(instant),
                model=self.versions[INVESTIGATE_ROLE],
                prompt_version=self.versions[INVESTIGATE_ROLE],
                build_id=self.record.build_id,
                amendments=self.ctx.amendments,
                replay=replay,
            ),
            await self.snapshot(),
            reads,
            [decision],
        )
        released = await self._release(outcome.releases, {gap.gap_id: aliases_of(gap.props) for gap in reads})
        touched_ids = {spec_id for _, about in outcome.holds.values() for spec_id in about}
        touched_ids |= {spec_id for gap in reads if gap.gap_id in outcome.releases for spec_id in gap.about_ids}
        held = HoldResult()
        if self.ctx.holds is not None and touched_ids:
            held = await hold_blockers(self.ctx.holds, self.project_id, await self.gaps(), touched_ids)
        await self._mark_not_spec(outcome.not_spec)
        recorded: Payload = {
            **payload,
            "state": RECORDED,
            "recorded": outcome.payload(),
            "released": released,
            "held": list(held.held),
        }
        await self.store.put_artifact(self.project_id, self.run_id, site_artifact(site.site_id), recorded)
        return recorded

    async def _finish(self) -> Payload:
        """The noise report of the ``not_spec`` sites (FR-GREL-07) and the holds of every open blocker gap (GD2)."""

        recorded = (await self.artifact("investigate"))["recorded"]
        payload: Payload = {"noise": await self._noise(recorded["not_spec"])}
        # GD2: every open blocker gap holds its tasks, the ones this run wrote or re-checked and those a plan run
        # reset, a claim left alone or a follow-up inherited (idempotent: held tasks stay as they are).
        held = await hold_blockers(self.ctx.holds, self.project_id, await self.gaps())
        payload["held_tasks"] = list(held.held)
        payload["in_progress_tasks"] = list(held.in_progress)
        return payload

    def _still_ai(self, decided: Payload, item: Checked) -> Payload:
        """A stored AI route, demoted to ``ask`` when the run resumes with AI answers off or a kind no AI kind."""

        kinds = self.ctx.rules.routing.ai_kinds
        if decided.get("mode") != "ai" or (
            self.ctx.settings.ai_answers and item.report is not None and item.report.kind in kinds
        ):
            return decided
        return decided | {"route": "ask", "mode": None, "reasons": ["ai_answers_off"]}

    async def _mark_not_spec(self, sites: Mapping[str, Sequence[str]]) -> None:
        """FR-GREL-07: the sections of a ``not_spec`` site's nodes become ``mixed`` (``mark_not_spec``)."""

        ids = sorted({spec_id for found in sites.values() for spec_id in found})
        if ids:
            relevance = await self.relevance()
            await mark_not_spec(
                await self.snapshot(), relevance, ids, project_id=self.project_id, records=self.ctx.records
            )

    async def _noise(self, sites: Mapping[str, Sequence[str]]) -> list[Payload]:
        """The nodes of the ``not_spec`` sites under their sections, for the run's noise report (FR-GREL-07)."""

        snapshot = await self.snapshot()
        by_section: dict[tuple[str, tuple[str, ...]], set[str]] = {}
        for spec_id in sorted({spec_id for found in sites.values() for spec_id in found}):
            for section in snapshot.node_sections.get(spec_id, ()):
                by_section.setdefault(section.ident, set()).add(spec_id)
        return [
            {"lineage": lineage, "heading_path": list(path), "spec_ids": sorted(found)}
            for (lineage, path), found in sorted(by_section.items())
        ]

    # The result

    async def result(self) -> GapRunResult:
        scoped, relevance, detection = (
            await self.artifact("scope"),
            await self.artifact("relevance"),
            await self.artifact("detect"),
        )
        sites, investigated, finished = (
            await self.artifact("sites"),
            await self.artifact("investigate"),
            await self.artifact("finish"),
        )
        found, recorded = list(investigated["sites"].values()), investigated["recorded"]
        results = [Investigated.from_payload(item["investigated"]).result for item in found]
        failed = sum(1 for result in results if result.report is None or result.error is not None)
        outcomes = recorded["outcomes"]
        deferred = sorted(
            {item["site_id"] for item in sites["sites"] if item["state"] == "deferred"}
            | {site_id for site_id, value in outcomes.items() if value[0] == "deferred"}
        )
        cost = _cost(relevance, results, _usage(item["usage"] for item in found))
        noise = _with_not_spec(NoiseReport.model_validate(detection["noise"]), finished.get("noise", ()))
        site_counts = dict(sites["counts"].get("sites", {}))
        counts: Payload = {
            "scope": _scope_counts(scoped),
            "close": _close_counts(await self.artifact("close")),
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
            "pointers": int(recorded.get("pointers", 0)),
            "ai_answered": int(recorded.get("ai_answered", 0)),
            "rejected": int(recorded.get("rejected", 0)),
            "notes": int(sites["counts"].get("notes", 0)) + int(recorded["notes"]),
            "model_calls": {role: value["calls"] for role, value in cost.items()},
            "tokens": {role: value["tokens"] for role, value in cost.items()},
            "noise_nodes": len({spec_id for section in noise.sections for spec_id in section.removed}),
        }
        warnings = sorted(
            {f"detector_failed:{code}" for code in detection["failures"]}
            | {code for item in found for code in item["checked"]["warnings"]}
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
            held_tasks=tuple(finished.get("held_tasks", ())),
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


def _close_counts(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Gaps the close step resolved, followed up, left waiting and took out, and stale witnesses removed."""

    if payload.get("skipped") or "resolved" not in payload:
        return {}
    counts: dict[str, Any] = {
        "resolved": len(payload["resolved"]),
        "follow_ups": len(payload["follow_ups"]),
        "waiting": len(payload["waiting"]),
        "obsolete": len(payload["obsolete"]),
        "cites_removed": int(payload["cites_removed"]),
    }
    reconciled = {
        name: len(ids) for name, ids in sorted(payload.get("reconciled", {}).items()) if name != "follow_ups" and ids
    }
    if reconciled:  # answers reconcile changed: absorbed, conflict, orphaned, rechecked
        counts["reconciled"] = reconciled
    return counts


def _sections_by_relevance(payload: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    found: dict[str, Counter[str]] = {}
    for item in payload["sections"]:
        found.setdefault(item["relevance"], Counter())[item["by"]] += 1
    return {relevance: dict(sorted(by.items())) for relevance, by in sorted(found.items())}


def site_artifact(site_id: str) -> str:
    """The run store name of a site's artifact (``SITE_ARTIFACT``)."""

    return f"{SITE_ARTIFACT}{site_id.removeprefix(SITE_PREFIX)}"


def _ticking(instant: datetime) -> Callable[[], datetime]:
    """A clock that gives ``instant``, then one microsecond more at each call: a site written again gets the same
    instants, and its events keep their order."""

    ticks = itertools.count()
    return lambda: instant + timedelta(microseconds=next(ticks))


def _decision(site: Site, item: Checked, decided: Mapping[str, Any]) -> SiteDecision:
    """What the record of a site takes of its check and its route (``decided``: the stored route, demoted or not)."""

    return SiteDecision(
        site=site,
        outcome=item.outcome,
        report=item.report,
        severity=decided.get("severity"),
        route=decided.get("route"),
        mode=decided.get("mode"),
        reasons=tuple(decided.get("reasons", ())),
        gap_id=item.gap_id,
        rejected=item.rejected,
        judge=item.ai_judge if decided.get("mode") == "ai" else item.judge,
        gates=dict(decided.get("gates") or {}),
        confidence=decided.get("confidence"),
    )


def _count_ai(valve: Valve, payload: Mapping[str, Any]) -> None:
    """Count a stored site's AI answer in the valve of a resumed run, as its first attempt did."""

    routed, report = payload.get("routed") or {}, payload["checked"].get("report")
    if routed.get("route") == "ai" and routed.get("mode") == "ai" and report is not None:
        valve.answered += 1
        valve.nodes |= touched(GapReport.model_validate(report))


def _usage(items: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """The model calls and tokens of the sites' checks, per gateway role."""

    total: dict[str, Counter[str]] = {"calls": Counter(), "tokens": Counter()}
    for item in items:
        for name, counter in total.items():
            counter.update(item.get(name, {}))
    return {name: dict(counter) for name, counter in total.items()}


def _cost(
    relevance: Mapping[str, Any], results: Sequence[InvestigationResult], checked: Mapping[str, Any]
) -> dict[str, dict[str, int]]:
    """Calls and tokens per gateway role: the relevance step's calls, the investigations', and the sites' checks' repair
    (``gap_investigate``) and second-check (``gap_judge``) calls; ``gap_edits`` is a service call, not a run's."""

    cost: dict[str, dict[str, int]] = {role: {"calls": 0, "tokens": 0} for role in GATEWAY_ROLES}
    cost[RELEVANCE_ROLE] = {"calls": int(relevance["model_calls"]), "tokens": int(relevance["tokens"])}
    cost[INVESTIGATE_ROLE] = {
        "calls": sum(result.model_calls for result in results),
        "tokens": sum(result.tokens for result in results),
    }
    for role in GATEWAY_ROLES:
        cost[role]["calls"] += int(checked.get("calls", {}).get(role, 0))
        cost[role]["tokens"] += int(checked.get("tokens", {}).get(role, 0))
    return cost


def _with_not_spec(noise: NoiseReport, extra: Sequence[Mapping[str, Any]]) -> NoiseReport:
    """The noise report with the nodes of ``not_spec`` sites (FR-GREL-07): their section turns ``mixed`` (by model)
    and lists them as removed."""

    if not extra:
        return noise
    sections = {(item.lineage, item.heading_path): item for item in noise.sections}
    for entry in extra:
        key = (entry["lineage"], tuple(entry["heading_path"]))
        current = sections.get(key)
        removed = tuple(sorted({*(current.removed if current else ()), *entry["spec_ids"]}))
        if current is None:
            sections[key] = NoiseSection(
                lineage=key[0],
                heading_path=key[1],
                relevance="mixed",
                signals=("not_spec",),
                by="model",
                removed=removed,
            )
        else:
            sections[key] = current.model_copy(update={"removed": removed})
    return NoiseReport(sections=tuple(sections[key] for key in sorted(sections)))


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
            if not found.finished and not set(found.progress) <= set(RUN_STEPS):
                found = await _investigate_again(store, found)
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


async def _investigate_again(store: GapRunStore, record: GapRunRecord) -> GapRunRecord:
    """An unfinished run an earlier version stored, with the steps ``check``, ``route`` and ``record`` after
    ``investigate``: its sites are investigated again and recorded one by one."""

    await store.put_progress(record.project_id, record.run_id, "investigate", "pending")
    return record.model_copy(update={"progress": {**record.progress, "investigate": "pending"}})


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
        for step in RUN_STEPS:
            await run.execute(step)
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
            existing=[existing_gap(gap) for gap in gaps if gap.signature],
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
    calls[INVESTIGATE_ROLE] = investigate * (limits.rounds_per_site + 1)
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


def _origin(site: Site) -> str:
    """The origin of a site's gap: ``person`` or ``development`` for a raised gap's own seed, else ``analysis``."""

    detector = min((seed.detector for seed in site.seeds), default="")
    return detector if detector in RAISED_ORIGINS else "analysis"


def existing_gap(gap: StoredGap) -> ExistingGap:
    """A stored gap as the sites step knows it (a raised gap with its question, kind and quoted lines)."""

    return ExistingGap(
        gap.gap_id,
        gap.signature,
        gap.status,
        gap.about_ids,
        gap.origin,
        kind=str(gap.props.get("kind") or ""),
        question=str(gap.props.get("question") or ""),
        witness=tuple(
            Witness(fragment_id=cite.fragment_id, quote=cite.quote, line_start=cite.line_start, line_end=cite.line_end)
            for cite in gap.cites
        ),
        seeds=tuple(str(item) for item in gap.props.get("seed_signatures") or ()),
        route=str(gap.props.get("route") or "ask"),
    )
