"""Step ``sites``: corroborate, suppress, group, rank and limit the seeds (docs/graph/gap/detection.md "Seeds and
corroboration", "Sites"; FR-GDET-06 to FR-GDET-09).

1. **Suppression**: a seed whose signature is in ``gap_suppression`` is dropped and counted (FR-GDET-07).
2. **Corroboration**: a strong seed always counts; a weak seed needs a signal of ``seeds.corroboration``
   (``obligation_wording``, ``names_system_part``, ``planned_dependency``); a weak seed from a ``mixed`` section
   needs ``obligation_wording`` and one more (FR-GDET-06). Uncorroborated seeds are kept with ``corroborated``
   false and never investigated.
3. **Grouping**: seeds sharing an ``ABOUT`` candidate merge; seeds whose candidates have the same owning modules and
   the same kind merge when their witnesses lie in one section; stated questions asking the same question merge;
   seeds matching an existing gap's signature join that gap's site. A site holds at most 12 seeds; a larger group
   splits by section (FR-GDET-08, FR-GDET-09).
4. **Ranking**: strength weight (strong 2, weak 1) x the kind's severity prior x reach; reach is 1 + the nodes trace
   reaches from the candidates (``affects``, depth 2) + 2 x the plan tasks implementing them (``PlanView``).
5. **Known sites**: a site whose input hash has a cached verdict is ``skipped``; the others are investigated in
   rank order (sites deferred by the last run first) up to the mode's limit; the rest are ``deferred``.

The step writes every seed (with its site) and the ``skipped`` and ``deferred`` site rows, plus the info notes of
detection; the sites to investigate are returned and written by the steps after investigation.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from graph.core.reader import GraphNode
from graph.gap.identity import canonical_json, sha256_hex
from graph.gap.models import SeedRow, SiteRow, VerdictRow
from graph.gap.names import RunMode
from graph.gap.ports import GapRecords, PlanView
from graph.gap.rules import GapRules
from graph.gap.settings import GapLimits
from graph.gap.steps.detect import DetectOutcome, Seed, question_text, same_question, witness_section
from graph.gap.steps.relevance import MIXED, cue_pattern, has_cue
from graph.gap.steps.snapshot import GraphSnapshot
from logger.logger import INFO, get_logger, log_event

logger = get_logger(__name__)

SITE_PREFIX: Final = "SITE::"
SITE_HEX: Final = 12
MAX_SEEDS: Final = 12
REACH_DEPTH: Final = 2
TRACE_IDS: Final = 50
STRENGTH_WEIGHT: Final[Mapping[str, float]] = {"strong": 2.0, "weak": 1.0}
OBLIGATION: Final = "obligation_wording"
SYSTEM_PART: Final = "names_system_part"
PLANNED: Final = "planned_dependency"
MIXED_SIGNALS: Final = 2
"""A weak seed from a mixed section needs obligation wording and one more signal."""
SiteState = Literal["investigate", "skipped", "deferred"]


@dataclass(frozen=True)
class ExistingGap:
    """A gap already in the graph, as the run engine reads it: its signature, status and ``ABOUT`` nodes. Open and
    dismissed gaps on a site's nodes are part of its input hash; a seed with the gap's signature joins its site."""

    gap_id: str
    signature: str
    status: str
    about: tuple[str, ...] = ()


@dataclass(frozen=True)
class Site:
    """One entry point of an investigation."""

    site_id: str
    seeds: tuple[Seed, ...]
    about: tuple[str, ...]
    context: tuple[str, ...]
    input_hash: str
    rank_score: float
    reach: int
    state: SiteState = "investigate"
    gap_id: str | None = None
    verdict: VerdictRow | None = None

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted({seed.kind for seed in self.seeds}))


@dataclass(frozen=True)
class SitesOutcome:
    """The sites of a run by state, the seed rows written and the metrics of the step."""

    sites: tuple[Site, ...]
    seed_rows: tuple[SeedRow, ...] = ()
    counts: Mapping[str, Any] = field(default_factory=dict)

    @property
    def investigate(self) -> tuple[Site, ...]:
        return tuple(site for site in self.sites if site.state == "investigate")

    @property
    def skipped(self) -> tuple[Site, ...]:
        return tuple(site for site in self.sites if site.state == "skipped")

    @property
    def deferred(self) -> tuple[Site, ...]:
        return tuple(site for site in self.sites if site.state == "deferred")


@dataclass(frozen=True)
class SystemParts:
    """The names of the system parts (``seeds.system_parts``) as one pattern, and the nodes each name belongs to."""

    pattern: re.Pattern[str] | None
    owners: Mapping[str, frozenset[str]]

    @classmethod
    def of(cls, snapshot: GraphSnapshot, rules: GapRules) -> SystemParts:
        labels = set(rules.system_parts)
        owners: dict[str, set[str]] = defaultdict(set)
        for node in snapshot.nodes.values():
            if node.label in labels and node.name.strip():
                owners[node.name.casefold()].add(node.spec_id)
        names = sorted(owners)
        pattern = cue_pattern([*names, *(f"{name}s" for name in names)]) if names else None
        return cls(pattern, {name: frozenset(ids) for name, ids in owners.items()})

    def named(self, text: str, *, besides: str) -> bool:
        """True when ``text`` names a system part other than the node ``besides`` (a plural ``s`` allowed)."""

        if self.pattern is None:
            return False
        for match in self.pattern.finditer(text):
            name = match.group(0).casefold()
            ids = self.owners.get(name) or self.owners.get(name[:-1], frozenset())
            if ids - {besides}:
                return True
        return False


def corroboration(
    snapshot: GraphSnapshot,
    seed: Seed,
    *,
    rules: GapRules,
    planned: Collection[str] = (),
    parts: SystemParts | None = None,
) -> tuple[str, ...]:
    """The corroboration signals of ``rules`` that hold for the seed (detection.md "Corroboration signals")."""

    nodes = [snapshot.nodes[spec_id] for spec_id in seed.about if spec_id in snapshot.nodes]
    texts = [(node, snapshot.text(node)) for node in nodes]
    found: list[str] = []
    cues = cue_pattern(rules.relevance.obligation_cues)
    if OBLIGATION in rules.corroboration and any(has_cue(cues, text) for _, text in texts):
        found.append(OBLIGATION)
    names = parts or SystemParts.of(snapshot, rules)
    if SYSTEM_PART in rules.corroboration and any(names.named(text, besides=node.spec_id) for node, text in texts):
        found.append(SYSTEM_PART)
    planned_ids = set(planned)
    if PLANNED in rules.corroboration and any(
        spec_id in planned_ids or snapshot.related(spec_id) & planned_ids for spec_id in seed.about
    ):
        found.append(PLANNED)
    return tuple(found)


class _Union:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def join(self, first: int, second: int) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def group_seeds(
    snapshot: GraphSnapshot, seeds: Sequence[Seed], *, existing: Sequence[ExistingGap] = ()
) -> list[tuple[list[Seed], str | None]]:
    """The groups of seeds that form sites, each with the existing gap it joins (None for a new one)."""

    union = _Union(len(seeds))
    first_of: dict[Any, int] = {}
    by_signature = {gap.signature: gap.gap_id for gap in existing}
    questions: list[tuple[int, str]] = []
    for index, seed in enumerate(seeds):
        for key in _keys(snapshot, seed, by_signature):
            union.join(first_of.setdefault(key, index), index)
        question = question_text(snapshot, seed)
        if question is not None:
            for other in (other for other, text in questions if same_question(question, text)):
                union.join(other, index)
            questions.append((index, question))
    grouped: dict[int, list[Seed]] = defaultdict(list)
    for index, seed in enumerate(seeds):
        grouped[union.find(index)].append(seed)
    groups: list[tuple[list[Seed], str | None]] = []
    for members in grouped.values():
        gap = next((by_signature[seed.signature] for seed in members if seed.signature in by_signature), None)
        groups += [(chunk, gap) for chunk in _split(snapshot, members)]
    return groups


def _keys(snapshot: GraphSnapshot, seed: Seed, by_signature: Mapping[str, str]) -> list[tuple[Any, ...]]:
    """What merges a seed with others: each ``ABOUT`` candidate, the existing gap of its signature, and its owning
    modules with its kind and witness section."""

    keys: list[tuple[Any, ...]] = [("about", spec_id) for spec_id in seed.about]
    if seed.signature in by_signature:
        keys.append(("gap", by_signature[seed.signature]))
    owners = frozenset().union(*(snapshot.spine_owners(spec_id) for spec_id in seed.about))
    section = witness_section(snapshot, seed)
    if owners and section is not None:
        keys.append(("owner", owners, seed.kind, section))
    return keys


def _split(snapshot: GraphSnapshot, seeds: list[Seed]) -> list[list[Seed]]:
    """At most ``MAX_SEEDS`` seeds per site: a larger group splits by section, then in order."""

    if len(seeds) <= MAX_SEEDS:
        return [seeds]
    by_section: dict[Any, list[Seed]] = defaultdict(list)
    for seed in sorted(seeds, key=lambda item: item.signature):
        by_section[witness_section(snapshot, seed)].append(seed)
    chunks: list[list[Seed]] = []
    for _, members in sorted(by_section.items(), key=lambda item: str(item[0])):
        chunks += [members[start : start + MAX_SEEDS] for start in range(0, len(members), MAX_SEEDS)]
    return chunks


def site_id(seeds: Iterable[Seed]) -> str:
    """``SITE::`` and the first 12 hex digits (upper case) of SHA-256 of the sorted seed signatures."""

    digest = hashlib.sha256(canonical_json(sorted(seed.signature for seed in seeds)).encode("utf-8")).hexdigest()
    return f"{SITE_PREFIX}{digest[:SITE_HEX].upper()}"


def node_fingerprint(node: GraphNode) -> str:
    """What the input hash takes of a node: its meaning (label, name, attributes, statements) and evidence."""

    return sha256_hex(
        {
            "label": node.label,
            "name": node.name,
            "attributes": node.attributes,
            "statements": sorted(statement.text for statement in node.statements),
            "evidence": sorted(
                (item.fragment_id, item.quote or "", item.line_start or 0, item.line_end or 0)
                for item in node.provenance
            ),
        }
    )


@dataclass(frozen=True)
class SiteRun:
    """What the sites step needs of the run: ids, mode, rules and limits, the ports and the run engine's knowledge
    of existing gaps, the sites the last run deferred, the sites a ``site`` run names and the prompt versions."""

    project_id: str
    run_id: str
    mode: RunMode
    rules: GapRules
    limits: GapLimits
    records: GapRecords
    reader: Any
    plan: PlanView | None = None
    existing: Sequence[ExistingGap] = ()
    deferred_first: Collection[str] = ()
    site_ids: Collection[str] = ()
    prompt_versions: Mapping[str, str] = field(default_factory=dict)
    use_cache: bool = True
    persist: bool = True


async def form_sites(snapshot: GraphSnapshot, detection: DetectOutcome, run: SiteRun) -> SitesOutcome:
    """Turn the seeds of ``detection`` into ranked sites and write the seed, site and note rows (``run.persist``)."""

    seeds = list(detection.seeds)
    suppressed = (
        await run.records.suppressed(run.project_id, [seed.signature for seed in seeds]) if seeds else frozenset()
    )
    kept = [seed for seed in seeds if seed.signature not in suppressed]
    corroborated = await _corroborated(snapshot, kept, run)
    corroborated_ids = {seed.signature for seed in corroborated}
    sites = await _sites(snapshot, corroborated, run)
    if run.mode == "site" or run.site_ids:
        named = set(run.site_ids)
        sites = [site for site in sites if site.site_id in named or (site.gap_id is not None and site.gap_id in named)]
    sites = await _states(sites, run)
    seed_site = {seed.signature: site.site_id for site in sites for seed in site.seeds}
    rows = tuple(
        SeedRow(
            run_id=run.run_id,
            detector=seed.detector,
            spec_ids=seed.about,
            witness=seed.witness,
            strength=seed.strength,
            relevance=seed.relevance,
            corroborated=seed.signature in corroborated_ids,
            site_id=seed_site.get(seed.signature),
        )
        for seed in kept
    )
    if run.persist:
        await _write(run, rows, sites, detection)
    log_event(
        logger,
        INFO,
        "graph.gap.sites.formed",
        project_id=run.project_id,
        run_id=run.run_id,
        seeds=len(seeds),
        suppressed=len(seeds) - len(kept),
        sites=len(sites),
        investigate=sum(1 for site in sites if site.state == "investigate"),
    )
    return SitesOutcome(sites=tuple(sites), seed_rows=rows, counts=_counts(detection, corroborated, suppressed, sites))


async def _corroborated(snapshot: GraphSnapshot, seeds: Sequence[Seed], run: SiteRun) -> list[Seed]:
    """The strong seeds and the weak seeds with enough corroboration signals, in order."""

    weak_ids = sorted(
        {spec_id for seed in seeds if seed.strength == "weak" for spec_id in _with_related(snapshot, seed)}
    )
    planned = await _planned(run.plan, run.project_id, weak_ids)
    parts = SystemParts.of(snapshot, run.rules)
    return [
        seed
        for seed in seeds
        if seed.strength == "strong"
        or _enough(seed, corroboration(snapshot, seed, rules=run.rules, planned=planned, parts=parts))
    ]


async def _write(run: SiteRun, rows: Sequence[SeedRow], sites: Sequence[Site], detection: DetectOutcome) -> None:
    await run.records.put_seeds(run.project_id, run.run_id, rows)
    written = [
        SiteRow(
            run_id=run.run_id,
            site_id=site.site_id,
            input_hash=site.input_hash,
            rank_score=site.rank_score,
            outcome=site.state,
            gap_id=site.gap_id,
        )
        for site in sites
        if site.state != "investigate"
    ]
    if written:
        await run.records.put_sites(run.project_id, run.run_id, written)
    if detection.notes:
        notes = [note.model_copy(update={"run_id": run.run_id}) for note in detection.notes]
        await run.records.put_notes(run.project_id, run.run_id, notes)


def _with_related(snapshot: GraphSnapshot, seed: Seed) -> set[str]:
    found = set(seed.about)
    for spec_id in seed.about:
        found |= snapshot.related(spec_id)
    return found


async def _planned(plan: PlanView | None, project_id: str, spec_ids: Sequence[str]) -> frozenset[str]:
    if plan is None or not spec_ids:
        return frozenset()
    tasks = await plan.tasks_for(project_id, spec_ids)
    return frozenset(spec_id for spec_id, found in tasks.items() if found)


def _enough(seed: Seed, signals: Sequence[str]) -> bool:
    if seed.relevance == MIXED:
        return OBLIGATION in signals and len(signals) >= MIXED_SIGNALS
    return bool(signals)


async def _sites(snapshot: GraphSnapshot, seeds: Sequence[Seed], run: SiteRun) -> list[Site]:
    project_id, rules, reader, plan = run.project_id, run.rules, run.reader, run.plan
    groups = group_seeds(snapshot, seeds, existing=run.existing)
    candidates = sorted(
        {spec_id for members, _ in groups for seed in members for spec_id in (*seed.about, *seed.context)}
    )
    tasks = await plan.tasks_for(project_id, candidates) if plan is not None and candidates else {}
    gaps_on: dict[str, list[ExistingGap]] = defaultdict(list)
    for known in run.existing:
        for spec_id in known.about:
            gaps_on[spec_id].append(known)
    sites: list[Site] = []
    for members, gap in groups:
        about = tuple(sorted({spec_id for seed in members for spec_id in seed.about}))
        context = tuple(sorted({spec_id for seed in members for spec_id in seed.context} - set(about)))
        ids = [spec_id for spec_id in (*about, *context) if spec_id in snapshot.nodes]
        reached = await _trace(reader, project_id, ids)
        task_ids = {task.task_id for spec_id in ids for task in tasks.get(spec_id, ())}
        reach = 1 + len(reached) + 2 * len(task_ids)
        weight = max(STRENGTH_WEIGHT[seed.strength] for seed in members)
        prior = max(rules.routing.severity_prior.get(seed.kind, 1.0) for seed in members)
        hashed = sorted({*ids, *reached})
        input_hash = sha256_hex(
            {
                "seeds": sorted(seed.signature for seed in members),
                "nodes": {
                    spec_id: node_fingerprint(snapshot.nodes[spec_id])
                    for spec_id in hashed
                    if spec_id in snapshot.nodes
                },
                "gaps": sorted({(item.gap_id, item.status) for spec_id in hashed for item in gaps_on.get(spec_id, ())}),
                "rules_version": rules.version,
                "prompt_versions": dict(sorted(run.prompt_versions.items())),
            }
        )
        sites.append(
            Site(
                site_id=site_id(members),
                seeds=tuple(sorted(members, key=lambda seed: seed.signature)),
                about=about,
                context=context,
                input_hash=input_hash,
                rank_score=round(weight * prior * reach, 6),
                reach=reach,
                gap_id=gap,
            )
        )
    return sites


async def _trace(reader: Any, project_id: str, spec_ids: Sequence[str]) -> tuple[str, ...]:
    """The nodes trace reaches from ``spec_ids`` along impact roles (``affects``, depth 2), the starts left out."""

    if not spec_ids:
        return ()
    result = await reader.trace(project_id, list(spec_ids)[:TRACE_IDS], direction="affects", depth=REACH_DEPTH)
    return tuple(sorted({node.spec_id for node in result.nodes} - set(spec_ids)))


async def _states(sites: list[Site], run: SiteRun) -> list[Site]:
    first = set(run.deferred_first)
    ordered = sorted(sites, key=lambda site: (site.site_id not in first, -site.rank_score, site.site_id))
    limit = {"update": run.limits.sites_update_run, "full": run.limits.sites_full_run}.get(run.mode)
    result: list[Site] = []
    taken = 0
    for site in ordered:
        verdict = await run.records.verdict(run.project_id, site.input_hash) if run.use_cache else None
        if verdict is not None:
            result.append(_with(site, "skipped", verdict))
        elif limit is not None and taken >= limit:
            result.append(_with(site, "deferred"))
        else:
            taken += 1
            result.append(site)
    return result


def _with(site: Site, state: SiteState, verdict: VerdictRow | None = None) -> Site:
    return Site(
        site_id=site.site_id,
        seeds=site.seeds,
        about=site.about,
        context=site.context,
        input_hash=site.input_hash,
        rank_score=site.rank_score,
        reach=site.reach,
        state=state,
        gap_id=site.gap_id,
        verdict=verdict,
    )


def _counts(
    detection: DetectOutcome,
    corroborated: Sequence[Seed],
    suppressed: Collection[str],
    sites: Sequence[Site],
) -> dict[str, Any]:
    """``seeds_by_detector`` (strong, weak, corroborated, suppressed, skipped as tracking), ``detector_failures``,
    ``sites`` (formed, investigate, skipped, deferred) and ``notes`` (detection.md "Run metrics")."""

    by_detector: dict[str, Counter[str]] = {code: Counter(found) for code, found in detection.by_detector.items()}
    for seed in corroborated:
        by_detector.setdefault(seed.detector, Counter())["corroborated"] += 1
    for seed in detection.seeds:
        if seed.signature in suppressed:
            by_detector.setdefault(seed.detector, Counter())["suppressed"] += 1
    states = Counter(site.state for site in sites)
    return {
        "seeds_by_detector": {code: dict(sorted(found.items())) for code, found in sorted(by_detector.items())},
        "detector_failures": dict(sorted(detection.failures.items())),
        "sites": {
            "formed": len(sites),
            "investigate": states["investigate"],
            "skipped": states["skipped"],
            "deferred": states["deferred"],
        },
        "notes": len(detection.notes),
    }
