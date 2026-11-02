"""Normalisation: Conform reshapes the legal graph by the ontology's flags."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from graph.build.consolidate import DECISION_KIND
from graph.build.models import Basis
from graph.build.ports import ExtractionStore
from graph.build.validator import simple_cycles
from graph.build.vectors import get_many, put_many
from graph.core.llm import Gateway
from graph.core.llm.judge import Evidence, JudgeFailure, JudgeRunner, Verdict, meets_floor, quotes_valid
from graph.core.ontology import DeriveViaRule, NodeView, Ontology, Pair, QuarantineReason
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.build.normalise")

BASIS_RANK: Final[dict[str, int]] = {"stated": 0, "implied": 1, "inferred": 2}
"""Lower is stronger."""
STATED: Final = "stated"
IMPLIED: Final = "implied"
FACT_MAX_CHARS: Final = 300
"""Longest fact a judge quote gives a fallback edge (as ``conform.FACT_MAX_CHARS``)."""
MAX_JUDGE_FRAGMENTS: Final = 8
"""Fragments one judge call shows at most: the winner's edge fragments first, then each loser's."""
MAX_JUDGE_STATEMENTS: Final = 3
"""Statements shown per node in a judge payload."""
CONFORM_KEY_SEGMENT: Final = "conform"
"""Middle segment of a conform judge memory key."""
LOSER_ID_PREFIX: Final = "L"
MIN_CONFLICT_TARGETS: Final = 2

Answer = Literal["relates", "unrelated", "fallback"]
JudgeOutcome = Literal["judged", "reused", "failed", "below_floor", "over_cap", "unavailable"]


# Inputs and outputs


@dataclass(frozen=True)
class NormNode:
    """A node as normalisation sees it: label, attribute values (normalised, defaults filled in) and the
    statements a judge payload shows."""

    key: str
    label: str
    name: str
    attributes: Mapping[str, object] = field(default_factory=dict)
    statements: tuple[str, ...] = ()

    @property
    def view(self) -> NodeView:
        return NodeView(self.label, self.attributes)


@dataclass
class NormEdge:
    """A legal domain edge between node keys."""

    relation: str
    source: str
    target: str
    basis: Basis
    fact: str | None = None
    fragment_ids: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.source}|{self.relation}|{self.target}"


@dataclass(frozen=True)
class Quarantined:
    """An edge normalisation cannot keep: the relation it had when it failed, its ends by key."""

    reason: QuarantineReason
    relation: str
    source: str
    target: str
    basis: Basis
    fragment_ids: frozenset[str]


@dataclass
class NormCounters:
    exclusive_conflicts: int = 0
    """Targets past the first in owner and exclusive groups (stage 1)."""
    owner_conflicts: int = 0
    """Owner and exclusive groups with more than one target."""
    owner_conflicts_judged: int = 0
    """Judge groups (stages 1 and 6) sent to the judge in this build: fresh calls, failed and below the
    floor included; stored verdicts are not (``judge_reused``)."""
    redundant_edges: int = 0
    cycles_broken: int = 0
    unsupported_nodes: int = 0
    derived_edges: int = 0
    judge_calls: int = 0
    """Fresh judge calls (failed and below the floor included)."""
    judge_reused: int = 0
    """Groups decided by a stored verdict (below the floor included)."""
    judge_failed: int = 0
    judge_below_floor: int = 0
    judge_over_cap: int = 0
    judge_invalid_quotes: int = 0


@dataclass
class Normalised:
    edges: list[NormEdge]
    """By edge key."""
    dropped_nodes: frozenset[str]
    quarantine: list[Quarantined]
    counted: dict[str, Counter[str]]
    """Dropped edges per relation and reason."""
    counters: NormCounters
    resolved: bool
    """Stage 1 ran (the ontology resolves owner and exclusive conflicts)."""


# Judge interface


@dataclass(frozen=True)
class JudgeGroup:
    """One judge call: the payload (``relation``, ``subject``, optional ``owner``, ``losers`` with ids)
    and the fragment texts it shows, by fragment id."""

    payload: Mapping[str, Any]
    fragments: Mapping[str, str]

    @property
    def loser_ids(self) -> tuple[str, ...]:
        return tuple(str(loser["id"]) for loser in self.payload["losers"])

    @property
    def digest(self) -> str:
        """sha256 of the judge input (payload and fragments, canonical JSON)."""

        text = json.dumps(
            {"payload": self.payload, "fragments": sorted(self.fragments.items())},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LoserAnswer:
    answer: Answer
    evidence: tuple[Evidence, ...] = ()


FALLBACK: Final = LoserAnswer("fallback")


@dataclass(frozen=True)
class GroupDecision:
    """What becomes of each loser (by local id) and how the group was decided."""

    answers: Mapping[str, LoserAnswer]
    outcome: JudgeOutcome
    invalid_quotes: int = 0
    reused: bool = False
    """The verdict was a stored one (``reused``, or ``below_floor`` from the store): no call."""


class OwnerJudge(Protocol):
    async def decide(self, groups: Sequence[JudgeGroup]) -> list[GroupDecision]:
        """One decision per group, in order."""
        ...


class FallbackJudge:
    """Answers every group with the fallback relation, without a call (``unavailable``)."""

    async def decide(self, groups: Sequence[JudgeGroup]) -> list[GroupDecision]:
        return [GroupDecision(dict.fromkeys(group.loser_ids, FALLBACK), "unavailable") for group in groups]


def _shown(node: NormNode) -> dict[str, Any]:
    return {"name": node.name, "label": node.label, "statements": list(node.statements[:MAX_JUDGE_STATEMENTS])}


def owner_conflict_payload(
    relation: str, subject: NormNode, owner: NormNode | None, losers: Sequence[NormNode]
) -> dict[str, Any]:
    """The payload of one owner-conflict judgement (``judge_owner_conflict`` prompt contract): the
    relation a kept link becomes, the subject, the kept owner (absent for a cycle break) and the losers
    with local ids ``L1``, ``L2``, ..."""

    payload: dict[str, Any] = {"relation": relation, "subject": _shown(subject)}
    if owner is not None:
        payload["owner"] = _shown(owner)
    payload["losers"] = [{"id": f"{LOSER_ID_PREFIX}{index}", **_shown(loser)} for index, loser in enumerate(losers, 1)]
    return payload


# The conform judge


class OwnerAnswer(BaseModel):
    """One loser's answer: ``relates`` (with a quote naming the loser) or ``unrelated``."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="the loser's id from the payload, e.g. L1")
    answer: Literal["relates", "unrelated"]
    evidence: list[Evidence] = Field(default_factory=list, description="the quotes for this loser only")


class OwnerVerdict(Verdict):
    """``judge_owner_conflict``: one answer per loser."""

    answers: list[OwnerAnswer] = Field(description="exactly one entry per loser id")


class ConformJudgeMemory:
    """Stored conform judge verdicts of one project (module docstring)."""

    def __init__(self, store: ExtractionStore, project_id: str, prompt: str, judge_model: str) -> None:
        self._store = store
        self._project_id = project_id
        self.fingerprint = f"{prompt}|{judge_model}"

    def key(self, digest: str) -> str:
        return f"{self._project_id}|{CONFORM_KEY_SEGMENT}|{digest}"

    async def recall(self, digests: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Stored verdict payloads by input digest."""

        wanted = {self.key(digest): digest for digest in digests}
        found = await get_many(self._store, DECISION_KIND, sorted(wanted), self.fingerprint)
        return {wanted[key]: dict(payload) for key, payload in found.items()}

    async def write(self, verdicts: Mapping[str, Mapping[str, Any]]) -> None:
        await put_many(
            self._store,
            DECISION_KIND,
            self.fingerprint,
            {self.key(digest): dict(payload) for digest, payload in sorted(verdicts.items())},
        )


def evaluate_verdict(verdict: OwnerVerdict, group: JudgeGroup, floor: float, *, reused: bool) -> GroupDecision:
    """The decision a verdict gives a group: below ``floor`` every loser keeps the fallback relation;
    else each loser's first answer counts (a missing answer or a ``relates`` answer without valid quotes
    is the fallback relation)."""

    ids = group.loser_ids
    if not meets_floor(verdict, floor):
        return GroupDecision(dict.fromkeys(ids, FALLBACK), "below_floor", reused=reused)
    first: dict[str, OwnerAnswer] = {}
    for item in verdict.answers:
        if item.id in ids:
            first.setdefault(item.id, item)
    answers: dict[str, LoserAnswer] = {}
    invalid = 0
    for loser_id in ids:
        given = first.get(loser_id)
        if given is None:
            answers[loser_id] = FALLBACK
        elif given.answer == "unrelated":
            answers[loser_id] = LoserAnswer("unrelated")
        elif quotes_valid(Verdict(confidence=verdict.confidence, evidence=given.evidence), group.fragments):
            answers[loser_id] = LoserAnswer("relates", tuple(given.evidence))
        else:
            invalid += 1
            answers[loser_id] = FALLBACK
    return GroupDecision(answers, "reused" if reused else "judged", invalid, reused=reused)


class ConformJudge:
    """The owner-conflict judge of one build (module docstring). ``cap`` counts the fresh calls of every
    ``decide`` of this object."""

    def __init__(self, gateway: Gateway, ontology: Ontology, memory: ConformJudgeMemory | None = None) -> None:
        spec = ontology.conform_spec()
        if spec is None:
            raise ValueError("conform judge needs the ontology's conform section")
        self._runner = JudgeRunner(gateway, ontology)
        self._prompt = spec.judge_prompt
        self._cap = spec.cap
        self._floor = spec.floor
        self._memory = memory
        self.fresh_calls = 0

    async def decide(self, groups: Sequence[JudgeGroup]) -> list[GroupDecision]:
        digests = [group.digest for group in groups]
        stored = await self._memory.recall(set(digests)) if self._memory is not None and groups else {}
        decisions: list[GroupDecision | None] = []
        calls: list[int] = []
        for index, (group, digest) in enumerate(zip(groups, digests, strict=True)):
            verdict = _stored_verdict(stored.get(digest))
            if verdict is not None:
                decisions.append(evaluate_verdict(verdict, group, self._floor, reused=True))
            elif self.fresh_calls < self._cap:
                self.fresh_calls += 1
                calls.append(index)
                decisions.append(None)
            else:
                decisions.append(GroupDecision(dict.fromkeys(group.loser_ids, FALLBACK), "over_cap"))
        results = await _gather(
            [
                self._runner.judge(
                    self._prompt, {**groups[index].payload, "fragments": dict(groups[index].fragments)}, OwnerVerdict
                )
                for index in calls
            ]
        )
        remember: dict[str, dict[str, Any]] = {}
        for index, result in zip(calls, results, strict=True):
            group = groups[index]
            if isinstance(result, JudgeFailure):
                decisions[index] = GroupDecision(dict.fromkeys(group.loser_ids, FALLBACK), "failed")
                continue
            remember[digests[index]] = {"verdict": result.model_dump(mode="json")}
            decisions[index] = evaluate_verdict(result, group, self._floor, reused=False)
        if remember and self._memory is not None:
            await self._memory.write(remember)
        return [decision for decision in decisions if decision is not None]


def _stored_verdict(entry: Mapping[str, Any] | None) -> OwnerVerdict | None:
    if entry is None:
        return None
    try:
        return OwnerVerdict.model_validate(entry.get("verdict"))
    except ValidationError:
        return None


async def _gather(awaitables: Sequence[Awaitable[Any]]) -> list[Any]:
    """All results in order; on the first error the other calls are cancelled and the error raised."""

    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# Normalisation


def resolves_conflicts(ontology: Ontology) -> bool:
    """True when stage 1 runs: the ontology declares an ``owner`` pair or a ``conform`` section."""

    if ontology.conform_spec() is not None:
        return True
    return any(pair.owner for relation in ontology.relations.values() for pair in relation.pairs)


async def normalise(
    nodes: Mapping[str, NormNode],
    edges: Iterable[NormEdge],
    ontology: Ontology,
    *,
    judge: OwnerJudge | None = None,
    texts: Mapping[str, str] | None = None,
    derived: Iterable[NormEdge] = (),
    admit: Callable[[NormEdge], bool] | None = None,
) -> Normalised:
    """Normalise one target graph (module docstring)."""

    run = _Normaliser(nodes, ontology, judge or FallbackJudge(), texts or {})
    for edge in edges:
        run.add(edge)
    resolved = resolves_conflicts(ontology)
    if resolved:
        await run.conflicts()
    run.conditions()
    run.depth()
    run.redundant_via_owner()
    run.distinct_owner()
    await run.cycles()
    run.unsupported()
    run.derive(derived, admit or (lambda _: True))
    counters = run.counters
    log_event(
        logger,
        INFO,
        "graph.normalise.done",
        edges=len(run.edges),
        quarantined=len(run.quarantine),
        owner_conflicts=counters.owner_conflicts,
        owner_conflicts_judged=counters.owner_conflicts_judged,
        redundant=counters.redundant_edges,
        cycles_broken=counters.cycles_broken,
        unsupported=counters.unsupported_nodes,
        derived=counters.derived_edges,
        judge_calls=counters.judge_calls,
        judge_reused=counters.judge_reused,
        judge_failed=counters.judge_failed,
        judge_over_cap=counters.judge_over_cap,
    )
    return Normalised(
        edges=[run.edges[key] for key in sorted(run.edges)],
        dropped_nodes=frozenset(run.dropped_nodes),
        quarantine=run.quarantine,
        counted=run.counted,
        counters=counters,
        resolved=resolved,
    )


@dataclass
class _Pending:
    """A judge group waiting for its decision: the loser edges per local id."""

    group: JudgeGroup
    losers: dict[str, list[NormEdge]]
    breaking: bool = False
    """A cycle break: an ``unrelated`` answer quarantines, the edge is no longer in the graph."""


class _Normaliser:
    def __init__(self, nodes: Mapping[str, NormNode], ontology: Ontology, judge: OwnerJudge, texts: Mapping[str, str]):
        self.nodes = nodes
        self.ontology = ontology
        self.judge = judge
        self.texts = texts
        self.rank = {fragment_id: index for index, fragment_id in enumerate(texts)}
        spec = ontology.conform_spec()
        self.fallback = spec.fallback_relation if spec is not None else None
        self.edges: dict[str, NormEdge] = {}
        self.quarantine: list[Quarantined] = []
        self.counted: dict[str, Counter[str]] = {}
        self.counters = NormCounters()
        self.dropped_nodes: set[str] = set()

    # Edge store

    def add(self, edge: NormEdge) -> NormEdge:
        """Adds ``edge``, merging it into an existing edge of its key (Conform's merge rule)."""

        existing = self.edges.get(edge.key)
        if existing is None:
            self.edges[edge.key] = edge
            return edge
        existing.fragment_ids |= edge.fragment_ids
        if BASIS_RANK[edge.basis] < BASIS_RANK[existing.basis]:
            existing.basis = edge.basis
            existing.fact = edge.fact or existing.fact
        elif existing.fact is None:
            existing.fact = edge.fact
        return existing

    def remove(self, edge: NormEdge) -> None:
        self.edges.pop(edge.key, None)

    def drop(self, edge: NormEdge, reason: QuarantineReason, relation: str | None = None) -> None:
        """Removes ``edge`` and quarantines it with ``reason`` (under ``relation`` when given)."""

        self.remove(edge)
        name = relation or edge.relation
        self.quarantine.append(
            Quarantined(reason, name, edge.source, edge.target, edge.basis, frozenset(edge.fragment_ids))
        )
        self.counted.setdefault(name, Counter())[reason] += 1

    def count(self, edge: NormEdge, reason: str) -> None:
        """Removes ``edge`` and counts it (``redundant``, ``unsupported``)."""

        self.remove(edge)
        self.counted.setdefault(edge.relation, Counter())[reason] += 1
        if reason == "redundant":
            self.counters.redundant_edges += 1

    def label(self, key: str) -> str:
        return self.nodes[key].label

    def pair(self, edge: NormEdge) -> Pair | None:
        return self.ontology.pair_rule(edge.relation, self.label(edge.source), self.label(edge.target))

    def orient(self, relation: str, source: str, target: str) -> tuple[str, str] | None:
        """The ends of ``relation`` between the two nodes when a declared pair allows it (a symmetric
        relation in canonical order, either direction), else None."""

        ontology = self.ontology
        if relation not in ontology.relations:
            return None
        symmetric = ontology.is_symmetric(relation)
        if ontology.is_legal(relation, self.label(source), self.label(target)):
            return (min(source, target), max(source, target)) if symmetric else (source, target)
        if symmetric and ontology.is_legal(relation, self.label(target), self.label(source)):
            return min(source, target), max(source, target)
        return None

    def convert(self, edge: NormEdge, relation: str, ends: tuple[str, str]) -> NormEdge:
        self.remove(edge)
        return self.add(NormEdge(relation, ends[0], ends[1], edge.basis, edge.fact, set(edge.fragment_ids)))

    def owners(self) -> dict[str, str]:
        """Each node's owner: its target on an ``owner`` pair (lowest rank, relation, target first)."""

        best: dict[str, tuple[int, str, str]] = {}
        for edge in self.edges.values():
            pair = self.pair(edge)
            if pair is None or not pair.owner:
                continue
            rank = (pair.owner_rank, edge.relation, edge.target)
            if edge.source not in best or rank < best[edge.source]:
                best[edge.source] = rank
        return {source: rank[2] for source, rank in best.items()}

    def conditions_hold(self, relation: str, source: str, target: str) -> bool:
        pair = self.ontology.pair_rule(relation, self.label(source), self.label(target))
        if pair is None:
            return False
        if self._across(pair, source, target):
            return False
        return self.ontology.where_holds(pair, self.nodes[source].view, self.nodes[target].view)

    def _across(self, pair: Pair, source: str, target: str) -> bool:
        """True when the ends differ in one of the pair's ``not_across`` attributes (a value still null
        after defaults never differs)."""

        for attribute in pair.not_across:
            values = []
            for key in (source, target):
                node = self.nodes[key]
                value = node.attributes.get(attribute)
                if value is None:
                    value = self.ontology.attribute_default(node.label, attribute)
                values.append(value)
            if None not in values and str(values[0]) != str(values[1]):
                return True
        return False

    def _ordered(self, fragment_ids: Iterable[str]) -> list[str]:
        """The known fragment ids among ``fragment_ids``, in document order."""

        known = {fid for fid in fragment_ids if fid in self.texts}
        return sorted(known, key=lambda fid: (self.rank.get(fid, len(self.rank)), fid))

    def fragments(self, kept: Sequence[NormEdge], losers: Mapping[str, Sequence[NormEdge]]) -> dict[str, str]:
        """The fragment texts a judge group shows, in document order, at most ``MAX_JUDGE_FRAGMENTS``:
        first one fragment per loser (``losers`` by target, in order) unless one already shown holds
        it, then the kept edges' fragments, then the losers' other fragments."""

        chosen: set[str] = set()
        groups = [self._ordered(fid for edge in edges for fid in edge.fragment_ids) for edges in losers.values()]
        for ids in groups:
            if ids and not chosen & set(ids) and len(chosen) < MAX_JUDGE_FRAGMENTS:
                chosen.add(ids[0])
        rest = [
            *self._ordered(fid for edge in kept for fid in edge.fragment_ids),
            *(fid for ids in groups for fid in ids),
        ]
        for fragment_id in rest:
            if len(chosen) >= MAX_JUDGE_FRAGMENTS:
                break
            chosen.add(fragment_id)
        return {fragment_id: self.texts[fragment_id] for fragment_id in self._ordered(chosen)}

    # Stage 1: owner and exclusive conflicts

    async def conflicts(self) -> None:
        owner_groups: dict[str, list[NormEdge]] = {}
        exclusive_groups: dict[tuple[str, str, str, str], list[NormEdge]] = {}
        for key in sorted(self.edges):
            edge = self.edges[key]
            pair = self.pair(edge)
            if pair is None:
                continue
            if pair.owner:
                owner_groups.setdefault(edge.source, []).append(edge)
            elif pair.exclusive:
                exclusive_groups.setdefault((edge.source, pair.relation, pair.source, pair.target), []).append(edge)
        winners: dict[str, str] = {}
        groups: list[tuple[str, list[NormEdge], str]] = []
        for source, members in sorted(owner_groups.items()):
            winner = self._winner(members)
            winners[source] = winner
            groups.append((source, members, winner))
        for (source, *_), members in sorted(exclusive_groups.items()):
            groups.append((source, members, self._winner(members)))
        pending: list[_Pending] = []
        for source, members, winner in groups:
            targets = {edge.target for edge in members}
            if len(targets) < MIN_CONFLICT_TARGETS:
                continue
            self.counters.owner_conflicts += 1
            self.counters.exclusive_conflicts += len(targets) - 1
            to_judge = [edge for edge in members if edge.target != winner and not self._settle(edge, winner, winners)]
            if to_judge:
                pending.append(self._group(source, winner, [e for e in members if e.target == winner], to_judge))
        await self._decide(pending)

    def _winner(self, members: Sequence[NormEdge]) -> str:
        def rank(edge: NormEdge) -> tuple[bool, int, int, int, str]:
            pair = self.pair(edge)
            return (
                not self.conditions_hold(edge.relation, edge.source, edge.target),
                pair.owner_rank if pair else 0,
                BASIS_RANK[edge.basis],
                -len(edge.fragment_ids),
                edge.target,
            )

        return min(members, key=rank).target

    def _settle(self, edge: NormEdge, winner: str, winners: Mapping[str, str]) -> bool:
        """Settles a loser edge without the judge when a rule decides it; False when it needs the judge."""

        winner_owner = winners.get(winner)
        if edge.target == winner_owner:
            self.count(edge, "redundant")
            return True
        fallback_ends = self.orient(self.fallback, edge.source, edge.target) if self.fallback else None
        if fallback_ends is not None and winner_owner is None:
            self.convert(edge, cast(str, self.fallback), fallback_ends)
            return True
        if fallback_ends is not None:
            return False
        default = self.ontology.default_for(self.nodes[edge.source].view, self.nodes[edge.target].view)
        ends = self.orient(default, edge.source, edge.target) if default and default != edge.relation else None
        if default is not None and ends is not None:
            self.convert(edge, default, ends)
        else:
            self.drop(edge, "owner_conflict")
        return True

    def _group(self, source: str, owner: str | None, kept: Sequence[NormEdge], losers: Sequence[NormEdge]) -> _Pending:
        by_target: dict[str, list[NormEdge]] = {}
        for edge in losers:
            by_target.setdefault(edge.target, []).append(edge)
        targets = sorted(by_target)
        payload = owner_conflict_payload(
            cast(str, self.fallback),
            self.nodes[source],
            self.nodes[owner] if owner is not None else None,
            [self.nodes[target] for target in targets],
        )
        group = JudgeGroup(payload, self.fragments(kept, {target: by_target[target] for target in targets}))
        return _Pending(
            group, {loser_id: by_target[target] for loser_id, target in zip(group.loser_ids, targets, strict=True)}
        )

    async def _decide(self, pending: Sequence[_Pending]) -> None:
        if not pending:
            return
        decisions = await self.judge.decide([item.group for item in pending])
        for item, decision in zip(pending, decisions, strict=True):
            self._tally(decision)
            for loser_id, edges in item.losers.items():
                answer = decision.answers.get(loser_id, FALLBACK)
                for edge in edges:
                    self._apply(edge, answer, item)

    def _tally(self, decision: GroupDecision) -> None:
        counters = self.counters
        if decision.reused:
            counters.judge_reused += 1
        elif decision.outcome in ("judged", "failed", "below_floor"):
            counters.owner_conflicts_judged += 1
            counters.judge_calls += 1
        if decision.outcome == "failed":
            counters.judge_failed += 1
        elif decision.outcome == "below_floor":
            counters.judge_below_floor += 1
        elif decision.outcome == "over_cap":
            counters.judge_over_cap += 1
        counters.judge_invalid_quotes += decision.invalid_quotes

    def _apply(self, edge: NormEdge, answer: LoserAnswer, item: _Pending) -> None:
        relation = cast(str, self.fallback)
        if answer.answer == "unrelated":
            if item.breaking:
                self._quarantine_removed(edge, "judge_rejected")
            else:
                self.drop(edge, "judge_rejected")
            return
        ends = self.orient(relation, edge.source, edge.target)
        if ends is None:  # the group was formed only for ends that allow it
            self.drop(edge, "owner_conflict")
            return
        if not item.breaking:
            self.remove(edge)
        fact = edge.fact
        fragment_ids = set(edge.fragment_ids)
        if answer.answer == "relates" and answer.evidence:
            quote = answer.evidence[0]
            fact = fact or quote.quote[:FACT_MAX_CHARS]
            fragment_ids.add(quote.fragment_id)
        self.add(NormEdge(relation, ends[0], ends[1], edge.basis, fact, fragment_ids))

    def _quarantine_removed(self, edge: NormEdge, reason: QuarantineReason) -> None:
        self.quarantine.append(
            Quarantined(reason, edge.relation, edge.source, edge.target, edge.basis, frozenset(edge.fragment_ids))
        )
        self.counted.setdefault(edge.relation, Counter())[reason] += 1

    # Stage 2: where and not_across

    def conditions(self) -> None:
        for key in sorted(self.edges):
            edge = self.edges.get(key)
            if edge is None:
                continue
            relation, ends = edge.relation, (edge.source, edge.target)
            tried: set[str] = set()
            while True:
                pair = self.ontology.pair_rule(relation, self.label(ends[0]), self.label(ends[1]))
                if pair is None:
                    break
                reason = cast(QuarantineReason, pair.where_reason)
                if self._across(pair, *ends):
                    self._drop_as(edge, reason, relation, ends)
                    break
                if self.ontology.where_holds(pair, self.nodes[ends[0]].view, self.nodes[ends[1]].view):
                    if relation != edge.relation:
                        self.convert(edge, relation, ends)
                    break
                tried.add(relation)
                otherwise = pair.otherwise
                converted = self.orient(otherwise, *ends) if otherwise and otherwise not in tried else None
                if otherwise is None or converted is None:
                    self._drop_as(edge, reason, relation, ends)
                    break
                relation, ends = otherwise, converted

    def _drop_as(self, edge: NormEdge, reason: QuarantineReason, relation: str, ends: tuple[str, str]) -> None:
        self.remove(edge)
        self._quarantine_removed(NormEdge(relation, ends[0], ends[1], edge.basis, edge.fact, edge.fragment_ids), reason)

    # Stage 3: containment depth

    def depth(self) -> None:
        parents: dict[tuple[str, str, str], dict[str, set[str]]] = {}
        limited: list[tuple[NormEdge, Pair]] = []
        for key in sorted(self.edges):
            edge = self.edges[key]
            pair = self.pair(edge)
            if pair is None or pair.max_depth is None:
                continue
            parents.setdefault((pair.relation, pair.source, pair.target), {}).setdefault(edge.source, set()).add(
                edge.target
            )
            limited.append((edge, pair))
        cut = [
            edge
            for edge, pair in limited
            if _chain_longer(
                parents[(pair.relation, pair.source, pair.target)], edge.source, edge.target, cast(int, pair.max_depth)
            )
        ]
        for edge in cut:
            self.drop(edge, "depth_cut")

    # Stages 4 and 5: redundant via owner, distinct owner

    def redundant_via_owner(self) -> None:
        """Decided on a snapshot of the edges, then removed, so the order of the edges never matters."""

        owners = self.owners()
        redundant: list[NormEdge] = []
        for key in sorted(self.edges):
            edge = self.edges[key]
            pair = self.pair(edge)
            if pair is None or not pair.redundant_via_owner:
                continue
            if edge.relation == self.fallback and edge.basis == STATED:
                continue
            owner = owners.get(edge.source)
            ends = (
                self.orient(edge.relation, owner, edge.target) if owner is not None and owner != edge.target else None
            )
            if ends is not None and f"{ends[0]}|{edge.relation}|{ends[1]}" in self.edges:
                redundant.append(edge)
        for edge in redundant:
            self.count(edge, "redundant")

    def distinct_owner(self) -> None:
        owners = self.owners()
        for key in sorted(self.edges):
            edge = self.edges[key]
            pair = self.pair(edge)
            if pair is None or not pair.distinct_owner:
                continue
            owner = owners.get(edge.source)
            if owner is not None and owner == owners.get(edge.target):
                self.count(edge, "redundant")

    # Stage 6: cycles

    async def cycles(self) -> None:
        broken: list[NormEdge] = []
        for relation in sorted(self.ontology.acyclic_relations()):
            while True:
                adjacency: dict[str, set[str]] = {}
                for edge in self.edges.values():
                    if edge.relation == relation:
                        adjacency.setdefault(edge.source, set()).add(edge.target)
                found = simple_cycles(adjacency, limit=1)
                if not found:
                    break
                cycle = found[0]
                members = [
                    self.edges[f"{cycle[index]}|{relation}|{cycle[(index + 1) % len(cycle)]}"]
                    for index in range(len(cycle))
                ]
                weakest = min(members, key=lambda e: (-BASIS_RANK[e.basis], len(e.fragment_ids), e.key))
                self.remove(weakest)
                broken.append(weakest)
        self.counters.cycles_broken += len(broken)
        pending: list[_Pending] = []
        for edge in broken:
            ends = self.orient(self.fallback, edge.source, edge.target) if self.fallback else None
            if ends is None:
                self._quarantine_removed(edge, "cycle_broken")
                continue
            item = self._group(edge.source, None, [], [edge])
            item.breaking = True
            pending.append(item)
        await self._decide(pending)

    # Stage 7: unsupported nodes

    def unsupported(self) -> None:
        held: dict[str, set[tuple[str, str]]] = {}
        for edge in self.edges.values():
            held.setdefault(edge.target, set()).add((edge.relation, "in"))
            held.setdefault(edge.source, set()).add((edge.relation, "out"))
        for key in sorted(self.nodes):
            node = self.nodes[key]
            rules = self.ontology.supported_by(node.label)
            if not rules or any((rule.relation, rule.direction) in held.get(key, set()) for rule in rules):
                continue
            self.dropped_nodes.add(key)
            self.counters.unsupported_nodes += 1
        if not self.dropped_nodes:
            return
        for key in sorted(self.edges):
            edge = self.edges[key]
            if edge.source in self.dropped_nodes or edge.target in self.dropped_nodes:
                self.count(edge, "unsupported")

    # Stage 8: derived edges

    def derive(self, candidates: Iterable[NormEdge], admit: Callable[[NormEdge], bool]) -> None:
        derived = [edge for rule in self.ontology.derive_via_rules() for edge in self._derived_via(rule)]
        derived.extend(self._kept_candidates(candidates))
        for edge in derived:
            if edge.source in self.dropped_nodes or edge.target in self.dropped_nodes:
                continue
            ends = self.orient(edge.relation, edge.source, edge.target)
            if ends is None or not self.conditions_hold(edge.relation, *ends):
                continue
            oriented = NormEdge(edge.relation, ends[0], ends[1], edge.basis, edge.fact, set(edge.fragment_ids))
            if not admit(oriented):
                continue
            if oriented.key not in self.edges:
                self.counters.derived_edges += 1
            self.add(oriented)

    def _derived_via(self, rule: DeriveViaRule) -> list[NormEdge]:
        """The ``implied`` edges of one ``derive_via`` rule: consumed target to provided target per hub."""

        consumers: dict[str, list[NormEdge]] = {}
        providers: dict[str, list[NormEdge]] = {}
        for key in sorted(self.edges):
            edge = self.edges[key]
            if edge.relation == rule.consumer:
                consumers.setdefault(edge.source, []).append(edge)
            if edge.relation == rule.provider:
                providers.setdefault(edge.source, []).append(edge)
        return [
            NormEdge(
                rule.relation,
                consumed.target,
                provided.target,
                IMPLIED,
                None,
                consumed.fragment_ids | provided.fragment_ids,
            )
            for hub in sorted(set(consumers) & set(providers))
            for consumed in consumers[hub]
            for provided in providers[hub]
            if consumed.target != provided.target
        ]

    def _kept_candidates(self, candidates: Iterable[NormEdge]) -> list[NormEdge]:
        """``candidates`` without those on a dropped node; one between two nodes of the same owner is
        counted ``redundant`` instead."""

        owners = self.owners()
        kept: list[NormEdge] = []
        for candidate in candidates:
            if candidate.source in self.dropped_nodes or candidate.target in self.dropped_nodes:
                continue
            owner = owners.get(candidate.source)
            if owner is not None and owner == owners.get(candidate.target):
                self.counted.setdefault(candidate.relation, Counter())["redundant"] += 1
                self.counters.redundant_edges += 1
                continue
            kept.append(candidate)
        return kept


def _chain_longer(parents: Mapping[str, set[str]], source: str, target: str, max_depth: int) -> bool:
    """True when a simple chain of more than ``max_depth`` edges starts with ``source -> target``
    (the validator's ``depth`` check)."""

    stack: list[tuple[str, int, frozenset[str]]] = [(target, 1, frozenset({source, target}))]
    while stack:
        node, length, seen = stack.pop()
        if length > max_depth:
            return True
        stack.extend(
            (parent, length + 1, seen | {parent}) for parent in sorted(parents.get(node, ())) if parent not in seen
        )
    return False
