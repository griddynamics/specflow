"""Step ``close``: the close check after a build (docs/graph/gap/lifecycle.md "Close check", "Follow-ups and
history", "Gap transitions"; FR-GCLS-01 to FR-GCLS-06).

The step runs after ``scope`` (which moved ``ABOUT`` and ``IMPACTS`` of rekeyed and merged nodes, FR-GCLS-04) in every
``update`` and ``full`` run, and reads the graph as the build left it.

1. **Applying gaps.** Each gap with status ``applying`` has one clarification whose answers were sent to an update
   build (``pending`` with ``apply_requested_at``).
   - *Grounding*: an ``amend`` clarification is grounded when every one of its ``active`` amendments' texts (the
     answer marker left out, whitespace and case normalised) is in an active fragment of the amendment's lineage; a
     ``document`` clarification when a fragment of the clarifications document holds its text (the fragment of its
     own section first). ``GROUNDED_IN`` goes to each such fragment with the build id; a ``document`` clarification is
     also ``PLACED_IN`` the clarifications document (GI4).
   - *Re-check*: the gap's detector runs again over the graph (every section read as ``spec``: the gap was relevant
     when it was found) and still fires when one of its seeds has a signature the gap keeps (``seed_signatures``), is
     about a node of the detector's types the gap is ``ABOUT``, or quotes a line the gap cites; with none of these to
     compare (a project-wide checklist gap without seeds kept) it cannot tell and the gap is left as it is. A
     detector that reads build facts (``refused_merge``, ``cycle``) still fires while two of the gap's nodes of its
     types are left (the refused pair or the cycle is gone otherwise).
     Gaps a person or a coding agent raised have no detector: they are fixed when grounded.
   - *Outcome*: grounded and fixed: the gap is ``resolved`` (resolution ``answered``, or ``ai_answered`` for an AI
     answer) and the clarification ``applied`` with ``applied_build_id``. Grounded and not fixed: the clarification is
     ``applied``, the gap ``resolved`` with resolution ``follow_up``, and a new ``open`` gap ``FOLLOWS_UP`` it with
     reason ``not_fixed``. Not grounded: the gap waits one more build (event ``not_grounded``); the second time it
     follows up the same way (its clarification stays ``pending``).
2. **Obsolete gaps.** An ``open`` gap that nobody answered leaves the graph (event ``obsolete``, options removed) when
   it is about no node any more and cites no line that holds, or when its detector no longer fires on it
   (FR-GCLS-03).
3. **Stale witnesses.** A ``CITES`` relation whose fragment is no longer active, or no longer holds the quote, is
   deleted; the quote stays in the event log (event ``cite_removed``).

A follow-up gap starts from the old gap's question, kind, severity, origin, detector and ``ABOUT``/``IMPACTS`` nodes;
its signature is derived from the old gap's (so it is a new gap with a new id), and the run's ``sites`` step gives it
a site so it is investigated in the same run for new options (lifecycle.md "Follow-ups and history"). No model is
called here. Held tasks of resolved gaps are released by the blocker-hold wiring of development gaps (not here yet).
Every transition is a ``gap_event`` row with the run as actor (FR-GCLS-06); logs carry ids and counts only.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from graph.core.ontology import Ontology
from graph.core.ontology.compiled import BUILD_OWNER
from graph.core.writer import DOCUMENT, FRAGMENT, NodeRef, OwnedWriter
from graph.gap.answers import marker
from graph.gap.corpus import APPLY_MARK, clarifications_lineage
from graph.gap.gaps import StoredGap, answer_key, cite_key, clarification_uuid, gap_uuid, link_key, quote_hash
from graph.gap.identity import gap_id, normalise_quote, sha256_hex
from graph.gap.models import BuildFacts, EventRow
from graph.gap.names import (
    ABOUT,
    CITES,
    CLARIFICATION,
    FOLLOWS_UP,
    GAP,
    GROUNDED_IN,
    OWNER,
    PLACED_IN,
    RUN_ACTOR,
)
from graph.gap.ports import Amendments, GapRecords
from graph.gap.resolve import resolve_spec_ids
from graph.gap.rules import GapRules
from graph.gap.steps.detect import Seed, detect
from graph.gap.steps.reconciled import (
    ABSORBED,
    CONFLICT,
    RECONCILE_REASONS,
    RECONCILE_STATE,
    FollowUpSpec,
    ReconciledOutcome,
    absorbed_grounds,
    amendments_state,
    conflict_follow_up,
    continues,
    orphaned_follow_up,
)
from graph.gap.steps.relevance import RelevanceOutcome, cue_pattern
from graph.gap.steps.snapshot import GraphSnapshot, heading_path_of
from logger.logger import INFO, get_logger, log_event

logger = get_logger(__name__)

APPLYING: Final = "applying"
OPEN: Final = "open"
RESOLVED: Final = "resolved"
PENDING: Final = "pending"
APPLIED: Final = "applied"
APPLIED_ROWS: Final = frozenset({"active", "undecided"})
"""Amendment statuses the corpus reader applies (reconcile.md "Edge cases": an undecided one stays applied)."""
NOT_FIXED: Final = "not_fixed"
NOT_GROUNDED: Final = "not_grounded"
RAISED_ORIGINS: Final = frozenset({"person", "development"})
BUILD_FACT_RULES: Final = frozenset({"refused_merge", "cycle"})
"""Detector rules that read the build's facts (dedup refusals, cycle cuts); only the build that made them lists them."""
PAIR: Final = 2
NO: Final = "no"
WAITING_REASONS: Final = RECONCILE_REASONS | {"reverted"}
"""Follow-ups that wait for a person and never become obsolete: reconcile's, and those of a reverted answer (the text
a revert removes may still be in the graph until its build)."""


@dataclass(frozen=True)
class CloseRun:
    """What the close step needs of the run: the stores, the ontology and rules, the ids, the build and the clock."""

    source: Any
    executor: Any
    ontology: Ontology
    rules: GapRules
    records: GapRecords
    amendments: Amendments
    project_id: str
    run_id: str
    now: Callable[[], datetime]
    build_id: str | None = None
    build: BuildFacts = field(default_factory=BuildFacts)
    database: str | None = None


@dataclass
class CloseOutcome:
    """What the close check did, by gap id."""

    resolved: list[str] = field(default_factory=list)
    follow_ups: dict[str, str] = field(default_factory=dict)
    """New follow-up gap id to the gap it continues."""
    waiting: list[str] = field(default_factory=list)
    obsolete: list[str] = field(default_factory=list)
    cites_removed: int = 0
    reconciled: ReconciledOutcome = field(default_factory=ReconciledOutcome)
    """Answers reconcile changed (``steps.reconciled``); their conflict and orphaned follow-ups are not in
    ``follow_ups`` (they get fixed options, not a site)."""

    def payload(self) -> dict[str, Any]:
        return {
            "skipped": False,
            "resolved": sorted(self.resolved),
            "follow_ups": dict(sorted(self.follow_ups.items())),
            "waiting": sorted(self.waiting),
            "obsolete": sorted(self.obsolete),
            "cites_removed": self.cites_removed,
            "reconciled": self.reconciled.payload(),
        }


def _plain(text: str) -> str:
    return normalise_quote(text).casefold()


def answer_text(text: str, gap: str) -> str:
    """An amended or document text without its answer marker line, normalised for a containment check."""

    kept = [line for line in text.split("\n") if normalise_quote(line) != marker(gap)]
    return _plain("\n".join(kept))


def still_fires(gap: StoredGap, seeds: Sequence[Seed], snapshot: GraphSnapshot, rules: GapRules) -> bool | None:
    """Whether the gap's detector still fires on it (module docstring); None when the gap has no detector of the
    rules (raised gaps, or a detector the rules dropped) or nothing to compare a seed with."""

    detector = str(gap.props.get("detector") or "")
    rule = rules.detectors.get(detector)
    if rule is None or gap.origin in RAISED_ORIGINS:
        return None
    labels = set(rule.labels)
    about = {
        spec_id
        for spec_id in gap.about_ids
        if spec_id in snapshot.nodes and (not labels or snapshot.nodes[spec_id].label in labels)
    }
    if rule.rule in BUILD_FACT_RULES:
        return len(about) >= PAIR
    quotes = {_plain(cite.quote) for cite in gap.cites if cite.quote}
    signatures = {str(item) for item in gap.props.get("seed_signatures") or ()}
    for seed in seeds:
        if seed.detector != detector:
            continue
        if seed.signature in signatures or about & set(seed.about):
            return True
        if any(item.quote and _plain(item.quote) in quotes for item in seed.witness):
            return True
    if not (about or quotes or signatures):
        return None  # a gap about no node of the detector's types, with no quote and no seed to compare: unknown
    return False


def holds(snapshot: GraphSnapshot, fragment_id: str, quote: str) -> bool:
    fragment = snapshot.fragments.get(fragment_id)
    return fragment is not None and bool(quote) and _plain(quote) in _plain(fragment.text)


def follow_up_signature(gap: StoredGap, reason: str) -> str:
    """The signature of a follow-up gap: the old gap's signature, its id and the reason (a new gap, never the old)."""

    return sha256_hex({"follows_up": gap.gap_id, "reason": reason, "signature": gap.signature})


class Closer:
    """The close check of one run over the gaps it read (module docstring)."""

    def __init__(self, run: CloseRun, snapshot: GraphSnapshot, gaps: Sequence[StoredGap]) -> None:
        self.run = run
        self.snapshot = snapshot
        self.gaps = list(gaps)
        self.taken: dict[str, str] = {gap.gap_id: gap.signature for gap in gaps}
        self.outcome = CloseOutcome()
        self.events: list[EventRow] = []
        self._seeds: tuple[Seed, ...] | None = None

    def seeds(self) -> tuple[Seed, ...]:
        """The detectors' seeds over the graph as it is now, with every section read as ``spec``."""

        if self._seeds is None:
            relevance = RelevanceOutcome(sections={}, cue_pattern=cue_pattern(self.run.rules.relevance.obligation_cues))
            found = detect(
                self.snapshot,
                rules=self.run.rules,
                relevance=relevance,
                build=self.run.build,
                project_id=self.run.project_id,
            )
            self._seeds = found.seeds
        return self._seeds

    def _writer(self) -> OwnedWriter:
        return OwnedWriter(self.run.source, self.run.ontology, OWNER, self.run.project_id, database=self.run.database)

    def _event(self, gap: str, event: str, *, clr: str | None = None, **detail: Any) -> None:
        self.events.append(
            EventRow(
                gap_id=gap,
                clr_id=clr,
                at=self.run.now(),
                actor=RUN_ACTOR,
                event=event,
                detail={"run_id": self.run.run_id, **detail},
            )
        )

    async def close(self) -> CloseOutcome:
        gone: set[str] = set()
        for gap in self.gaps:
            if gap.status == APPLYING:
                await self._applying(gap)
            elif gap.status == RESOLVED:
                await self._reconciled(gap)
            elif gap.status == OPEN and await self._obsolete(gap):
                gone.add(gap.gap_id)
        await self._stale_cites([gap for gap in self.gaps if gap.gap_id not in gone])
        if self.events:
            await self.run.records.log(self.run.project_id, self.events)
        log_event(
            logger,
            INFO,
            "graph.gap.close.done",
            project_id=self.run.project_id,
            run_id=self.run.run_id,
            resolved=len(self.outcome.resolved),
            follow_ups=len(self.outcome.follow_ups),
            waiting=len(self.outcome.waiting),
            obsolete=len(self.outcome.obsolete),
            cites_removed=self.outcome.cites_removed,
        )
        return self.outcome

    # Applying gaps

    async def _applying(self, gap: StoredGap) -> None:
        current = [props for props in gap.clarifications if props.get("status") == PENDING and props.get(APPLY_MARK)]
        if not current:
            return
        props = max(current, key=lambda item: str(item.get(APPLY_MARK)))
        clr = str(props["clr_id"])
        if props.get("placement") == "amend" and await self._reconciled_on_apply(gap, props):
            return
        grounds = await self._grounds(gap, props)
        fires = still_fires(gap, self.seeds(), self.snapshot, self.run.rules)
        if not grounds:
            earlier = [
                event
                for event in await self.run.records.events(self.run.project_id, gap.gap_id)
                if event.event == NOT_GROUNDED and event.clr_id == clr
            ]
            if not earlier:
                self.outcome.waiting.append(gap.gap_id)
                self._event(gap.gap_id, NOT_GROUNDED, clr=clr, build_id=self.run.build_id)
                return
            await self._resolve(gap, props, grounds, follow_up=True)
            return
        await self._resolve(gap, props, grounds, follow_up=bool(fires))

    async def _grounds(self, gap: StoredGap, props: Mapping[str, Any]) -> tuple[str, ...]:
        """The fragments that hold the clarification's text now, or none when any part of it is missing."""

        clr = str(props["clr_id"])
        if props.get("placement") == "document":
            lineage = clarifications_lineage(self.run.project_id)
            wanted = answer_text(str(props.get("text") or ""), gap.gap_id)
            own = f"({gap.gap_id})".casefold()
            candidates = sorted(
                (fragment for fragment in self.snapshot.fragments.values() if fragment.document_id == lineage),
                key=lambda fragment: own not in " ".join(heading_path_of(fragment)).casefold(),
            )
            found = next((item.fragment_id for item in candidates if wanted and wanted in _plain(item.text)), None)
            return () if found is None else (found,)
        rows = await self.run.amendments.for_clarification(self.run.project_id, clr)
        active = [row for row in rows if row.status in APPLIED_ROWS]
        if not active:
            return ()
        grounds: list[str] = []
        for row in active:
            wanted = answer_text(row.text, gap.gap_id)
            found = next(
                (
                    fragment.fragment_id
                    for fragment in self.snapshot.fragments.values()
                    if fragment.document_id == row.lineage and wanted and wanted in _plain(fragment.text)
                ),
                None,
            )
            if found is None:
                return ()
            grounds.append(found)
        return tuple(dict.fromkeys(grounds))

    async def _resolve(
        self, gap: StoredGap, props: Mapping[str, Any], grounds: Sequence[str], *, follow_up: bool
    ) -> None:
        run = self.run
        clr = str(props["clr_id"])
        now = run.now().isoformat(timespec="milliseconds")
        resolution = "follow_up" if follow_up else ("ai_answered" if props.get("mode") == "ai" else "answered")
        build: dict[str, Any] = {} if run.build_id is None else {"build_id": run.build_id}
        new: str | None = None
        async with self._writer().transaction() as tx:
            await tx.upsert_node(
                GAP,
                gap_uuid(run.project_id, gap.gap_id),
                {"status": RESOLVED, "resolution": resolution, "resolved_at": now, "updated_at": now},
            )
            node = NodeRef(CLARIFICATION, clarification_uuid(run.project_id, clr))
            if grounds:
                changed: dict[str, Any] = {"status": APPLIED}
                if run.build_id is not None:
                    changed["applied_build_id"] = run.build_id
                if props.get("placement") == "amend":
                    rows = await run.amendments.for_clarification(run.project_id, clr)
                    changed[RECONCILE_STATE] = amendments_state(rows)
                await tx.upsert_node(CLARIFICATION, node.key, changed)
                for fragment in grounds:
                    await tx.upsert_relationship(
                        GROUNDED_IN, node, NodeRef(FRAGMENT, fragment), answer_key(clr, GROUNDED_IN, fragment), build
                    )
                lineage = clarifications_lineage(run.project_id)
                if props.get("placement") == "document" and lineage in self.snapshot.documents:
                    await tx.upsert_relationship(
                        PLACED_IN, node, NodeRef(DOCUMENT, lineage), answer_key(clr, PLACED_IN, lineage), {}
                    )
            if follow_up:
                new = await self._follow_up(tx, gap, now)
        self.outcome.resolved.append(gap.gap_id)
        if grounds:
            self._event(gap.gap_id, APPLIED, clr=clr, build_id=run.build_id, grounded=len(grounds))
        self._event(gap.gap_id, RESOLVED, clr=clr, resolution=resolution, build_id=run.build_id)
        if new is not None:
            self.outcome.follow_ups[new] = gap.gap_id
            self._event(new, "created", follows_up=gap.gap_id, reason=NOT_FIXED, about=len(gap.about_ids))

    async def _follow_up(self, tx: OwnedWriter, gap: StoredGap, now: str, spec: FollowUpSpec | None = None) -> str:
        """A new open gap ``FOLLOWS_UP`` the old one: reason ``not_fixed`` from the old gap's question and nodes, or the
        reconcile follow-up ``spec`` (its own question, nodes and cites, no detector: it is never obsolete)."""

        run = self.run
        reason = NOT_FIXED if spec is None else spec.reason
        signature = follow_up_signature(gap, reason)
        new = gap_id(signature, self.taken)
        self.taken[new] = signature
        names = ("kind", "severity", "origin", "title", "question", "why", "detector")
        props = {name: gap.props[name] for name in names if gap.props.get(name) is not None}
        if spec is not None:
            props.pop("detector", None)
            props |= dict(spec.props)
        props |= {
            "gap_id": new,
            "status": OPEN,
            "route": "ask",
            "signature": signature,
            "blocking": NO,
            "held_back": NO,
            "run_id": run.run_id,
            "created_by": RUN_ACTOR,
            "created_at": now,
            "updated_at": now,
        }
        owner = NodeRef(GAP, gap_uuid(run.project_id, new))
        await tx.upsert_node(GAP, owner.key, props)
        old = NodeRef(GAP, gap_uuid(run.project_id, gap.gap_id))
        await tx.upsert_relationship(FOLLOWS_UP, owner, old, link_key(new, FOLLOWS_UP, gap.gap_id), {"reason": reason})
        if spec is not None:
            await self._spec_links(tx, owner, new, spec)
            return new
        for link in (*gap.about, *gap.impacts):
            if link.spec_id in self.snapshot.nodes and link.uuid and link.label:
                await tx.upsert_relationship(
                    link.relation,
                    owner,
                    NodeRef(link.label, link.uuid),
                    link_key(new, link.relation, link.spec_id),
                    {"added_by_run": run.run_id},
                )
        for cite in gap.cites:
            if holds(self.snapshot, cite.fragment_id, cite.quote):
                digest = quote_hash(cite.quote)
                cited: dict[str, Any] = {"quote": cite.quote, "quote_hash": digest, "added_by_run": run.run_id}
                if cite.line_start is not None:
                    cited |= {"line_start": cite.line_start, "line_end": cite.line_end or cite.line_start}
                await tx.upsert_relationship(
                    CITES, owner, NodeRef(FRAGMENT, cite.fragment_id), cite_key(new, cite.fragment_id, digest), cited
                )
        return new

    async def _spec_links(self, tx: OwnedWriter, owner: NodeRef, new: str, spec: FollowUpSpec) -> None:
        run = self.run
        labels = [label for label in run.ontology.labels if run.ontology.owner_of(label) == BUILD_OWNER]
        refs = await resolve_spec_ids(run.executor, run.project_id, sorted(spec.about), labels)
        for ref in sorted({ref.spec_id: ref for ref in refs.values()}.values(), key=lambda ref: ref.spec_id):
            if run.ontology.is_legal(ABOUT, GAP, ref.label):
                await tx.upsert_relationship(
                    ABOUT,
                    owner,
                    NodeRef(ref.label, ref.uuid),
                    link_key(new, ABOUT, ref.spec_id),
                    {"added_by_run": run.run_id},
                )
        for fragment_id, quote in spec.cites:
            digest = quote_hash(quote)
            await tx.upsert_relationship(
                CITES,
                owner,
                NodeRef(FRAGMENT, fragment_id),
                cite_key(new, fragment_id, digest),
                {"quote": quote, "quote_hash": digest, "added_by_run": run.run_id},
            )

    # Reconciled answers

    async def _reconciled(self, gap: StoredGap) -> None:
        """A resolved gap whose applied ``amend`` clarification reconcile changed (``steps.reconciled``)."""

        current = [
            props
            for props in gap.clarifications
            if props.get("status") == APPLIED and props.get("placement") == "amend"
        ]
        if not current:
            return
        props = max(current, key=lambda item: str(item.get("answered_at") or ""))
        clr = str(props["clr_id"])
        rows = list(await self.run.amendments.for_clarification(self.run.project_id, clr))
        state = amendments_state(rows)
        if not rows or props.get(RECONCILE_STATE) == state:
            return
        statuses = {row.status for row in rows}
        if "suspended" in statuses:
            await self._reconcile_follow_up(gap, clr, state, conflict_follow_up(gap, props, rows, self.snapshot))
        elif "orphaned" in statuses:
            await self._reconcile_follow_up(gap, clr, state, orphaned_follow_up(gap, props, rows, self.snapshot))
        elif statuses == {"retired"}:
            await self._absorbed(gap, clr, state, rows)
        elif statuses & APPLIED_ROWS:
            await self._recheck(gap, props, state)
        else:
            await self._clarification(clr, {RECONCILE_STATE: state})

    async def _clarification(self, clr: str, changed: Mapping[str, Any], grounds: Sequence[str] = ()) -> None:
        run = self.run
        node = NodeRef(CLARIFICATION, clarification_uuid(run.project_id, clr))
        build: dict[str, Any] = {} if run.build_id is None else {"build_id": run.build_id}
        async with self._writer().transaction() as tx:
            await tx.upsert_node(CLARIFICATION, node.key, dict(changed))
            for fragment in grounds:
                await tx.upsert_relationship(
                    GROUNDED_IN, node, NodeRef(FRAGMENT, fragment), answer_key(clr, GROUNDED_IN, fragment), build
                )

    async def _reconciled_on_apply(self, gap: StoredGap, props: Mapping[str, Any]) -> bool:
        """An answer an upload met in the build that applied it: reconcile's outcome decides, as for an applied answer
        (a conflict or orphaned follow-up, or absorbed); the gap resolves. False when its rows are still applied."""

        clr = str(props["clr_id"])
        rows = list(await self.run.amendments.for_clarification(self.run.project_id, clr))
        statuses = {row.status for row in rows}
        state = amendments_state(rows)
        if "suspended" in statuses:
            spec = conflict_follow_up(gap, props, rows, self.snapshot)
        elif "orphaned" in statuses:
            spec = orphaned_follow_up(gap, props, rows, self.snapshot)
        elif rows and statuses == {"retired"}:
            await self._absorbed(gap, clr, state, rows, resolve=True)
            return True
        else:
            return False
        await self._reconcile_follow_up(gap, clr, state, spec, resolve=True)
        return True

    async def _reconcile_follow_up(
        self, gap: StoredGap, clr: str, state: str, spec: FollowUpSpec, *, resolve: bool = False
    ) -> None:
        run = self.run
        now = run.now().isoformat(timespec="milliseconds")
        node = clarification_uuid(run.project_id, clr)
        changed: dict[str, Any] = {"updated_at": now}
        if resolve:  # an applying gap: the answer never held, the follow-up takes over
            changed |= {"status": RESOLVED, "resolution": "follow_up", "resolved_at": now}
        async with self._writer().transaction() as tx:
            await tx.upsert_node(CLARIFICATION, node, {"status": spec.reason, RECONCILE_STATE: state})
            await tx.upsert_node(GAP, gap_uuid(run.project_id, gap.gap_id), changed)
            new = await self._follow_up(tx, gap, now, spec)
        options = [
            option.model_copy(update={"gap_id": new, "rank": rank, "created_run": run.run_id})
            for rank, option in enumerate(spec.options, start=1)
        ]
        try:
            await run.records.put_options(run.project_id, new, options)
        except BaseException:  # the next run meets the answer again and writes the same follow-up with its options
            async with self._writer().transaction() as tx:
                await tx.upsert_node(
                    CLARIFICATION, node, {"status": APPLIED if not resolve else PENDING, RECONCILE_STATE: ""}
                )
            raise
        if resolve:
            self.outcome.resolved.append(gap.gap_id)
        outcome = self.outcome.reconciled
        (outcome.conflict if spec.reason == CONFLICT else outcome.orphaned).append(gap.gap_id)
        outcome.follow_ups[new] = gap.gap_id
        self._event(gap.gap_id, spec.reason, clr=clr, follows_up=new)
        self._event(new, "created", follows_up=gap.gap_id, reason=spec.reason, about=len(spec.about))

    async def _absorbed(
        self, gap: StoredGap, clr: str, state: str, rows: Sequence[Any], *, resolve: bool = False
    ) -> None:
        grounds = absorbed_grounds(gap, rows, self.snapshot)
        changed: dict[str, Any] = {"status": ABSORBED, RECONCILE_STATE: state}
        await self._clarification(clr, changed, grounds)
        if resolve:  # the owner's upload already says what the answer said
            now = self.run.now().isoformat(timespec="milliseconds")
            async with self._writer().transaction() as tx:
                resolved = {"status": RESOLVED, "resolution": "answered", "resolved_at": now, "updated_at": now}
                await tx.upsert_node(GAP, gap_uuid(self.run.project_id, gap.gap_id), resolved)
            self.outcome.resolved.append(gap.gap_id)
        self.outcome.reconciled.absorbed.append(gap.gap_id)
        self._event(gap.gap_id, ABSORBED, clr=clr, build_id=self.run.build_id, grounded=len(grounds))

    async def _recheck(self, gap: StoredGap, props: Mapping[str, Any], state: str) -> None:
        """Applied again, re-anchored or undecided: grounded again, and a follow-up when the detector fires again."""

        clr = str(props["clr_id"])
        grounds = await self._grounds(gap, props)
        fires = still_fires(gap, self.seeds(), self.snapshot, self.run.rules) if grounds else None
        followed = any(item.direction == "continued_by" and item.reason == NOT_FIXED for item in gap.follow_ups)
        await self._clarification(clr, {RECONCILE_STATE: state}, grounds)
        self.outcome.reconciled.rechecked.append(gap.gap_id)
        self._event(gap.gap_id, "rechecked", clr=clr, build_id=self.run.build_id, grounded=len(grounds))
        if fires and not followed:
            now = self.run.now().isoformat(timespec="milliseconds")
            async with self._writer().transaction() as tx:
                await tx.upsert_node(
                    GAP, gap_uuid(self.run.project_id, gap.gap_id), {"resolution": "follow_up", "updated_at": now}
                )
                new = await self._follow_up(tx, gap, now)
            self.outcome.follow_ups[new] = gap.gap_id
            self._event(new, "created", follows_up=gap.gap_id, reason=NOT_FIXED, about=len(gap.about_ids))

    # Open gaps

    async def _obsolete(self, gap: StoredGap) -> bool:
        if continues(gap, WAITING_REASONS) is not None:  # a reconcile or revert follow-up waits for a person
            return False
        about = [spec_id for spec_id in gap.about_ids if spec_id in self.snapshot.nodes]
        cited = [cite for cite in gap.cites if holds(self.snapshot, cite.fragment_id, cite.quote)]
        fires = still_fires(gap, self.seeds(), self.snapshot, self.run.rules)
        if (about or cited) and fires is not False:
            return False
        reason = "detector_silent" if (about or cited) else "nodes_gone"
        async with self._writer().transaction() as tx:
            await tx.delete_node(GAP, gap_uuid(self.run.project_id, gap.gap_id))
        await self.run.records.put_options(self.run.project_id, gap.gap_id, ())
        self.outcome.obsolete.append(gap.gap_id)
        self._event(gap.gap_id, "obsolete", reason=reason, detector=gap.props.get("detector"))
        return True

    # Witnesses

    async def _stale_cites(self, gaps: Collection[StoredGap]) -> None:
        stale = [
            (gap, cite) for gap in gaps for cite in gap.cites if not holds(self.snapshot, cite.fragment_id, cite.quote)
        ]
        if not stale:
            return
        async with self._writer().transaction() as tx:
            for _, cite in stale:
                await tx.delete_relationship(CITES, cite.edge_key)
        for gap, cite in stale:
            self.outcome.cites_removed += 1
            self._event(gap.gap_id, "cite_removed", fragment_id=cite.fragment_id, quote=cite.quote)


async def close_gaps(run: CloseRun, snapshot: GraphSnapshot, gaps: Sequence[StoredGap]) -> CloseOutcome:
    """The close check of one run (module docstring)."""

    return await Closer(run, snapshot, gaps).close()
