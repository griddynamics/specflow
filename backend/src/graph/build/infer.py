"""Light inference: domain edges between existing spec nodes, judged by a model."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from collections import Counter, deque
from collections.abc import Callable, Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import combinations
from typing import Any, Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from graph.build import infer_bound, infer_reads, infer_xdoc, metrics
from graph.build.conform import GROUNDING_STATUSES
from graph.build.consolidate import DECISION_KIND
from graph.build.hubforms import hub_words
from graph.build.infer_xdoc import CrossDocumentVerdict
from graph.build.metrics import READ_BASES
from graph.build.models import (
    BacklogQueue,
    InferenceReport,
    JudgedItem,
    JudgedItemKind,
    QuarantinedEdge,
    RuleLimit,
    RuleReport,
    edge_ref,
)
from graph.build.ports import BuildStore, ExtractionStore
from graph.build.prompts import BREADCRUMB_SEPARATOR
from graph.build.queries import (
    READ_INFER_FRAGMENTS,
    READ_INFER_NODES,
    container_path,
    infer_delete_edges,
    infer_endpoint_subjects,
    infer_exclusive_holders,
    infer_realization_subjects,
    infer_similar_targets,
    infer_write_edges,
    read_current_edges,
)
from graph.build.spine import SpineEdge, SpineGraph, SpineNode, load_spine_graph, owner_relations
from graph.build.terms import heading_section
from graph.build.validator import InMemoryGraphView, ProposalChecker, ViewEdge, ViewNode
from graph.build.vectors import get_many, put_many
from graph.core.db import Executor
from graph.core.errors import ConformanceError, LLMError, LLMFatalError
from graph.core.identity import edge_uuid
from graph.core.llm import Evidence, JudgeFailure, JudgeRunner, Verdict, meets_floor, quote_in, quotes_valid
from graph.core.llm.gateway import request_tokens
from graph.core.llm.judge import FRAGMENTS_KEY, judge_messages
from graph.core.ontology import QUOTE_ATTRIBUTE, InferenceRule, NodeView, Ontology
from graph.core.ontology.model import CROSS_DOCUMENT
from graph.core.queries import Query
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.infer")

QUOTE_FIELDS: Final = 2
"""Fields of a stored evidence item: fragment id and quote."""
INFERRED: Final = "inferred"
SUBJECT_FRAGMENTS: Final = 3
OTHER_FRAGMENTS: Final = 1
MAX_FRAGMENTS: Final = 8
STATEMENTS_SHOWN: Final = 3
SECTIONS_SHOWN: Final = 3
"""Most sections an ``attach_orphan`` payload shows per node (``_owner_shown``)."""
MAX_SHOWN: Final = 12
"""Most members and most candidates a ``realization`` payload shows."""
BATCH_MIN: Final = 2
"""Fewest subjects of a batched ``attach_orphan`` call (``InferenceRule.batch``); one subject is a single call."""

SEEN: Final = "seen"
"""Last segment of a judge memory key recording that a subject was judged."""
MEMBERS: Final = "members"
"""Last segment of a judge memory key recording a ``cross_document`` subject's members (``spec_id``s)."""
BUDGET: Final = "budget"
"""Middle segment of the judge memory key of a build's completed inference input (``JudgeMemory.spent``)."""
TOKENS: Final = "tokens"

ENDPOINTS: Final = "endpoints"
REALIZATION: Final = "realization"
ATTACH_ORPHAN: Final = "attach_orphan"

INVALID_PROPOSAL: Final = "invalid_proposal"
"""The quarantine reason of an inferred edge the shared validator refuses."""
REFUSED: Final = "refused"
"""Key of a stored judgement's edges the shared validator refused (an ``invalid`` outcome)."""

Outcome = Literal["accepted", "declined", "rejected", "invalid"]
OUTCOMES: Final[tuple[Outcome, ...]] = ("accepted", "declined", "rejected", "invalid")


# Verdict models. Fields after the base's ``confidence`` and ``evidence`` carry local ids.


class EndpointLink(BaseModel):
    """One directed link between two targets: the source initiates, the target is called or used."""

    model_config = ConfigDict(frozen=True)

    source: str = Field(description="id of the target that initiates the interaction or uses the other")
    target: str = Field(description="id of the target that is called or depended on")


class EndpointsVerdict(Verdict):
    """Template endpoints: the links the fragments show between candidate pairs; empty when none."""

    links: list[EndpointLink] = Field(description="links on candidate pairs; empty when none qualifies")


class RealizationVerdict(Verdict):
    """Template realization: the candidates that realise the subject; empty when none."""

    targets: list[str] = Field(description="ids of the candidates that realise the subject; empty when none")


class AttachOrphanVerdict(Verdict):
    """Template attach_orphan: the one candidate the subject is a part of, or null."""

    target: str | None = Field(description="id of the candidate the subject is part of, or null when none")


class OrphanAnswer(BaseModel):
    """One subject's answer in a batched ``attach_orphan`` call (``AttachOrphanBatchVerdict``)."""

    model_config = ConfigDict(frozen=True)

    subject: str = Field(description="id of the subject this answer is for")
    target: str | None = Field(description="id of the candidate the subject is part of, or null when none")
    confidence: float = Field(ge=0.0, le=1.0, description="how sure you are of this answer, from 0 to 1")
    evidence: list[Evidence] = Field(description="passages that show it: fragment id and a verbatim quote")


class AttachOrphanBatchVerdict(Verdict):
    """Template attach_orphan with ``batch``: one answer per subject; the top-level confidence is the
    lowest answer's and the top-level evidence is empty (every quote belongs to an answer)."""

    answers: list[OrphanAnswer] = Field(description="one answer per subject, each subject exactly once")


VERDICT_TYPES: Final[dict[str, type[Verdict]]] = {
    ENDPOINTS: EndpointsVerdict,
    REALIZATION: RealizationVerdict,
    ATTACH_ORPHAN: AttachOrphanVerdict,
    CROSS_DOCUMENT: CrossDocumentVerdict,
}


# Judge memory


class JudgeMemory:
    """Stored judgements of one project: ExtractionStore kind ``decisions``, key ``<project_id>|<rule id>|<subject
    spec_id>|<input hash>``, fingerprint ``<prompt>|<judge model>``.
    """

    def __init__(self, store: ExtractionStore | None, project_id: str, judge_model: str) -> None:
        self._store = store
        self._project_id = project_id
        self.judge_model = judge_model

    def _fingerprint(self, prompt: str) -> str:
        return f"{prompt}|{self.judge_model}"

    def key(self, rule: InferenceRule, spec_id: str, digest: str) -> str:
        return f"{self._project_id}|{rule.id}|{spec_id}|{digest}"

    def seen_key(self, rule: InferenceRule, spec_id: str) -> str:
        return f"{self._project_id}|{rule.id}|{spec_id}|{SEEN}"

    async def seen(self, rule: InferenceRule, spec_ids: Iterable[str]) -> set[str]:
        """The ``spec_id``s of the subjects judged before (under this prompt and judge model)."""

        wanted = {self.seen_key(rule, spec_id): spec_id for spec_id in spec_ids}
        found = await self.recall(rule, sorted(wanted))
        return {wanted[key] for key in found}

    def members_key(self, rule: InferenceRule, spec_id: str) -> str:
        return f"{self._project_id}|{rule.id}|{spec_id}|{MEMBERS}"

    async def members(self, rule: InferenceRule, spec_ids: Iterable[str]) -> dict[str, frozenset[str]]:
        """The member ``spec_id``s each ``cross_document`` subject had when it was last a subject (a
        subject never recorded is absent)."""

        wanted = {self.members_key(rule, spec_id): spec_id for spec_id in spec_ids}
        found = await self.recall(rule, sorted(wanted))
        return {
            wanted[key]: frozenset(str(item) for item in entry[MEMBERS])
            for key, entry in found.items()
            if isinstance(entry.get(MEMBERS), list)
        }

    async def recall(self, rule: InferenceRule, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        if self._store is None or not keys:
            return {}
        return await get_many(self._store, DECISION_KIND, list(keys), self._fingerprint(rule.prompt))

    async def remember(self, rule: InferenceRule, entries: Mapping[str, dict[str, Any]]) -> None:
        if self._store is not None and entries:
            await put_many(self._store, DECISION_KIND, self._fingerprint(rule.prompt), dict(entries))

    def budget_key(self, build_id: str) -> str:
        return f"{self._project_id}|{BUDGET}|{build_id}"

    async def spent(self, build_id: str) -> int:
        """The counted input of the build's completed fresh calls recorded so far (0 when none)."""

        if self._store is None:
            return 0
        key = self.budget_key(build_id)
        found = await get_many(self._store, DECISION_KIND, [key], self._fingerprint(BUDGET))
        tokens = found.get(key, {}).get(TOKENS)
        return tokens if isinstance(tokens, int) and tokens > 0 else 0

    async def record_spent(self, build_id: str, tokens: int) -> None:
        """Record the counted input of the build's completed fresh calls (``spent``)."""

        if self._store is not None:
            entries = {self.budget_key(build_id): {TOKENS: tokens}}
            await put_many(self._store, DECISION_KIND, self._fingerprint(BUDGET), entries)


# Selection


@dataclass(frozen=True)
class _Node:
    uuid: str
    spec_id: str
    name: str
    label: str
    identity_key: str
    statements: tuple[str, ...]
    fragments: tuple[tuple[str, str], ...]
    """``(fragment_id, content_hash)`` of its first grounding fragments, in document order."""
    sections: tuple[str, ...] = ()
    """The section it sits in within each of ``fragments`` (``terms.heading_section``), in their order."""
    documents: tuple[str, ...] = ()
    """The document of each of ``fragments``, in their order."""
    quote: str = ""
    """Its ``quote`` attribute: with identity key, label and name, what the judgement input hash keys it by."""


@dataclass(frozen=True)
class _Subject:
    """One subject with its candidates (uuids); ``pairs`` and ``targets`` for ``endpoints``, ``members`` and ``via``
    (candidate uuid to member uuids) for ``realization``.
    """

    uuid: str
    spec_id: str
    candidates: tuple[str, ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()
    targets: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    via: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    truncated: bool = False
    owners: Mapping[str, str] = field(default_factory=dict)
    documents: Mapping[str, str] = field(default_factory=dict)
    reads: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def owns(self, template: str, source: str, target: str) -> bool:
        if template == ENDPOINTS:
            return source in self.targets and target in self.targets
        if template == CROSS_DOCUMENT:
            return source in self.targets and (target in self.targets or target in self.via.get(source, ()))
        return self.uuid in (source, target)


@dataclass(frozen=True)
class _CurrentEdge:
    """A stored edge of a relation inference reads; ``confidence`` and ``evidence`` (the stored JSON
    text) are an inferred edge's, compared with a reused judgement's edge (``_in_place``)."""

    uuid: str | None
    edge_key: str
    source: str
    target: str
    basis: str
    rule: str | None
    ref: str
    relation: str
    confidence: float | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class _Proposal:
    relation: str
    source: str
    target: str
    edge_key: str
    ref: str
    confidence: float
    evidence: tuple[tuple[str, str], ...]
    reused: bool = False


@dataclass(frozen=True)
class _Selection:
    """One rule's selection: current edges of the relations it writes, subjects (``spec_id`` order; ``cross_document``
    once its pairs are chosen: most members first, then ``spec_id``) and lists.
    """

    rule: InferenceRule
    current: tuple[_CurrentEdge, ...]
    subjects: tuple[_Subject, ...]
    outliers: tuple[str, ...] = ()
    without_candidate: tuple[str, ...] = ()
    documents: Mapping[str, frozenset[str]] = field(default_factory=dict)
    stale: frozenset[str] = frozenset()
    duplicate_pairs: int = 0


@dataclass
class _RulePlan:
    """What one rule decided, before the swap; ``memory``: entries to store once the swap committed
    (``cross_document`` members; judgements are written per chunk, before it); ``queue``: the rule's
    queue after this build."""

    rule: InferenceRule
    report: RuleReport
    subjects: tuple[_Subject, ...]
    judged: frozenset[str]
    accepted: dict[str, _Proposal]
    current: tuple[_CurrentEdge, ...]
    memory: dict[str, dict[str, Any]] = field(default_factory=dict)
    quarantine: tuple[QuarantinedEdge, ...] = ()
    stale: frozenset[str] = frozenset()
    items: tuple[JudgedItem, ...] = ()
    queue: BacklogQueue | None = None


@dataclass(frozen=True)
class _Loaded:
    """The spine graph inference reads once per build, its nodes by key, each node's targets on an
    owner pair over stated and implied edges (``owners``) and over the inferred edges earlier builds
    wrote (``inferred``: target to the rule that wrote it)."""

    spine: SpineGraph
    nodes: Mapping[str, SpineNode]
    owners: Mapping[str, frozenset[str]]
    inferred: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @classmethod
    def of(cls, ontology: Ontology, spine: SpineGraph, inferred: Iterable[_CurrentEdge] = ()) -> _Loaded:
        """``inferred``: current edges of the owner relations (``_inferred_owner_edges``); only the
        inferred ones on an owner pair are kept."""

        nodes = {node.key: node for node in spine.nodes}
        owners: dict[str, set[str]] = {}
        for edge in spine.edges:
            if _basis(edge.basis) in READ_BASES and _owner_pair(
                ontology, nodes, edge.relation, edge.source, edge.target
            ):
                owners.setdefault(edge.source, set()).add(edge.target)
        held: dict[str, dict[str, str]] = {}
        for found in inferred:
            if (
                found.basis == INFERRED
                and found.rule
                and _owner_pair(ontology, nodes, found.relation, found.source, found.target)
            ):
                held.setdefault(found.source, {})[found.target] = found.rule
        return cls(spine, nodes, {key: frozenset(found) for key, found in owners.items()}, held)

    def inferred_owners(self, key: str, *, rule: str | None = None, other: bool = False) -> set[str]:
        """The node's owners by inferred edges of earlier builds: all, those of ``rule``, or (``other``)
        those of every other rule."""

        return {
            target
            for target, writer in self.inferred.get(key, {}).items()
            if rule is None or (writer != rule if other else writer == rule)
        }

    def owned_by(self, key: str, labels: Iterable[str]) -> bool:
        """Whether the node has a target on an owner pair with a label in ``labels``."""

        wanted = frozenset(labels)
        return any(self.nodes[owner].label in wanted for owner in self.owners.get(key, ()))

    def excluded(self, ontology: Ontology, rule: InferenceRule, node: SpineNode) -> bool:
        """Whether the node, or one of its owners, matches the rule's ``exclude_where``."""

        if not rule.exclude_where:
            return False
        owners = (self.nodes[key] for key in sorted(self.owners.get(node.key, ())))
        return any(
            ontology.condition_matches(rule.exclude_where, NodeView(item.label, item.attributes))
            for item in (node, *owners)
        )


def _owner_pair(ontology: Ontology, nodes: Mapping[str, SpineNode], relation: str, source: str, target: str) -> bool:
    """Whether ``relation`` from ``source`` to ``target`` (keys of ``nodes``) is on an owner pair."""

    start, end = nodes.get(source), nodes.get(target)
    pair = None if start is None or end is None else ontology.pair_rule(relation, start.label, end.label)
    return pair is not None and pair.owner


async def _inferred_owner_edges(executor: Executor, project_id: str, ontology: Ontology) -> list[_CurrentEdge]:
    """The current edges of every owner relation a rule writes (``_Loaded.inferred`` keeps the inferred ones)."""

    written = {relation for rule in ontology.inference.rules for relation in rule.written}
    return [
        edge
        for relation in sorted(owner_relations(ontology) & written)
        for edge in await _current_edges(executor, project_id, relation)
    ]


async def _load(executor: Executor, project_id: str, ontology: Ontology) -> _Loaded:
    spine = await load_spine_graph(executor, project_id, ontology)
    return _Loaded.of(ontology, spine, await _inferred_owner_edges(executor, project_id, ontology))


async def _loaded(executor: Executor, project_id: str, ontology: Ontology, loaded: _Loaded | None) -> _Loaded:
    return loaded if loaded is not None else await _load(executor, project_id, ontology)


def _bases() -> list[str]:
    return list(READ_BASES)


def _basis(value: Any) -> str:
    return str(value) if value else READ_BASES[0]


async def _current_edges(executor: Executor, project_id: str, relation: str) -> tuple[_CurrentEdge, ...]:
    rows = await executor.read(read_current_edges(relation), project_id)
    return tuple(
        _CurrentEdge(
            uuid=row["props"].get("uuid"),
            edge_key=row["edge_key"],
            source=row["source_uuid"],
            target=row["target_uuid"],
            basis=_basis(row["props"].get("basis")),
            rule=row["props"].get("inference_rule"),
            ref=edge_ref(row.get("source_spec_id") or "", relation, row.get("target_spec_id") or ""),
            relation=relation,
            confidence=row["props"].get("confidence"),
            evidence=row["props"].get("evidence"),
        )
        for row in rows
    )


def _linked(current: Iterable[_CurrentEdge]) -> set[frozenset[str]]:
    """Node pairs linked by a non-inferred edge, either direction."""

    return {frozenset((edge.source, edge.target)) for edge in current if edge.basis != INFERRED}


async def _endpoint_subjects(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    rule: InferenceRule,
    current: Sequence[_CurrentEdge],
    *,
    loaded: _Loaded,
) -> tuple[list[_Subject], list[str]]:
    """Subjects of an ``endpoints`` rule, and the ``spec_id``s of those outside the target bounds;
    nodes owned by a node with a label in ``exclude_owned_by`` are neither."""

    rows = await executor.read(
        infer_endpoint_subjects(
            rule.via, tuple(via for via in rule.via if ontology.is_symmetric(via)), mentions=rule.mentions
        ),
        project_id,
        subject_labels=sorted(rule.subject),
        target_labels=sorted(rule.targets),
        bases=_bases(),
        statuses=sorted(GROUNDING_STATUSES),
    )
    low, high = rule.min_targets or 1, rule.max_targets or 1
    linked = _linked(current)
    subjects: list[_Subject] = []
    outliers: list[str] = []
    for row in rows:
        if rule.exclude_owned_by and loaded.owned_by(row["uuid"], rule.exclude_owned_by):
            continue
        targets = tuple(sorted(set(row["targets"])))
        if not low <= len(targets) <= high:
            outliers.append(row["spec_id"])
            continue
        pairs = tuple(pair for pair in combinations(targets, 2) if frozenset(pair) not in linked)
        if pairs:
            subjects.append(_Subject(row["uuid"], row["spec_id"], candidates=targets, pairs=pairs, targets=targets))
    return subjects, outliers


def _realization_queries(ontology: Ontology, rule: InferenceRule) -> tuple[Query, ...]:
    """The graph queries a ``realization`` rule's subjects were read with, one per container path (``infer_reads`` walks
    the paths in code; the equivalence test runs these).
    """

    member = rule.member_relation or ""
    symmetric = tuple(
        sorted({name for path in rule.container_paths for name in path.relations if ontology.is_symmetric(name)})
    )
    if not rule.container_paths:
        raise ConformanceError("inference_container_paths_empty", rule=rule.id)
    return tuple(
        infer_realization_subjects(
            member,
            container_path(path.relations, sequence=path.sequence, min_hops=path.min_hops, max_hops=path.max_hops),
            symmetric,
            member_symmetric=ontology.is_symmetric(member),
        )
        for path in rule.container_paths
    )


def _realization_subjects(
    ontology: Ontology,
    rule: InferenceRule,
    current: Sequence[_CurrentEdge],
    *,
    loaded: _Loaded,
    overlay: Sequence[SpineEdge] = (),
) -> list[_Subject]:
    """Subjects of a ``realization`` rule, walked over the loaded graph plus ``overlay`` (the planned
    edges of the rules it reads; ``infer_reads``), less the candidates it already has a stated or
    implied edge of the written relation to."""

    rows = infer_reads.realization_rows(ontology, rule, loaded.spine, bases=READ_BASES, overlay=overlay)
    linked = _linked(current)
    subjects: list[_Subject] = []
    for row in rows:
        via = {
            uuid: container.via
            for uuid, container in row.containers.items()
            if frozenset((row.uuid, uuid)) not in linked
        }
        if not via:
            continue
        # Bounded payload: the candidates reached from the most members, then their members first.
        shown = sorted(via, key=lambda uuid: (-len(via[uuid]), uuid))[:MAX_SHOWN]
        members = sorted(set(row.members))
        leading = [uuid for uuid in members if any(uuid in via[candidate] for candidate in shown)]
        kept_members = (leading + [uuid for uuid in members if uuid not in leading])[:MAX_SHOWN]
        subjects.append(
            _Subject(
                row.uuid,
                row.spec_id,
                candidates=tuple(sorted(shown)),
                members=tuple(sorted(kept_members)),
                via={uuid: via[uuid] for uuid in shown},
                truncated=len(shown) < len(via) or len(kept_members) < len(members),
                reads={uuid: row.containers[uuid].reads for uuid in shown if row.containers[uuid].reads},
            )
        )
    return subjects


def _owner_based(ontology: Ontology, rule: InferenceRule) -> bool:
    """Whether an ``attach_orphan`` rule selects by ownership: some subject label has an owner pair."""

    return any(ontology.owner_pairs(label) for label in rule.subject)


def _candidate_labels(ontology: Ontology, rule: InferenceRule, label: str) -> frozenset[str]:
    """The labels an ``attach_orphan`` subject of ``label`` takes candidates from: the rule's targets
    that are targets of the label's owner pairs of the written relation (all of them without owner
    pairs, see ``_owner_based``)."""

    if not _owner_based(ontology, rule):
        return rule.targets
    owners = {pair.target for pair in ontology.owner_pairs(label) if pair.relation == rule.writes}
    return rule.targets & owners


def _excluded_candidates(
    ontology: Ontology, rule: InferenceRule, loaded: _Loaded, node: SpineNode, labels: frozenset[str]
) -> frozenset[str]:
    """The nodes of ``labels`` that are never candidates of the ``attach_orphan`` subject ``node``: those matching the
    rule's ``target_exclude_where`` and those whose pair of the written relation from the subject has a ``source_where``
    or ``target_where`` the two ends do not meet.
    """

    subject = NodeView(node.label, node.attributes)
    excluded: set[str] = set()
    for other in loaded.spine.nodes:
        if other.label not in labels:
            continue
        view = NodeView(other.label, other.attributes)
        pair = ontology.pair_rule(rule.writes, node.label, other.label)
        if ontology.condition_matches(rule.target_exclude_where, view) or (
            pair is not None and not ontology.where_holds(pair, subject, view)
        ):
            excluded.add(other.key)
    return frozenset(excluded)


def _left_out(
    ontology: Ontology, rule: InferenceRule, loaded: _Loaded, orphans: Sequence[tuple[SpineNode, frozenset[str]]]
) -> dict[str, frozenset[str]]:
    """Each orphan's ``_excluded_candidates`` (by orphan uuid), computed once per label, candidate
    labels and attribute values."""

    excluded: dict[tuple[str, frozenset[str], str], frozenset[str]] = {}
    left_out: dict[str, frozenset[str]] = {}
    for node, labels in orphans:
        seen = (node.label, labels, json.dumps(node.attributes, sort_keys=True, default=str))
        if seen not in excluded:
            excluded[seen] = _excluded_candidates(ontology, rule, loaded, node, labels)
        left_out[node.key] = excluded[seen]
    return left_out


async def _similar_candidates(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    rule: InferenceRule,
    *,
    orphans: Sequence[tuple[SpineNode, frozenset[str]]],
    left_out: Mapping[str, frozenset[str]],
) -> dict[str, tuple[str, ...]]:
    """Each orphan's candidates (uuids, most similar first) among its candidate labels, less
    ``left_out`` (``_left_out``): one read per distinct set of labels and exclusions."""

    groups: dict[tuple[frozenset[str], frozenset[str]], list[str]] = {}
    for node, labels in orphans:
        groups.setdefault((labels, left_out[node.key]), []).append(node.key)
    candidates: dict[str, tuple[str, ...]] = {}
    for labels, excluded in sorted(groups, key=lambda key: (sorted(key[0]), sorted(key[1]))):
        rows = await executor.read(
            infer_similar_targets(rule.writes, symmetric=ontology.is_symmetric(rule.writes)),
            project_id,
            uuids=groups[(labels, excluded)],
            target_labels=sorted(labels),
            excluded=sorted(excluded),
            bases=_bases(),
            limit=rule.max_candidates or 1,
        )
        candidates.update({row["uuid"]: tuple(item["uuid"] for item in row["candidates"]) for row in rows})
    return candidates


_SECTION_NUMBER: Final = re.compile(r"^(?:\d+(?:\.\d+)*\.?|[A-Z]\.(?:\d+(?:\.\d+)*\.?)?)\s+")
"""A heading's section number ("6.", "19.4", "A.2"), left out of its title."""


def _title_forms(title: str) -> frozenset[tuple[str, ...]]:
    """A heading title in ``hub_words`` form, without its section number, and also without a ``…:``
    prefix when it has one ("Table 6.4: Requirements")."""

    bare = _SECTION_NUMBER.sub("", title.strip(), count=1)
    forms = {tuple(hub_words(bare))}
    if ":" in bare:
        forms.add(tuple(hub_words(bare.rsplit(":", 1)[1])))
    return frozenset(form for form in forms if form)


CONTAINED_WORDS: Final = 2
"""A hub name of at least this many words names a heading whose title contains it (``_section_hubs``)."""


def _named_in(title: str, named: Mapping[tuple[str, ...], Sequence[_Node]]) -> list[_Node]:
    """The nodes ``title`` names: those whose whole name equals it (``_title_forms``), else those whose
    name of at least ``CONTAINED_WORDS`` words it contains as consecutive words."""

    exact = [node for form in _title_forms(title) for node in named.get(form, ())]
    if exact:
        return exact
    words = f" {' '.join(hub_words(title))} "
    return [
        node
        for name, nodes in named.items()
        if len(name) >= CONTAINED_WORDS and f" {' '.join(name)} " in words
        for node in nodes
    ]


async def _section_hubs(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    *,
    loaded: _Loaded,
    orphans: Sequence[tuple[SpineNode, frozenset[str]]],
    left_out: Mapping[str, frozenset[str]],
) -> dict[str, str]:
    """Each orphan's section hub (by orphan uuid), an extra candidate beside the most similar ones: the headings of the
    section the orphan sits in (its first grounding fragment's section at its first evidence line, without the document
    title) are read nearest first, and the first heading that names a node of its candidate labels, less ``left_out``
    (``_named_in``: by whole name, no alias, else by a name of several words it contains), decides: its one node, or
    none when it names several.
    """

    wanted = frozenset(label for _, labels in orphans for label in labels)
    hubs = [node.key for node in loaded.spine.nodes if node.label in wanted]
    nodes = await _nodes(executor, project_id, ontology, [*hubs, *(node.key for node, _ in orphans)])
    named: dict[tuple[str, ...], list[_Node]] = {}
    for key in hubs:
        hub = nodes.get(key)
        if hub is not None:
            named.setdefault(tuple(hub_words(hub.name)), []).append(hub)
    found: dict[str, str] = {}
    for orphan, labels in orphans:
        node = nodes.get(orphan.key)
        headings = node.sections[0].split(BREADCRUMB_SEPARATOR)[1:] if node is not None and node.sections else []
        for title in reversed(headings):
            matches = {
                hub.uuid
                for hub in _named_in(title, named)
                if hub.label in labels and hub.uuid not in left_out[orphan.key]
            }
            if len(matches) == 1:
                found[orphan.key] = next(iter(matches))
            if matches:
                break
    return found


async def _orphan_subjects(
    executor: Executor, project_id: str, ontology: Ontology, rule: InferenceRule, loaded: _Loaded
) -> tuple[list[_Subject], list[str]]:
    """Subjects of an ``attach_orphan`` rule, and the ``spec_id``s of orphans without a candidate;
    ``loaded``: the graph read at the start of inference (selection writes nothing). Candidates leave
    out ``_excluded_candidates``."""

    if _owner_based(ontology, rule):
        found = sorted(
            (
                node
                for node in loaded.spine.nodes
                if node.label in rule.subject
                and not loaded.owners.get(node.key)
                and not loaded.inferred_owners(node.key, rule=rule.id, other=True)
            ),
            key=lambda node: (node.spec_id, node.key),
        )
    else:
        found = loaded.spine.unreached(ontology, rule.subject, READ_BASES)
    orphans: list[tuple[SpineNode, frozenset[str]]] = []
    for node in found:
        labels = _candidate_labels(ontology, rule, node.label)
        if labels and not loaded.excluded(ontology, rule, node):
            orphans.append((node, labels))
    exclusive = sorted(
        [source, target] for relation, source, target in ontology.exclusive_pairs if relation == rule.writes
    )
    if orphans and exclusive:
        held = await executor.read(
            infer_exclusive_holders(rule.writes),
            project_id,
            uuids=[node.key for node, _ in orphans],
            pairs=exclusive,
            bases=_bases(),
        )
        holders = {row["uuid"] for row in held}
        orphans = [(node, labels) for node, labels in orphans if node.key not in holders]
    if not orphans:
        return [], []
    left_out = _left_out(ontology, rule, loaded, orphans)
    candidates = await _similar_candidates(executor, project_id, ontology, rule, orphans=orphans, left_out=left_out)
    sections = await _section_hubs(executor, project_id, ontology, loaded=loaded, orphans=orphans, left_out=left_out)
    subjects: list[_Subject] = []
    without: list[str] = []
    for node, _ in orphans:
        section = sections.get(node.key)
        uuids = tuple(dict.fromkeys((*candidates.get(node.key, ()), *((section,) if section else ()))))
        if uuids:
            subjects.append(_Subject(node.key, node.spec_id, candidates=uuids))
        else:
            without.append(node.spec_id)
    return subjects, without


async def _cross_document_hubs(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    rule: InferenceRule,
    *,
    current: tuple[_CurrentEdge, ...],
    loaded: _Loaded,
) -> _Selection:
    """The hubs of a ``cross_document`` rule (``infer_xdoc``), without the members that, or whose
    owner, match ``exclude_where``; each subject's ``targets`` are its members. Its pairs are chosen
    when the rule is judged (``_cross_document_pairs``), after the earlier rules."""

    def keep(uuid: str) -> bool:
        node = loaded.nodes.get(uuid)
        return node is not None and not loaded.excluded(ontology, rule, node)

    hubs = await infer_xdoc.read_hubs(executor, project_id, rule, bases=_bases(), statuses=sorted(GROUNDING_STATUSES))
    kept = infer_xdoc.candidate_hubs(rule, hubs, keep)
    subjects = tuple(_Subject(hub.uuid, hub.spec_id, targets=tuple(sorted(hub.members))) for hub in kept)
    documents = {uuid: found for hub in kept for uuid, found in hub.members.items()}
    return _Selection(rule, current, subjects, documents=documents)


def _owners_now(ontology: Ontology, loaded: _Loaded, plans: Iterable[_RulePlan]) -> dict[str, set[str]]:
    """Each node's owners: its stated and implied owner edges, the owner edges the ``plans`` of earlier
    rules of this build accepted, and the inferred owner edges of earlier builds (any rule)."""

    owners = {key: set(found) for key, found in loaded.owners.items()}
    for key in loaded.inferred:
        owners.setdefault(key, set()).update(loaded.inferred_owners(key))
    for plan in plans:
        for proposal in plan.accepted.values():
            if _owner_pair(ontology, loaded.nodes, proposal.relation, proposal.source, proposal.target):
                owners.setdefault(proposal.source, set()).add(proposal.target)
    return owners


def _stale(
    ontology: Ontology, selection: _Selection, loaded: _Loaded, owners: Mapping[str, set[str]]
) -> frozenset[str]:
    """Keys of a ``cross_document`` rule's inferred edges a deferred subject does not keep: ends now linked by a stated
    or implied edge of a relation it writes, or an owner edge whose member now has an owner other than the rule's own
    inferred owner edges of earlier builds (``owners``).
    """

    rule = selection.rule
    linked = _linked(selection.current)
    stale: set[str] = set()
    for edge in selection.current:
        if edge.basis != INFERRED or edge.rule != rule.id:
            continue
        owned = _owner_pair(ontology, loaded.nodes, edge.relation, edge.source, edge.target)
        others = owners.get(edge.source, set()) - loaded.inferred_owners(edge.source, rule=rule.id)
        if frozenset((edge.source, edge.target)) in linked or (owned and others):
            stale.add(edge.edge_key)
    return frozenset(stale)


def _subject_owners(
    subject: _Subject, owners: Mapping[str, set[str]], own: Mapping[str, set[str]]
) -> dict[str, set[str]]:
    """The owners of a ``cross_document`` subject's members as the subject sees them: ``owners``, less each member's own
    inferred owner edges of earlier builds (``own``: of this rule) that this subject could have given it (to the hub, or
    to the owner of another of its members), so that the subject judges that owner again instead of seeing the member as
    owned.
    """

    held = Counter(target for member in subject.targets for target in owners.get(member, ()))
    view: dict[str, set[str]] = {}
    for member in subject.targets:
        found = owners.get(member, set())
        mine = {
            target for target in own.get(member, ()) if target == subject.uuid or held[target] > int(target in found)
        }
        if found:
            view[member] = found - mine
    return view


async def _cross_document_pairs(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    selection: _Selection,
    *,
    loaded: _Loaded,
    owners: Mapping[str, set[str]],
) -> _Selection:
    """``selection`` (``_cross_document_hubs``) with each subject's pairs, owner candidates, member owners and document
    titles (``infer_xdoc``), ``owners`` counting the earlier rules' accepted owner edges; a hub left without a pair is
    no subject.
    """

    rule = selection.rule
    linked = _linked(selection.current)
    ordered = sorted(selection.subjects, key=lambda subject: (-len(subject.targets), subject.spec_id))
    own = {key: loaded.inferred_owners(key, rule=rule.id) for key in loaded.inferred}
    views = {subject.uuid: _subject_owners(subject, owners, own) for subject in ordered}
    open_pairs = {
        subject.uuid: infer_xdoc.open_pairs(
            subject.targets, selection.documents, linked=linked, owners=views[subject.uuid]
        )
        for subject in ordered
    }
    embeddings = await infer_xdoc.member_embeddings(
        executor, project_id, {uuid for pairs in open_pairs.values() for pair in pairs for uuid in pair}
    )
    labels = {key: node.label for key, node in loaded.nodes.items()}
    spec_ids = {key: node.spec_id for key, node in loaded.nodes.items()}
    chosen: list[tuple[_Subject, infer_xdoc.Choice]] = []
    taken: set[frozenset[str]] = set()
    duplicates = 0
    for subject in ordered:
        pairs = [pair for pair in open_pairs[subject.uuid] if frozenset(pair) not in taken]
        duplicates += len(open_pairs[subject.uuid]) - len(pairs)
        choice = infer_xdoc.choose(
            ontology,
            rule,
            subject.uuid,
            pairs,
            infer_xdoc.pair_cosines(pairs, embeddings),
            owners=views[subject.uuid],
            labels=labels,
            spec_ids=spec_ids,
        )
        if choice is not None:
            chosen.append((subject, choice))
            taken.update(frozenset(pair) for pair in choice.pairs)
    titles = await infer_xdoc.document_titles(
        executor,
        project_id,
        (document for _, choice in chosen for member in choice.members for document in selection.documents[member]),
    )

    def first_owner(found: Iterable[str]) -> str:
        return min(found, key=lambda uuid: (spec_ids.get(uuid, ""), uuid))

    subjects = tuple(
        replace(
            subject,
            pairs=choice.pairs,
            members=choice.members,
            candidates=tuple(sorted({uuid for found in choice.candidates.values() for uuid in found})),
            via=dict(choice.candidates),
            owners={
                member: first_owner(views[subject.uuid][member])
                for member in choice.members
                if views[subject.uuid].get(member)
            },
            documents={
                member: infer_xdoc.document_label(selection.documents[member], titles) for member in choice.members
            },
        )
        for subject, choice in chosen
    )
    return replace(
        selection, subjects=subjects, stale=_stale(ontology, selection, loaded, owners), duplicate_pairs=duplicates
    )


async def _rule_current(executor: Executor, project_id: str, rule: InferenceRule) -> tuple[_CurrentEdge, ...]:
    """The current edges of every relation ``rule`` writes (``InferenceRule.written`` order)."""

    return tuple([edge for relation in rule.written for edge in await _current_edges(executor, project_id, relation)])


async def _select(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    rule: InferenceRule,
    loaded: _Loaded | None = None,
    *,
    current: tuple[_CurrentEdge, ...] | None = None,
    overlay: Sequence[SpineEdge] = (),
) -> _Selection:
    """One rule's selection over the graph read at the start of inference (``loaded``; default: read
    now) and ``current``, the current edges of the relations it writes (default: read now).
    ``overlay``: the edges of this build's plan a rule with ``reads`` sees (``_overlay``)."""

    if current is None:
        current = await _rule_current(executor, project_id, rule)
    if rule.template == ENDPOINTS:
        graph = await _loaded(executor, project_id, ontology, loaded)
        subjects, outliers = await _endpoint_subjects(executor, project_id, ontology, rule, current, loaded=graph)
        return _Selection(rule, current, tuple(subjects), outliers=tuple(outliers))
    if rule.template == REALIZATION:
        graph = await _loaded(executor, project_id, ontology, loaded)
        subjects = _realization_subjects(ontology, rule, current, loaded=graph, overlay=overlay)
        return _Selection(rule, current, tuple(subjects))
    if rule.template == ATTACH_ORPHAN:
        graph = await _loaded(executor, project_id, ontology, loaded)
        subjects, without = await _orphan_subjects(executor, project_id, ontology, rule, graph)
        return _Selection(rule, current, tuple(subjects), without_candidate=tuple(without))
    if rule.template == CROSS_DOCUMENT:
        graph = await _loaded(executor, project_id, ontology, loaded)
        return await _cross_document_hubs(executor, project_id, ontology, rule, current=current, loaded=graph)
    raise ConformanceError("inference_template_unknown", template=rule.template)


async def _nodes(executor: Executor, project_id: str, ontology: Ontology, uuids: Iterable[str]) -> dict[str, _Node]:
    wanted = sorted(set(uuids))
    if not wanted:
        return {}
    rows = await executor.read(
        READ_INFER_NODES,
        project_id,
        uuids=wanted,
        labels=list(ontology.labels),
        statuses=sorted(GROUNDING_STATUSES),
        per_node=SUBJECT_FRAGMENTS,
        quote=QUOTE_ATTRIBUTE,
    )
    nodes: dict[str, _Node] = {}
    for row in rows:
        labels = row["labels"] or []
        if len(labels) != 1 or not row["identity_key"] or not row["spec_id"]:
            continue  # not a valid spec node (verification reports it); never judged or linked
        grounding = [item for item in row["fragments"] or () if item.get("fragment_id")]
        nodes[row["uuid"]] = _Node(
            uuid=row["uuid"],
            spec_id=row["spec_id"],
            name=row["name"],
            label=labels[0],
            identity_key=row["identity_key"],
            statements=_statement_texts(row["statements"]),
            fragments=tuple((item["fragment_id"], item.get("content_hash") or "") for item in grounding),
            sections=tuple(
                heading_section(item.get("heading_path") or (), item.get("headings") or ()) for item in grounding
            ),
            documents=tuple(item.get("document_id") or "" for item in grounding),
            quote=row.get("quote") or "",
        )
    return nodes


def _statement_texts(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    texts: list[str] = []
    for item in value if isinstance(value, list) else ():
        text = item.get("text") if isinstance(item, dict) else None
        if isinstance(text, str) and text.strip() and text not in texts:
            texts.append(text)
        if len(texts) == STATEMENTS_SHOWN:
            break
    return tuple(texts)


# Judgement


def _shown(node: _Node, local_id: str | None = None) -> dict[str, Any]:
    shown: dict[str, Any] = {} if local_id is None else {"id": local_id}
    shown.update({"name": node.name, "label": node.label, "statements": list(node.statements)})
    return shown


def _owner_shown(node: _Node, local_id: str | None = None) -> dict[str, Any]:
    """An ``attach_orphan`` subject or candidate as shown: ``_shown`` with ``sections``, the distinct
    sections it sits in within its first ``SECTIONS_SHOWN`` grounding fragments."""

    sections = dict.fromkeys(section for section in node.sections[:SECTIONS_SHOWN] if section)
    return {**_shown(node, local_id), "sections": list(sections)}


def _fragment_refs(subject: _Node, others: Sequence[_Node]) -> dict[str, str]:
    """Fragment id to content hash of the fragments a judgement shows, in order."""

    chosen: dict[str, str] = {}
    for node, count in ((subject, SUBJECT_FRAGMENTS), *((other, OTHER_FRAGMENTS) for other in others)):
        for fragment_id, content_hash in node.fragments[:count]:
            if len(chosen) == MAX_FRAGMENTS:
                return chosen
            chosen.setdefault(fragment_id, content_hash)
    return chosen


@dataclass(frozen=True)
class _Judgement:
    """A judgement's payload (without ``fragments``), the fragments it shows (id to content hash),
    the local ids it shows (local id to node uuid) and the hash of all its inputs."""

    payload: dict[str, Any]
    fragments: Mapping[str, str]
    ids: Mapping[str, str]
    digest: str


_Item = tuple[_Subject, _Judgement]
"""A fresh subject with its judgement."""


CANDIDATES: Final = "candidates"
"""The payload field of an ``attach_orphan`` judgement's candidates."""
HASH_LEFT_OUT: Final = frozenset({"statements", "sections"})
"""Payload fields the judgement input hash leaves out: a node's statements and sections are re-extracted texts that a
re-extraction rewords while its quote, name and fragments stay; the hash keys every shown node by identity key, label,
name and quote (``_hashed_node``) and the fragments by content hash.
"""


def _hashed_node(node: _Node | None, uuid: str) -> list[str]:
    return [uuid] if node is None else [node.identity_key, node.label, node.name, node.quote]


def _hashed_payload(value: Any) -> Any:
    """``value`` without the ``HASH_LEFT_OUT`` fields, at any depth."""

    if isinstance(value, dict):
        return {key: _hashed_payload(item) for key, item in value.items() if key not in HASH_LEFT_OUT}
    if isinstance(value, list):
        return [_hashed_payload(item) for item in value]
    return value


def _digested(
    rule: InferenceRule,
    subject: _Subject,
    judge_model: str,
    *,
    payload: dict[str, Any],
    fragments: Mapping[str, str],
    ids: Mapping[str, str],
    nodes: Mapping[str, _Node],
) -> _Judgement:
    """The judgement with the hash of its inputs: prompt, judge model, subject, the payload less ``HASH_LEFT_OUT``, the
    fragments by content hash and every shown node (the subject and those of ``ids``) by identity key, label, name and
    quote (``_hashed_node``), so a reworded statement or ``stmt`` of an unchanged node keeps the hash while a changed
    quote, name or fragment changes it.
    """

    hashed = _hashed_payload(payload)
    shown = sorted(_hashed_node(nodes.get(uuid), uuid) for uuid in {subject.uuid, *ids.values()})
    order_free = rule.template == ATTACH_ORPHAN and isinstance(hashed.get(CANDIDATES), list)
    if order_free:
        hashed[CANDIDATES] = sorted(
            json.dumps({key: item for key, item in candidate.items() if key != "id"}, sort_keys=True)
            for candidate in hashed[CANDIDATES]
        )
    inputs: dict[str, Any] = {
        "prompt": rule.prompt,
        "judge_model": judge_model,
        "subject": subject.uuid,
        "payload": hashed,
        "fragments": sorted(fragments.items()),
        "ids": sorted(ids.values()) if order_free else sorted(ids.items()),
        "nodes": shown,
    }
    reads = {local_id: list(subject.reads[uuid]) for local_id, uuid in ids.items() if subject.reads.get(uuid)}
    if reads:
        inputs["reads"] = reads
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return _Judgement(payload, fragments, ids, digest)


def _xdoc_shown(node: _Node) -> infer_xdoc.Shown:
    return infer_xdoc.Shown(node.uuid, _shown(node), node.fragments)


def _cross_document_judgement(
    ontology: Ontology, rule: InferenceRule, subject: _Subject, nodes: Mapping[str, _Node], judge_model: str
) -> _Judgement | None:
    """A ``cross_document`` subject's judgement (payload: ``infer_xdoc.judgement_parts``); None when
    its hub or every pair cannot be shown."""

    hub = nodes.get(subject.uuid)
    if hub is None:
        return None
    members = sorted((nodes[uuid] for uuid in subject.members if uuid in nodes), key=lambda n: (n.spec_id, n.uuid))
    view = infer_xdoc.View(
        hub=_xdoc_shown(hub),
        members=[_xdoc_shown(member) for member in members],
        pairs=subject.pairs,
        documents=subject.documents,
        owners={member: nodes[owner].name for member, owner in subject.owners.items() if owner in nodes},
        candidates={
            member: [_xdoc_shown(nodes[uuid]) for uuid in found if uuid in nodes]
            for member, found in subject.via.items()
        },
        labels={uuid: node.label for uuid, node in nodes.items()},
    )
    parts = infer_xdoc.judgement_parts(ontology, rule, view)
    if parts is None:
        return None
    return _digested(
        rule, subject, judge_model, payload=parts.payload, fragments=parts.fragments, ids=parts.ids, nodes=nodes
    )


def _judgement(
    rule: InferenceRule,
    subject: _Subject,
    nodes: Mapping[str, _Node],
    judge_model: str,
    *,
    ontology: Ontology | None = None,
) -> _Judgement | None:
    """The subject's judgement; None when its nodes cannot be shown. A ``cross_document`` subject
    needs the ``ontology`` (none: not shown)."""

    if rule.template == CROSS_DOCUMENT:
        return None if ontology is None else _cross_document_judgement(ontology, rule, subject, nodes, judge_model)
    node = nodes.get(subject.uuid)
    if node is None:
        return None
    payload: dict[str, Any] = {
        "relation": rule.writes,
        "subject": _owner_shown(node) if rule.template == ATTACH_ORPHAN else _shown(node),
    }
    ids: dict[str, str] = {}
    others: list[_Node] = []
    if rule.template == ENDPOINTS:
        targets = sorted((nodes[uuid] for uuid in subject.targets if uuid in nodes), key=lambda n: n.spec_id)
        ids = {f"T{index}": target.uuid for index, target in enumerate(targets, 1)}
        local = {uuid: local_id for local_id, uuid in ids.items()}
        pairs = [sorted((local[a], local[b])) for a, b in subject.pairs if a in local and b in local]
        if not pairs:
            return None
        payload["targets"] = [_shown(target, local[target.uuid]) for target in targets]
        payload["candidate_pairs"] = sorted(pairs)
        others = targets
    elif rule.template == REALIZATION:
        members = sorted((nodes[uuid] for uuid in subject.members if uuid in nodes), key=lambda n: n.spec_id)
        member_ids = {member.uuid: f"M{index}" for index, member in enumerate(members, 1)}
        candidates = sorted((nodes[uuid] for uuid in subject.candidates if uuid in nodes), key=lambda n: n.spec_id)
        if not candidates:
            return None
        ids = {f"C{index}": candidate.uuid for index, candidate in enumerate(candidates, 1)}
        payload["members"] = [_shown(member, member_ids[member.uuid]) for member in members]
        payload["candidates"] = [
            {
                **_shown(candidate, local_id),
                "via": [member_ids[uuid] for uuid in subject.via.get(candidate.uuid, ()) if uuid in member_ids],
            }
            for local_id, candidate in zip(ids, candidates, strict=True)
        ]
        others = [*members, *candidates]
        ids.update({local_id: uuid for uuid, local_id in member_ids.items()})
    else:
        candidates = [nodes[uuid] for uuid in subject.candidates if uuid in nodes]  # most similar first
        if not candidates:
            return None
        ids = {f"C{index}": candidate.uuid for index, candidate in enumerate(candidates, 1)}
        payload["candidates"] = [
            _owner_shown(candidate, local_id) for local_id, candidate in zip(ids, candidates, strict=True)
        ]
        others = candidates
    return _digested(
        rule, subject, judge_model, payload=payload, fragments=_fragment_refs(node, others), ids=ids, nodes=nodes
    )


def _both_ways(pairs: Iterable[tuple[str, str]]) -> set[frozenset[str]]:
    """The node pairs ``pairs`` links in both directions."""

    directed = set(pairs)
    return {frozenset(pair) for pair in directed if (pair[1], pair[0]) in directed}


def _endpoint_pairs(
    subject: _Subject, verdict: EndpointsVerdict, ids: Mapping[str, str]
) -> list[tuple[str, str]] | None:
    """The pairs an ``endpoints`` verdict links: only candidate pairs, one direction each; None when it
    names an unknown id or a non-candidate pair, or when nothing is left after dropping both-way pairs."""

    allowed = {frozenset(pair) for pair in subject.pairs}
    pairs: list[tuple[str, str]] = []
    for link in verdict.links:
        source, target = ids.get(link.source), ids.get(link.target)
        if source is None or target is None or source == target or frozenset((source, target)) not in allowed:
            return None
        pairs.append((source, target))
    both = _both_ways(pairs)
    if both:
        pairs = [pair for pair in pairs if frozenset(pair) not in both]
        if not pairs:
            return None
    return list(dict.fromkeys(pairs))


def _realization_pairs(
    subject: _Subject, verdict: RealizationVerdict, ids: Mapping[str, str]
) -> list[tuple[str, str]] | None:
    """The pairs from the subject to each container a ``realization`` verdict chose; None on a non-candidate."""

    chosen = [ids.get(local_id) for local_id in dict.fromkeys(verdict.targets)]
    if any(uuid is None or uuid not in subject.candidates for uuid in chosen):
        return None
    return [(subject.uuid, uuid) for uuid in chosen if uuid is not None]


def _proposed_pairs(
    rule: InferenceRule, subject: _Subject, verdict: Verdict, ids: Mapping[str, str]
) -> list[tuple[str, str]] | None:
    """The ``(source uuid, target uuid)`` pairs a verdict proposes; None when it names an id it was not
    given or a pair that is not a candidate. An ``endpoints`` pair linked both ways is invalid: neither
    link is proposed, and a verdict left without a link is None."""

    if isinstance(verdict, EndpointsVerdict):
        return _endpoint_pairs(subject, verdict, ids)
    if isinstance(verdict, RealizationVerdict):
        return _realization_pairs(subject, verdict, ids)
    if isinstance(verdict, AttachOrphanVerdict):
        if verdict.target is None:
            return []
        uuid = ids.get(verdict.target)
        return None if uuid is None else [(subject.uuid, uuid)]
    raise ConformanceError("inference_verdict_type_unknown", template=rule.template)


def _stored_reversed(ontology: Ontology, relation: str, source: _Node, target: _Node) -> bool:
    """Whether the edge is stored from ``target`` to ``source``: a symmetric relation puts the lower
    identity key first."""

    return ontology.is_symmetric(relation) and target.identity_key < source.identity_key


def _proposal(
    ontology: Ontology,
    relation: str,
    source: _Node,
    target: _Node,
    *,
    confidence: float,
    evidence: Sequence[tuple[str, str]],
    reused: bool = False,
) -> _Proposal:
    """The edge as stored (``_stored_reversed``); whether it may be written is the shared validator's
    call (``_Validator``)."""

    if _stored_reversed(ontology, relation, source, target):
        source, target = target, source
    return _Proposal(
        relation=relation,
        source=source.uuid,
        target=target.uuid,
        edge_key=f"{source.identity_key}|{relation}|{target.identity_key}",
        ref=edge_ref(source.spec_id, relation, target.spec_id),
        confidence=float(confidence),
        evidence=tuple(evidence),
        reused=reused,
    )


class _Validator:
    """The shared validator (``ProposalChecker``) over the graph inference reads plus the proposals
    accepted so far. A verdict's edges are tried together on a copy of the checker; the copy replaces
    it only when the verdict is accepted (``commit``)."""

    def __init__(
        self,
        checker: ProposalChecker,
        *,
        nodes: Sequence[ViewNode] = (),
        base: Sequence[ViewEdge] = (),
        ontology: Ontology | None = None,
    ) -> None:
        self._checker = checker
        self._nodes = tuple(nodes)
        self._base = tuple(base)
        self._ontology = ontology
        self._held: tuple[ViewEdge, ...] = ()

    @classmethod
    def over(cls, ontology: Ontology, loaded: _Loaded, current: Iterable[_CurrentEdge]) -> _Validator:
        """The spec nodes with their condition attributes and the stated and implied edges of the owner
        relations (spine graph) and of every written relation (``current``: the current edges of
        every relation a rule writes)."""

        written = {relation for rule in ontology.inference.rules for relation in rule.written}
        relations = owner_relations(ontology) | written
        edges = {
            ViewEdge(edge.relation, edge.source, edge.target, _basis(edge.basis))
            for edge in loaded.spine.edges
            if edge.relation in relations and _basis(edge.basis) in READ_BASES
        }
        edges.update(
            ViewEdge(edge.relation, edge.source, edge.target, edge.basis)
            for edge in current
            if edge.basis in READ_BASES
        )
        nodes = [ViewNode(node.key, node.label, node.attributes) for node in loaded.spine.nodes]
        ordered = sorted(edges, key=_view_order)
        checker = ProposalChecker(InMemoryGraphView(nodes, ordered, ontology), ontology)
        return cls(checker, nodes=nodes, base=ordered, ontology=ontology)

    @classmethod
    def bare(cls, ontology: Ontology, nodes: Mapping[str, _Node]) -> _Validator:
        """Over ``nodes`` (labels only) and no edge: the pair checks alone."""

        view = InMemoryGraphView((ViewNode(node.uuid, node.label) for node in nodes.values()), (), ontology)
        return cls(ProposalChecker(view, ontology))

    def trial(self, rule: str, proposals: Sequence[_Proposal]) -> tuple[ProposalChecker, list[_Proposal]]:
        """A checker with ``proposals`` (inferred edges of ``rule``) offered in order, and those it refused."""

        checker = copy.copy(self._checker)  # offer rebinds the copy's state; the original is untouched
        refused = [
            proposal
            for proposal in proposals
            if checker.offer(ViewEdge(proposal.relation, proposal.source, proposal.target, INFERRED, rule))
        ]
        return checker, refused

    def commit(self, checker: ProposalChecker) -> None:
        self._checker = checker

    def hold(self, edges: Iterable[ViewEdge]) -> None:
        """Start over from the graph inference reads plus ``edges``, the inferred edges the swap keeps
        beside the edges of the rule about to be judged (``_held``); a validator without that graph
        (``bare``) is left as it is."""

        self._held = tuple(sorted(set(edges), key=_view_order))
        self.hold_also(())

    def hold_also(self, edges: Iterable[ViewEdge]) -> None:
        """Start over from the graph inference reads, the ``hold`` edges and ``edges`` (the inferred
        edges of the rule being judged that its subjects not judged keep, ``_kept_edges``)."""

        if self._ontology is None:
            return
        more = sorted(set(edges) - set(self._held), key=_view_order)
        view = InMemoryGraphView(self._nodes, (*self._base, *self._held, *more), self._ontology)
        self._checker = ProposalChecker(view, self._ontology)

    def keep(self, edges: Iterable[ViewEdge]) -> None:
        """Add ``edges`` unchecked: the kept edges of a subject whose judgement failed."""

        checker = copy.copy(self._checker)  # accept rebinds the copy's state; a snapshot stays as it was
        for edge in edges:
            checker.accept(edge)
        self._checker = checker

    def snapshot(self) -> ProposalChecker:
        return self._checker


def _view_order(edge: ViewEdge) -> tuple[str, str, str, str, str]:
    return (edge.relation, edge.source, edge.target, edge.basis or "", edge.rule or "")


def _view_edge(edge: SpineEdge | _CurrentEdge, rule: str) -> ViewEdge:
    return ViewEdge(edge.relation, edge.source, edge.target, INFERRED, rule)


class _Evaluation(NamedTuple):
    """A verdict's outcome, the edges it adds (``accepted`` only) and the edges the shared validator
    refused (``invalid`` only)."""

    outcome: Outcome
    proposals: tuple[_Proposal, ...] = ()
    refused: tuple[_Proposal, ...] = ()


def _relocated(
    rule: InferenceRule,
    subject: _Subject,
    judgement: _Judgement,
    verdict: Verdict,
    *,
    nodes: Mapping[str, _Node],
    texts: Mapping[str, str],
) -> Verdict:
    """An ``attach_orphan`` verdict with each quote cited from a fragment that holds it: a quote its cited fragment does
    not hold (a garbled fragment id, or a quote taken from another shown fragment) is cited from the first shown
    fragment of the subject, else of the chosen candidate, that holds it; other quotes, and other verdicts, are left as
    they are.
    """

    if rule.template != ATTACH_ORPHAN or not isinstance(verdict, AttachOrphanVerdict) or verdict.target is None:
        return verdict
    ends = (nodes.get(subject.uuid), nodes.get(judgement.ids.get(verdict.target, "")))
    shown = [fragment_id for end in ends if end is not None for fragment_id, _ in end.fragments if fragment_id in texts]
    evidence: list[Evidence] = []
    for item in verdict.evidence:
        cited = texts.get(item.fragment_id)
        home = (
            None
            if cited is not None and quote_in(item.quote, cited)
            else next((fragment_id for fragment_id in shown if quote_in(item.quote, texts[fragment_id])), None)
        )
        evidence.append(item if home is None else Evidence(fragment_id=home, quote=item.quote))
    if evidence == list(verdict.evidence):
        return verdict
    return verdict.model_copy(update={"evidence": evidence})


def _evaluate(
    ontology: Ontology,
    rule: InferenceRule,
    subject: _Subject,
    judgement: _Judgement,
    verdict: Verdict,
    *,
    nodes: Mapping[str, _Node],
    texts: Mapping[str, str],
    validator: _Validator | None = None,
) -> _Evaluation:
    """The verdict's outcome (module doc). ``validator``: the build's shared validator, whose graph
    takes the edges of an accepted verdict; by default one over ``nodes`` alone."""

    pairs = _proposed_pairs(rule, subject, verdict, judgement.ids)
    if pairs is None:
        return _Evaluation("invalid")
    if not pairs:
        return _Evaluation("declined")
    verdict = _relocated(rule, subject, judgement, verdict, nodes=nodes, texts=texts)
    evidence = [(item.fragment_id, item.quote) for item in verdict.evidence]
    proposals = tuple(
        _proposal(
            ontology,
            rule.writes,
            nodes[source_uuid],
            nodes[target_uuid],
            confidence=verdict.confidence,
            evidence=evidence,
        )
        for source_uuid, target_uuid in pairs
    )
    validator = validator or _Validator.bare(ontology, nodes)
    checker, refused = validator.trial(rule.id, proposals)
    if refused:
        return _Evaluation("invalid", refused=tuple(refused))
    if not meets_floor(verdict, ontology.inference.floor_of(rule)) or not quotes_valid(verdict, texts):
        return _Evaluation("rejected")
    validator.commit(checker)
    return _Evaluation("accepted", proposals)


def _stored(proposals: Sequence[_Proposal]) -> list[dict[str, Any]]:
    return [
        {
            "relation": p.relation,
            "source": p.source,
            "target": p.target,
            "confidence": p.confidence,
            "evidence": [list(e) for e in p.evidence],
        }
        for p in proposals
    ]


def _memory_entry(
    outcome: Outcome, proposals: Sequence[_Proposal], refused: Sequence[_Proposal] = ()
) -> dict[str, Any]:
    entry: dict[str, Any] = {"outcome": outcome, "edges": _stored(proposals)}
    if refused:
        entry[REFUSED] = _stored(refused)
    return entry


def _read_edges(
    ontology: Ontology, rule: InferenceRule, edges: Any, nodes: Mapping[str, _Node]
) -> list[_Proposal] | None:
    """Stored edges as reused proposals; None when one does not read back. An edge stored without a
    relation is one of the rule's ``writes``; one of a relation the rule no longer writes does not read
    back."""

    if not isinstance(edges, list):
        return None
    proposals: list[_Proposal] = []
    for item in edges:
        if not isinstance(item, dict):
            return None
        source, target = (nodes.get(item.get("source", "")), nodes.get(item.get("target", "")))
        confidence, evidence = item.get("confidence"), item.get("evidence")
        relation = item.get("relation", rule.writes)
        if (
            source is None
            or target is None
            or relation not in rule.written
            or not isinstance(confidence, int | float)
            or not isinstance(evidence, list)
        ):
            return None
        quotes = [(str(e[0]), str(e[1])) for e in evidence if isinstance(e, list) and len(e) == QUOTE_FIELDS]
        proposals.append(
            _proposal(ontology, relation, source, target, confidence=confidence, evidence=quotes, reused=True)
        )
    return proposals


def _recalled(
    ontology: Ontology, rule: InferenceRule, entry: Mapping[str, Any], nodes: Mapping[str, _Node]
) -> list[_Proposal] | None:
    """The accepted edges of a stored judgement; None when it does not read back."""

    if entry.get("outcome") not in OUTCOMES:
        return None
    proposals = _read_edges(ontology, rule, entry.get("edges"), nodes)
    if proposals is None:
        return None
    if rule.template == ENDPOINTS and _both_ways((p.source, p.target) for p in proposals):
        return None  # stored before one direction per pair: judged again
    return proposals


def _recalled_refused(
    ontology: Ontology, rule: InferenceRule, entry: Mapping[str, Any], nodes: Mapping[str, _Node]
) -> list[_Proposal]:
    """The edges a stored ``invalid`` judgement had refused by the validator (none when unreadable)."""

    return _read_edges(ontology, rule, entry.get(REFUSED, []), nodes) or []


def _quarantined(proposal: _Proposal, nodes: Mapping[str, _Node]) -> QuarantinedEdge:
    source, target = nodes[proposal.source], nodes[proposal.target]
    return QuarantinedEdge(
        reason=INVALID_PROPOSAL,
        relation=proposal.relation,
        source_label=source.label,
        target_label=target.label,
        basis=INFERRED,
        fragment_ids=tuple(dict.fromkeys(fragment_id for fragment_id, _ in proposal.evidence)),
        source_spec_id=source.spec_id,
        target_spec_id=target.spec_id,
    )


async def _fragment_texts(executor: Executor, project_id: str, fragment_ids: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(fragment_ids))
    if not wanted:
        return {}
    rows = await executor.read(READ_INFER_FRAGMENTS, project_id, fragment_ids=wanted)
    return {row["fragment_id"]: row["text"] for row in rows}


def _keep_best(accepted: dict[str, _Proposal], proposal: _Proposal) -> None:
    best = accepted.get(proposal.edge_key)
    if best is None or (proposal.confidence, not proposal.reused) > (best.confidence, not best.reused):
        accepted[proposal.edge_key] = proposal


@dataclass(frozen=True)
class UpdateScope:
    """What an ``update`` build changed, for the subjects a ``cross_document`` rule judges in it: ``added`` the
    ``spec_id``s of nodes created by this build, ``changed`` those created, modified or rekeyed (``SpecChanges``).
    """

    added: frozenset[str] = frozenset()
    changed: frozenset[str] = frozenset()

    def covers(self, members: frozenset[str], before: frozenset[str] | None, *, judged: bool) -> bool:
        """Whether a subject with no stored judgement for its inputs is judged in this update: ``members`` its member
        ``spec_id``s, ``before`` those it had when it was last a subject (None when not recorded: then only ``added``
        members count as new).
        """

        joined = members - before if before is not None else members & self.added
        if not judged:
            return bool(joined)
        left = before - members if before is not None else frozenset()
        return bool(joined or left or members & self.changed)


@dataclass(frozen=True)
class SubjectSample:
    """A replay's subset of each rule's subjects (``infer(..., sample=...)``; examples/graph_replay.py ``--sample``):
    ``sizes`` by rule id (a rule not named is judged whole), the subjects ordered by the hash of ``seed``, rule id and
    ``spec_id`` and the first ``size`` taken, so a seed always picks the same subjects.
    """

    sizes: Mapping[str, int]
    seed: int = 0

    def pick(self, rule_id: str, spec_ids: Iterable[str]) -> frozenset[str] | None:
        """The ``spec_id``s picked for the rule; None when it is judged whole."""

        size = self.sizes.get(rule_id)
        found = sorted(set(spec_ids))
        if size is None or size >= len(found):
            return None

        def order(spec_id: str) -> str:
            return hashlib.sha256(f"{self.seed}|{rule_id}|{spec_id}".encode()).hexdigest()

        return frozenset(sorted(found, key=order)[: max(size, 0)])


@dataclass(frozen=True)
class BuildChanges:
    """What this build changed, as inference reads it: ``graph_changed`` false when its diff changed nothing (each rule
    drains its queue, then never-judged subjects, under its ``cap``), ``update`` an ``update`` build's spec changes
    (None in the other modes), ``first`` a ``first`` or ``rebuild`` build (past the ``cap``: ``first_cap`` within
    ``first_budget``; ``graph.build.infer_bound``), ``backlog`` the queues of the project's latest finished build, by
    rule id (``previous_backlog``), and ``sample`` a replay's subset of subjects to judge (``SubjectSample``; None in a
    build).
    """

    graph_changed: bool = True
    update: UpdateScope | None = None
    first: bool = False
    backlog: Mapping[str, BacklogQueue] = field(default_factory=dict)
    sample: SubjectSample | None = None


@dataclass(frozen=True)
class _Run:
    """One rule's judging context: the ontology, the rule, the shown nodes, the build's shared validator, each node's
    owners (``cross_document``: updated as owner edges are admitted), judge memory, each ``cross_document`` subject's
    member ``spec_id``s (by subject uuid) and the loaded spine nodes by key (the ends of read edges, ``_read_refs``).
    """

    ontology: Ontology
    rule: InferenceRule
    nodes: Mapping[str, _Node]
    validator: _Validator
    owners: dict[str, set[str]]
    memory: JudgeMemory
    members: Mapping[str, frozenset[str]] = field(default_factory=dict)
    spine: Mapping[str, SpineNode] = field(default_factory=dict)
    keepable: tuple[_CurrentEdge, ...] = ()
    """The rule's own inferred edges a subject that is not judged keeps (``_keepable``)."""

    def keep_failed(self, subject: _Subject) -> None:
        """A subject whose judgement failed keeps its edges: later verdicts are validated against them."""

        self.validator.keep(
            _view_edge(edge, self.rule.id)
            for edge in self.keepable
            if subject.owns(self.rule.template, edge.source, edge.target)
        )


@dataclass
class _Collected:
    """What a rule's judgements gave: subjects judged or reused, accepted edges (best per key),
    refused edges, judgements per outcome, judged items, reused subjects and the ``spec_id``s of the
    subjects whose judgement failed (judging order)."""

    judged: set[str] = field(default_factory=set)
    accepted: dict[str, _Proposal] = field(default_factory=dict)
    refused: list[_Proposal] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys((*OUTCOMES, "failed"), 0))
    failed: list[str] = field(default_factory=list)
    items: list[JudgedItem] = field(default_factory=list)
    reused: int = 0


def _admit(run: _Run, proposal: _Proposal) -> bool:
    """Check 5 of a ``cross_document`` item: its ends share no owner on a ``distinct_owner`` pair, and the shared
    validator accepts it against the graph plus the edges accepted before.
    """

    source, target = run.nodes[proposal.source], run.nodes[proposal.target]
    pair = run.ontology.pair_rule(proposal.relation, source.label, target.label)
    owners = run.owners
    if pair is not None and pair.distinct_owner and owners.get(source.uuid, set()) & owners.get(target.uuid, set()):
        return False
    checker, refused = run.validator.trial(run.rule.id, [proposal])
    if refused:
        return False
    run.validator.commit(checker)
    if pair is not None and pair.owner:
        owners.setdefault(source.uuid, set()).add(target.uuid)
    return True


def _judged_items(
    run: _Run, shown: tuple[_Subject, _Judgement, Mapping[str, str]], verdict: Verdict, evaluation: _Evaluation
) -> tuple[JudgedItem, ...]:
    """The judged items of an ``endpoints``, ``realization`` or ``attach_orphan`` verdict (``JudgedItem``): one per edge
    it proposes (an ``owner`` item for ``attach_orphan``, else ``link`` items), ends as stored; one ``declined`` item
    without a target when it proposes none, one ``invalid`` item without a target when it names an id it was not given.
    """

    subject, judgement, texts = shown
    rule, nodes = run.rule, run.nodes
    kind: JudgedItemKind = infer_xdoc.OWNER if rule.template == ATTACH_ORPHAN else infer_xdoc.LINK
    verdict = _relocated(rule, subject, judgement, verdict, nodes=nodes, texts=texts)
    evidence = tuple((item.fragment_id, item.quote) for item in verdict.evidence)

    def item(ends: tuple[str, str | None], outcome: str) -> JudgedItem:
        source, target = (nodes.get(end) if end is not None else None for end in ends)
        return JudgedItem(
            rule=rule.id,
            kind=kind,
            subject=subject.spec_id,
            source_spec_id=source.spec_id if source is not None else None,
            relation=rule.writes,
            target_spec_id=target.spec_id if target is not None else None,
            outcome=outcome,
            confidence=verdict.confidence,
            fragment_ids=tuple(dict.fromkeys(fragment_id for fragment_id, _ in evidence)),
            evidence=evidence,
            read_edges=_read_refs(run, subject, next((end for end in ends if end != subject.uuid), None)),
        )

    pairs = _proposed_pairs(rule, subject, verdict, judgement.ids)
    if not pairs:
        return (item((subject.uuid, None), "invalid" if pairs is None else "declined"),)
    if evaluation.outcome in ("accepted", "invalid"):
        outcome: str = evaluation.outcome
    else:
        outcome = infer_xdoc.QUOTE_MISSING if not quotes_valid(verdict, texts) else infer_xdoc.BELOW_FLOOR
    stored = [
        _proposal(run.ontology, rule.writes, nodes[source], nodes[target], confidence=0.0, evidence=())
        for source, target in pairs
    ]
    return tuple(item((proposal.source, proposal.target), outcome) for proposal in stored)


def _read_refs(run: _Run, subject: _Subject, target: str | None) -> tuple[str, ...] | None:
    """The read rules' planned edges behind ``target`` (``_Subject.reads``) in ``edge_ref`` form, for
    ``JudgedItem.read_edges``: None for a rule without ``reads``, empty without a target."""

    if not run.rule.reads:
        return None
    refs: list[str] = []
    for found in subject.reads.get(target, ()) if target is not None else ():
        source, relation, end = infer_reads.edge_parts(found)
        if source in run.spine and end in run.spine:
            refs.append(edge_ref(run.spine[source].spec_id, relation, run.spine[end].spec_id))
    return tuple(refs)


def _read_edges_count(selection: _Selection) -> int:
    """The read rules' planned edges behind the shown candidates of the rule's subjects, each once."""

    return len({edge for subject in selection.subjects for found in subject.reads.values() for edge in found})


def _items_outcome(items: Sequence[JudgedItem]) -> Outcome:
    """A ``cross_document`` judgement's outcome: ``accepted`` when an item is written, ``declined``
    without items, else ``invalid`` when an item is, else ``rejected``."""

    outcomes = {item.outcome for item in items}
    if outcomes & infer_xdoc.ADMITTED:
        return "accepted"
    if not outcomes:
        return "declined"
    return "invalid" if infer_xdoc.INVALID in outcomes else "rejected"


def _as_stored(
    ontology: Ontology, item: infer_xdoc.Item, nodes: Mapping[str, _Node], ends: Mapping[str, frozenset[str]]
) -> infer_xdoc.Item:
    """An admitted item with the ends its edge is stored with: a symmetric relation (a downgraded
    ``DEPENDS_ON`` too) stored the other way round swaps them and puts the stored source side's
    evidence (``ends``: each node's shown fragments) first."""

    source, target = item.source, item.target
    if item.outcome not in infer_xdoc.ADMITTED or source is None or target is None:
        return item
    if not _stored_reversed(ontology, item.relation, nodes[source], nodes[target]):
        return item
    return infer_xdoc.reversed_item(item, ends.get(target, frozenset()))


def _cross_document_evaluation(
    run: _Run, shown: tuple[_Subject, _Judgement, Mapping[str, str]], verdict: CrossDocumentVerdict
) -> tuple[_Evaluation, tuple[JudgedItem, ...]]:
    """Each item of a ``cross_document`` verdict on its own: checks 1-4 (``infer_xdoc.check_items``), then check 5
    (``_admit``); an item check 5 refuses is ``invalid`` and its edge quarantined.
    """

    subject, judgement, texts = shown
    nodes = run.nodes
    known = {uuid: nodes[uuid] for uuid in judgement.ids.values() if uuid in nodes}
    given = infer_xdoc.Given(
        ids=judgement.ids,
        pairs=subject.pairs,
        candidates=subject.via,
        relations=tuple(judgement.payload.get("relations", ())),
        labels={uuid: node.label for uuid, node in known.items()},
        ends={
            uuid: frozenset(fragment_id for fragment_id, _ in node.fragments if fragment_id in judgement.fragments)
            for uuid, node in known.items()
        },
        texts=texts,
    )
    spec_ids = {uuid: node.spec_id for uuid, node in known.items()}
    proposals: list[_Proposal] = []
    refused: list[_Proposal] = []
    items: list[JudgedItem] = []
    checked = infer_xdoc.check_items(run.ontology, run.rule, verdict, given)
    for item in (_as_stored(run.ontology, found, nodes, given.ends) for found in checked):
        outcome, ends = item.outcome, (item.source, item.target)
        if outcome in infer_xdoc.ADMITTED and item.source is not None and item.target is not None:
            proposal = _proposal(
                run.ontology,
                item.relation,
                nodes[item.source],
                nodes[item.target],
                confidence=item.confidence,
                evidence=item.evidence,
            )
            ends = (proposal.source, proposal.target)
            if _admit(run, proposal):
                proposals.append(proposal)
            else:
                outcome = infer_xdoc.INVALID
                refused.append(proposal)
        items.append(
            infer_xdoc.judged_item(run.rule, subject.spec_id, item, outcome=outcome, ends=ends, spec_ids=spec_ids)
        )
    return _Evaluation(_items_outcome(items), tuple(proposals), tuple(refused)), tuple(items)


def _reuse(
    run: _Run,
    readable: Sequence[tuple[_Subject, _Judgement]],
    keys: Mapping[str, str],
    stored: Mapping[str, dict[str, Any]],
    collected: _Collected,
) -> tuple[list[tuple[_Subject, _Judgement]], list[list[_Proposal]]]:
    """Take the remembered judgements that read back; the subjects left to judge, and the reused
    judgements' edges, which ``_check_reused`` checks once the subjects to judge are admitted."""

    fresh: list[tuple[_Subject, _Judgement]] = []
    reused: list[list[_Proposal]] = []
    for subject, judgement in readable:
        entry = stored.get(keys[subject.uuid])
        proposals = None if entry is None else _recalled(run.ontology, run.rule, entry, run.nodes)
        if entry is None or proposals is None:
            fresh.append((subject, judgement))
            continue
        collected.reused += 1
        collected.judged.add(subject.uuid)
        collected.refused.extend(_recalled_refused(run.ontology, run.rule, entry, run.nodes))
        reused.append(proposals)
    return fresh, reused


def _check_reused(run: _Run, reused: Sequence[list[_Proposal]], collected: _Collected) -> None:
    """Check the reused judgements' edges with the validator again (all together, or each on its own
    for ``cross_document``) and collect those it accepts."""

    for proposals in reused:
        if run.rule.template == CROSS_DOCUMENT:
            admitted = [proposal for proposal in proposals if _admit(run, proposal)]
            collected.refused.extend(proposal for proposal in proposals if proposal not in admitted)
        else:
            checker, failing = run.validator.trial(run.rule.id, proposals)
            if failing:
                collected.refused.extend(failing)  # the graph changed under a remembered verdict: none of it is written
                continue
            run.validator.commit(checker)
            admitted = proposals
        for proposal in admitted:
            _keep_best(collected.accepted, proposal)


@dataclass
class _Budget:
    """The counted input (estimated tokens) of the build's fresh inference calls: ``spent`` of every call admitted so
    far (what admission is charged against), ``done`` of the calls completed so far (what judge memory records with each
    flush, so a resumed infer step starts from it).
    """

    spent: int = 0
    done: int = 0


@dataclass
class _Fresh:
    """What judging a rule's fresh subjects gave: those deferred (not judged), the limit that stopped the rule
    (``infer_bound.stop``; None when no fresh subject was left), the counted input of its calls, its queue after the
    build and the subjects with changed inputs a build whose diff changed nothing queued without judging them
    (``waiting``).
    """

    deferred: int = 0
    limit: RuleLimit | None = None
    tokens: int = 0
    queue: BacklogQueue | None = None
    waiting: int = 0


async def _judging_order(
    inference: _Inference, run: _Run, fresh: Sequence[_Item]
) -> tuple[list[list[_Item]], list[_Item]]:
    """The fresh subjects a rule may judge, in judging order, in the parts of ``infer_bound.judging_parts``: (1) judged
    before with changed inputs, (2) the persisted queue (subjects still fresh, in its order), (3) never judged and not
    queued, (1) and (3) in the selection's order; and the subjects it queues without judging them.
    """

    rule, memory, changes = run.rule, run.memory, inference.changes
    seen = await memory.seen(rule, (subject.spec_id for subject, _ in fresh))
    previous = inference.changes.backlog.get(rule.id)
    by_spec_id = {subject.spec_id: (subject, judgement) for subject, judgement in fresh}
    queue = [spec_id for spec_id in dict.fromkeys(previous.spec_ids if previous else ()) if spec_id in by_spec_id]
    queued = set(queue)
    scope = changes.update
    waiting: list[_Item] = []
    if not changes.graph_changed:
        changed: list[_Item] = []
        first: set[str] = set()
        new = [item for item in fresh if item[0].spec_id not in seen and item[0].spec_id not in queued]
        waiting = [item for item in fresh if item[0].spec_id in seen and item[0].spec_id not in queued]
    elif scope is not None and rule.template == CROSS_DOCUMENT:
        before = await memory.members(rule, (subject.spec_id for subject, _ in fresh))
        covered = [
            item
            for item in fresh
            if scope.covers(
                run.members.get(item[0].uuid, frozenset()), before.get(item[0].spec_id), judged=item[0].spec_id in seen
            )
        ]
        changed = sorted(covered, key=lambda item: item[0].spec_id not in seen)  # stable: selection order
        first = {subject.spec_id for subject, _ in changed}
        new = []
        log_event(
            logger,
            INFO,
            "graph.infer.update_scope",
            rule=rule.id,
            in_scope=len(changed),
            queued=len(queued - first),
            deferred=len(fresh) - len(changed) - len(queued - first),
        )
    else:
        changed = [item for item in fresh if item[0].spec_id in seen]
        first = {subject.spec_id for subject, _ in changed}
        new = [item for item in fresh if item[0].spec_id not in seen and item[0].spec_id not in queued]
    parts = infer_bound.judging_parts(
        changed,
        [by_spec_id[spec_id] for spec_id in queue if spec_id not in first],
        new,
        cap=run.ontology.inference.cap_of(rule),
        update=scope is not None,
    )
    return parts, waiting


async def _judge_fresh(
    inference: _Inference,
    run: _Run,
    fresh: Sequence[_Item],
    keys: Mapping[str, str],
    collected: _Collected,
    *,
    admitted: Callable[[frozenset[str]], None] | None = None,
) -> _Fresh:
    """Judge the fresh subjects in judging order (``_judging_order``) within the rule's limits (``infer_bound``)."""

    rule = run.rule
    parts, waiting = await _judging_order(inference, run, fresh)
    order = [item for part in parts for item in part]
    limits = infer_bound.limits_of(run.ontology.inference, rule, first=inference.changes.first)
    out = _Fresh(waiting=len(waiting))
    calls = await _admitted_calls(inference, run, parts, limits, out)
    taken = {subject.uuid for call in calls for subject, _, _ in call.members}
    if admitted is not None:
        admitted(frozenset(taken))
    rank = {subject.uuid: index for index, (subject, _) in enumerate(order)}
    await _run_calls(inference, run, calls, sorted(taken, key=rank.__getitem__), (keys, collected, out))
    out.deferred = len(fresh) - len(taken)
    out.queue = infer_bound.next_queue(
        (subject.spec_id for subject, _ in order if subject.uuid not in taken),
        collected.failed,
        inference.changes.backlog.get(rule.id),
        build_id=inference.build_id,
        waiting=(subject.spec_id for subject, _ in waiting),
    )
    if waiting:
        log_event(
            logger, INFO, "graph.infer.changed_queued", build_id=inference.build_id, rule=rule.id, subjects=len(waiting)
        )
    if out.limit is not None:
        log_event(
            logger,
            INFO,
            "graph.infer.bounded",
            build_id=inference.build_id,
            rule=rule.id,
            limit=out.limit,
            judged=len(taken),
            deferred=out.deferred,
            budget_tokens=out.tokens,
        )
    return out


async def _admitted_calls(
    inference: _Inference, run: _Run, parts: Sequence[Sequence[_Item]], limits: infer_bound.RuleLimits, out: _Fresh
) -> list[_Call]:
    """The calls the rule's limits admit, decided before any call: the fresh subjects grouped into calls over the whole
    judging order, within one part of it and one side of the ``cap`` (``_calls``), in the order of their first subject;
    each call admitted and charged to the build's budget (``_admitted``), the fragment texts read once per
    ``infer_bound.CHUNK`` subjects (``_chunks``); ``out.limit`` the limit that stopped the rule, ``out.tokens`` the
    charged input.
    """

    order: list[_Item] = []
    part_of: dict[str, tuple[int, bool]] = {}
    for index, part in enumerate(parts):
        for item in part:
            part_of[item[0].uuid] = (index, len(order) >= limits.cap)
            order.append(item)
    calls: list[_Call] = []
    count = 0
    for chunk in _chunks(_calls(run, order, part_of)):
        texts = await _fragment_texts(
            inference.executor,
            inference.project_id,
            (fragment_id for group in chunk for _, judgement in group for fragment_id in judgement.fragments),
        )
        for group in chunk:
            call, out.limit = _admitted(run, group, texts, limits, count=count, spent=inference.budget.spent)
            if call is not None:
                inference.budget.spent += call.tokens
                out.tokens += call.tokens
                count += len(call.members)
                calls.append(call)
            if out.limit is not None:
                return calls
    return calls


def _chunks(groups: Sequence[list[_Item]]) -> list[list[list[_Item]]]:
    """Consecutive calls' groups, at least ``infer_bound.CHUNK`` subjects each (the last fewer): the
    fragment texts of a chunk are read at once."""

    chunks: list[list[list[_Item]]] = []
    current: list[list[_Item]] = []
    size = 0
    for group in groups:
        current.append(group)
        size += len(group)
        if size >= infer_bound.CHUNK:
            chunks.append(current)
            current, size = [], 0
    if current:
        chunks.append(current)
    return chunks


WINDOW_PER_SLOT: Final = 2
"""Judge calls kept in flight per gateway slot (``JudgeRunner.concurrency``): a call waiting out a
retry backoff frees its gateway slot, so twice the slots keep the gateway busy meanwhile."""


async def _run_calls(
    inference: _Inference,
    run: _Run,
    calls: Sequence[_Call],
    expected: Sequence[str],
    into: tuple[Mapping[str, str], _Collected, _Fresh],
) -> None:
    """Run the admitted calls in a sliding window of ``WINDOW_PER_SLOT`` times the gateway's concurrency (a slow call
    holds one place, never the others: no head-of-line blocking).
    """

    keys, collected, out = into
    width = max(1, WINDOW_PER_SLOT * inference.judge.concurrency)
    waiting: deque[tuple[int, _Call]] = deque(enumerate(calls))
    sequence = len(calls)
    running: dict[asyncio.Future[Verdict | JudgeFailure], tuple[int, _Call]] = {}
    ready: dict[str, _Result] = {}
    entries: dict[str, dict[str, Any]] = {}
    cursor = completed = flushed = 0
    try:
        while waiting or running:
            while waiting and len(running) < width:
                index, call = waiting.popleft()
                started = asyncio.ensure_future(inference.judge.judge(call.prompt, call.payload, call.verdict_type))
                running[started] = (index, call)
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for done in sorted(finished, key=lambda found: running[found][0]):
                _, call = running.pop(done)
                completed += 1
                inference.budget.done += call.tokens
                results, again = _resolved(inference, run, call, done.result(), out)
                for single in reversed(again):
                    waiting.appendleft((sequence, single))
                    sequence += 1
                ready.update((found[0].uuid, found) for found in results)
                while cursor < len(expected) and expected[cursor] in ready:
                    entries.update(_take(run, [ready.pop(expected[cursor])], keys, collected))
                    cursor += 1
                if completed - flushed >= infer_bound.CHUNK:
                    await _flush(inference, run.rule, entries)
                    entries, flushed = {}, completed
    finally:
        for pending in running:
            pending.cancel()
        await asyncio.gather(*running, return_exceptions=True)
    if calls:
        await _flush(inference, run.rule, entries)


async def _flush(inference: _Inference, rule: InferenceRule, entries: Mapping[str, dict[str, Any]]) -> None:
    """Write judgements and ``seen`` keys to judge memory, with the build's completed input so far
    (``JudgeMemory.record_spent``); nothing in a dry run."""

    if not inference.write:
        return
    await inference.memory.remember(rule, entries)
    await inference.memory.record_spent(inference.build_id, inference.budget.done)


# Batched attach_orphan calls

_Shown = tuple[_Subject, _Judgement, dict[str, str]]
"""A subject in a call: its judgement and the fragment texts its own call shows."""
_Result = tuple[_Subject, _Judgement, dict[str, str], Verdict | JudgeFailure]
UNSHOWN_TARGET: Final = "C0"
"""The target a batched answer becomes when it names a candidate that is not one of its subject's own: no
single call shows ``C0``, so the answer is ``invalid``, as a single call naming an id it was not given."""


def _batching(rule: InferenceRule) -> bool:
    """Whether the rule judges subjects together: ``batch`` of at least ``BATCH_MIN`` and a ``batch_prompt``
    (``attach_orphan`` only; the ontology names it)."""

    return rule.batch_prompt is not None and (rule.batch or 0) >= BATCH_MIN


@dataclass(frozen=True)
class _Call:
    """One judge call: its subjects, prompt, verdict type and payload (with ``fragments``), its counted
    input and, for a batch, the local ids of its subjects (``S1``, ...) and of every candidate one of them
    shows (``C1``, ...)."""

    members: tuple[_Shown, ...]
    prompt: str
    verdict_type: type[Verdict]
    payload: dict[str, Any]
    tokens: int
    subjects: Mapping[str, str] = field(default_factory=dict)
    candidates: Mapping[str, str] = field(default_factory=dict)


def _calls(run: _Run, items: Sequence[_Item], part_of: Mapping[str, Hashable] | None = None) -> list[list[_Item]]:
    """Fresh subjects (judging order) as calls: one per subject, or for a batching rule the subjects written in the same
    section of the same document (``_written_in``), each with its own candidates, at most ``batch`` per call, grouped
    over all of ``items``; calls in the order of their first subject.
    """

    if not _batching(run.rule):
        return [[item] for item in items]
    size = run.rule.batch or BATCH_MIN
    parts = part_of or {}
    groups: dict[tuple[Hashable, ...], list[_Item]] = {}
    for subject, judgement in items:
        groups.setdefault((parts.get(subject.uuid), _written_in(run, subject)), []).append((subject, judgement))
    rank = {subject.uuid: index for index, (subject, _) in enumerate(items)}
    calls = [group[start : start + size] for group in groups.values() for start in range(0, len(group), size)]
    return sorted(calls, key=lambda call: rank[call[0][0].uuid])


def _written_in(run: _Run, subject: _Subject) -> Hashable:
    """Where a subject is written, which batching groups by: the document and the section (heading
    path, ``_Node.sections``) of its first grounding fragment; that fragment when it has no section;
    the subject alone when it has no fragment."""

    node = run.nodes.get(subject.uuid)
    if node is None or not node.fragments:
        return subject.uuid
    document = node.documents[0] if node.documents else ""
    section = node.sections[0] if node.sections else ""
    return (document, section) if section else (document, node.fragments[0][0])


def _single_call(run: _Run, item: tuple[_Subject, _Judgement], texts: Mapping[str, str]) -> _Call:
    subject, judgement = item
    fragments = {fid: texts[fid] for fid in judgement.fragments if fid in texts}
    payload = {**judgement.payload, FRAGMENTS_KEY: fragments}
    tokens = request_tokens(judge_messages(run.ontology, run.rule.prompt, payload))
    return _Call(((subject, judgement, fragments),), run.rule.prompt, VERDICT_TYPES[run.rule.template], payload, tokens)


def _call(run: _Run, items: Sequence[tuple[_Subject, _Judgement]], texts: Mapping[str, str]) -> _Call:
    """The call of ``items``: a single call for one subject; else a batch (prompt ``InferenceRule.batch_prompt``): the
    subjects (``S1``,...
    """

    if len(items) == 1:
        return _single_call(run, items[0], texts)
    rule, nodes = run.rule, run.nodes
    local: dict[str, str] = {}  # candidate uuid -> its id in the batch
    members: list[_Shown] = []
    shown: dict[str, str] = {}
    listed: list[dict[str, Any]] = []
    for index, (subject, judgement) in enumerate(items, 1):
        fragments = {fid: texts[fid] for fid in judgement.fragments if fid in texts}
        members.append((subject, judgement, fragments))
        shown.update(fragments)
        own = [fid for fid, _ in nodes[subject.uuid].fragments[:SUBJECT_FRAGMENTS] if fid in fragments]
        mine = [local.setdefault(uuid, f"C{len(local) + 1}") for uuid in judgement.ids.values()]
        listed.append({**_owner_shown(nodes[subject.uuid], f"S{index}"), "fragments": own, "candidates": mine})
    payload: dict[str, Any] = {
        "relation": rule.writes,
        "subjects": listed,
        "candidates": [
            {
                **_owner_shown(nodes[uuid], local_id),
                "fragments": [fid for fid, _ in nodes[uuid].fragments[:OTHER_FRAGMENTS] if fid in shown],
            }
            for uuid, local_id in local.items()
        ],
        FRAGMENTS_KEY: shown,
    }
    prompt = rule.batch_prompt or rule.prompt
    return _Call(
        tuple(members),
        prompt,
        AttachOrphanBatchVerdict,
        payload,
        request_tokens(judge_messages(run.ontology, prompt, payload)),
        subjects={f"S{index}": subject.uuid for index, (subject, _) in enumerate(items, 1)},
        candidates={local_id: uuid for uuid, local_id in local.items()},
    )


def _admitted(
    run: _Run,
    group: Sequence[tuple[_Subject, _Judgement]],
    texts: Mapping[str, str],
    limits: infer_bound.RuleLimits,
    *,
    count: int,
    spent: int,
) -> tuple[_Call | None, RuleLimit | None]:
    """The call of the longest head of ``group`` the limits admit (``infer_bound.stop`` for its last subject, ``count``
    subjects taken before it, with the call's counted input) and the limit that stopped the rest of the group (None: all
    of it admitted).
    """

    stopped: RuleLimit | None = None
    for size in range(len(group), 0, -1):
        call = _call(run, group[:size], texts)
        limit = infer_bound.stop(limits, count + size - 1, call.tokens, spent)
        if limit is None:
            return call, stopped
        stopped = stopped or limit
    return None, stopped


def _answers(call: _Call, verdict: Verdict) -> dict[str, Verdict] | None:
    """Each answered subject's verdict from a batch verdict, by subject uuid, as its own call would give it: the target
    named by the subject's own candidate id, ``UNSHOWN_TARGET`` when the answer names a candidate that is not one of the
    subject's own.
    """

    if not isinstance(verdict, AttachOrphanBatchVerdict):
        return None
    found: dict[str, OrphanAnswer] = {}
    for answer in verdict.answers:
        uuid = call.subjects.get(answer.subject)
        if uuid is None or uuid in found:
            return None
        found[uuid] = answer
    verdicts: dict[str, Verdict] = {}
    for subject, judgement, _ in call.members:
        given = found.get(subject.uuid)
        if given is None:
            continue
        target = given.target
        if target is not None:
            own = {uuid: local_id for local_id, uuid in judgement.ids.items()}
            target = own.get(call.candidates.get(target, ""), UNSHOWN_TARGET)
        single = AttachOrphanVerdict(confidence=given.confidence, evidence=given.evidence, target=target)
        single.attach_prompt_ref(call.prompt)
        verdicts[subject.uuid] = single
    return verdicts


def _resolved(
    inference: _Inference, run: _Run, call: _Call, answer: Verdict | JudgeFailure, out: _Fresh
) -> tuple[list[_Result], list[_Call]]:
    """A completed call's subjects with their verdicts (or failure), and the single calls to make instead: every subject
    of a batch that failed or whose answers are malformed, the subjects a batch left without an answer (charged to the
    budget, never stopped by it: its subjects were admitted).
    """

    if len(call.members) == 1:
        ((subject, judgement, fragments),) = call.members
        return [(subject, judgement, fragments, answer)], []
    verdicts = None if isinstance(answer, JudgeFailure) else _answers(call, answer)
    found = verdicts or {}
    results: list[_Result] = [(*member, found[member[0].uuid]) for member in call.members if member[0].uuid in found]
    missing = [member for member in call.members if member[0].uuid not in found]
    if not missing:
        return results, []
    log_event(
        logger,
        WARNING,
        "graph.infer.batch_fallback",
        build_id=inference.build_id,
        rule=run.rule.id,
        subjects=len(missing),
        reason="failed" if isinstance(answer, JudgeFailure) else "malformed" if verdicts is None else "missing",
    )
    again = [_single_call(run, (subject, judgement), shown) for subject, judgement, shown in missing]
    for single in again:
        inference.budget.spent += single.tokens
        out.tokens += single.tokens
    return results, again


def _take(
    run: _Run,
    results: Sequence[_Result],
    keys: Mapping[str, str],
    collected: _Collected,
) -> dict[str, dict[str, Any]]:
    """Evaluate the fresh verdicts (a failed judgement is counted and listed, not remembered); the
    judge memory entries of the others (judgement and ``seen`` key)."""

    rule = run.rule
    entries: dict[str, dict[str, Any]] = {}
    for subject, judgement, fragments, result in results:
        if isinstance(result, JudgeFailure):
            collected.counts["failed"] += 1
            collected.failed.append(subject.spec_id)
            run.keep_failed(subject)
            continue
        if isinstance(result, CrossDocumentVerdict):
            evaluation, items = _cross_document_evaluation(run, (subject, judgement, fragments), result)
            collected.items.extend(items)
        else:
            evaluation = _evaluate(
                run.ontology,
                rule,
                subject,
                judgement,
                result,
                nodes=run.nodes,
                texts=fragments,
                validator=run.validator,
            )
            collected.items.extend(_judged_items(run, (subject, judgement, fragments), result, evaluation))
        collected.counts[evaluation.outcome] += 1
        collected.judged.add(subject.uuid)
        entries[keys[subject.uuid]] = _memory_entry(evaluation.outcome, evaluation.proposals, evaluation.refused)
        entries[run.memory.seen_key(rule, subject.spec_id)] = {"digest": judgement.digest}
        collected.refused.extend(evaluation.refused)
        for proposal in evaluation.proposals:
            _keep_best(collected.accepted, proposal)
    return entries


async def _judge_rule(
    inference: _Inference,
    *,
    selection: _Selection,
    owners: dict[str, set[str]] | None = None,
) -> _RulePlan:
    """Reuse remembered judgements, judge the other subjects within the rule's limits (``_judge_fresh``) and collect the
    accepted edges.
    """

    executor, project_id, ontology = (inference.executor, inference.project_id, inference.ontology)
    rule, memory = selection.rule, inference.memory
    nodes = await _nodes(
        executor,
        project_id,
        ontology,
        (
            uuid
            for subject in selection.subjects
            for uuid in (subject.uuid, *subject.candidates, *subject.members, *subject.owners.values())
        ),
    )
    judgements = [
        (subject, _judgement(rule, subject, nodes, memory.judge_model, ontology=ontology))
        for subject in selection.subjects
    ]
    readable = [(subject, judgement) for subject, judgement in judgements if judgement is not None]
    keys = {subject.uuid: memory.key(rule, subject.spec_id, judgement.digest) for subject, judgement in readable}
    stored = await memory.recall(rule, list(keys.values()))
    spine = inference.loaded.nodes
    members = (
        {
            subject.uuid: frozenset(spine[uuid].spec_id for uuid in subject.targets if uuid in spine)
            for subject in selection.subjects
        }
        if rule.template == CROSS_DOCUMENT
        else {}
    )
    run = _Run(
        ontology,
        rule,
        nodes,
        inference.validator,
        {} if owners is None else owners,
        memory,
        members=members,
        spine=inference.loaded.nodes,
        keepable=_keepable(rule, selection.current, selection.stale),
    )
    collected = _Collected()
    fresh, reused = _reuse(run, readable, keys, stored, collected)
    sample = inference.changes.sample
    picked = sample.pick(rule.id, (subject.spec_id for subject in selection.subjects)) if sample else None
    left_out = 0
    if picked is not None:
        sampled = [item for item in fresh if item[0].spec_id in picked]
        left_out, fresh = len(fresh) - len(sampled), sampled

    def admitted(taken: frozenset[str]) -> None:
        # The verdicts are validated against every inferred edge the swap keeps: the other rules'
        # (``_held``) and those of this rule's subjects that are not judged (deferred, sampled out
        # unreadable), known once the calls are admitted.
        kept = _kept_edges(rule, selection.subjects, collected.judged | taken, selection.current, selection.stale)
        run.validator.hold_also(_view_edge(edge, rule.id) for edge in kept)
        _check_reused(run, reused, collected)

    judged = await _judge_fresh(inference, run, fresh, keys, collected, admitted=admitted)

    counts = collected.counts
    report = RuleReport(
        rule=rule.id,
        subjects=len(selection.subjects),
        judged=sum(counts[name] for name in OUTCOMES),
        accepted=counts["accepted"],
        declined=counts["declined"],
        rejected=counts["rejected"],
        invalid=counts["invalid"],
        failed=counts["failed"],
        deferred=judged.deferred + left_out + len(judgements) - len(readable),
        reused=collected.reused,
        truncated=sum(1 for subject in selection.subjects if subject.truncated),
        items_by_outcome=infer_xdoc.items_by_outcome(collected.items) if rule.template == CROSS_DOCUMENT else None,
        duplicate_pairs=selection.duplicate_pairs if rule.template == CROSS_DOCUMENT else None,
        limit=judged.limit,
        budget_tokens=judged.tokens,
        backlog=len(judged.queue.spec_ids) if judged.queue is not None else 0,
        read_edges=_read_edges_count(selection) if rule.reads else None,
        changed_queued=None if inference.changes.graph_changed else judged.waiting,
    )
    return _RulePlan(
        rule,
        report,
        selection.subjects,
        frozenset(collected.judged),
        collected.accepted,
        selection.current,
        {
            memory.members_key(rule, subject.spec_id): {MEMBERS: sorted(members[subject.uuid])}
            for subject in selection.subjects
            if subject.uuid in members
        },
        tuple(_quarantined(proposal, nodes) for proposal in collected.refused),
        stale=selection.stale,
        items=tuple(collected.items),
        queue=judged.queue,
    )


# Swap


@dataclass
class _Swap:
    deletes: list[dict[str, Any]] = field(default_factory=list)
    creates: list[dict[str, Any]] = field(default_factory=list)
    refreshes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _PlannedEdges:
    """What the swap leaves for one rule (``_planned_edges``): its inferred edges before it (``old``), the keys of those
    a deferred or failed subject keeps (``kept``), the keys another edge holds (stated, implied or another rule's:
    ``taken``, never written by the rule) and its accepted edges.
    """

    rule: str
    old: tuple[_CurrentEdge, ...]
    kept: frozenset[str]
    taken: frozenset[str]
    accepted: Mapping[str, _Proposal]

    @property
    def edges(self) -> tuple[SpineEdge, ...]:
        """The rule's inferred edges once the swap committed: the old ones kept or accepted again, then
        the accepted ones it writes new (key order)."""

        old_keys = {edge.edge_key for edge in self.old}
        staying = [
            SpineEdge(edge.relation, edge.source, edge.target, INFERRED, self.rule)
            for edge in self.old
            if edge.edge_key in self.kept or edge.edge_key in self.accepted
        ]
        added = [
            SpineEdge(proposal.relation, proposal.source, proposal.target, INFERRED, self.rule)
            for key, proposal in sorted(self.accepted.items())
            if key not in self.taken and key not in old_keys
        ]
        return (*staying, *added)


def _planned_edges(plan: _RulePlan) -> _PlannedEdges:
    """The rule's edges as the swap will leave them (``_swap_rows`` writes them; a rule that reads this
    one sees ``edges``): a judged or reused subject gets the edges of its accepted verdict, a deferred or
    failed subject keeps its own (module doc, 3)."""

    rule = plan.rule
    old = tuple(edge for edge in plan.current if edge.basis == INFERRED and edge.rule == rule.id)
    old_keys = {edge.edge_key for edge in old}
    kept = frozenset(edge.edge_key for edge in _kept_edges(rule, plan.subjects, plan.judged, plan.current, plan.stale))
    taken = frozenset(edge.edge_key for edge in plan.current if edge.edge_key not in old_keys)
    return _PlannedEdges(rule.id, old, kept, taken, plan.accepted)


def _keepable(rule: InferenceRule, current: Iterable[_CurrentEdge], stale: frozenset[str]) -> tuple[_CurrentEdge, ...]:
    """The rule's inferred edges a subject that is not judged keeps (module doc, 3): all but an
    ``endpoints`` edge whose ends a non-inferred edge now links and a ``cross_document`` stale edge."""

    current = tuple(current)
    linked = _linked(current) if rule.template == ENDPOINTS else set()
    return tuple(
        edge
        for edge in current
        if edge.basis == INFERRED
        and edge.rule == rule.id
        and frozenset((edge.source, edge.target)) not in linked
        and edge.edge_key not in stale
    )


def _kept_edges(
    rule: InferenceRule,
    subjects: Iterable[_Subject],
    judged: Collection[str],
    current: Iterable[_CurrentEdge],
    stale: frozenset[str],
) -> tuple[_CurrentEdge, ...]:
    """The rule's inferred edges the swap keeps: the ``_keepable`` ones a subject not in ``judged``
    (uuids) owns."""

    unjudged = [subject for subject in subjects if subject.uuid not in judged]
    return tuple(
        edge
        for edge in _keepable(rule, current, stale)
        if any(subject.owns(rule.template, edge.source, edge.target) for subject in unjudged)
    )


def _held(
    rules: Iterable[InferenceRule],
    rule: InferenceRule,
    plans: Iterable[_RulePlan],
    currents: Mapping[str, tuple[_CurrentEdge, ...]],
) -> list[ViewEdge]:
    """The inferred edges of the rules other than ``rule`` that the swap leaves (what ``rule``'s proposals are validated
    against, beside the graph inference reads): a judged rule's planned edges (``_planned_edges``), and the current
    inferred edges of a rule not judged yet or that failed (the swap leaves those as they are until that rule's own
    judgements replace them).
    """

    planned = {plan.rule.id: plan for plan in plans}
    edges: list[ViewEdge] = []
    for other in rules:
        if other.id == rule.id:
            continue
        plan = planned.get(other.id)
        if plan is not None:
            edges.extend(_view_edge(edge, other.id) for edge in _planned_edges(plan).edges)
        else:
            edges.extend(
                _view_edge(edge, other.id)
                for edge in currents.get(other.id, ())
                if edge.basis == INFERRED and edge.rule == other.id
            )
    return edges


def _swap_rows(plan: _RulePlan, *, project_id: str, build_id: str, at: str, judge_model: str) -> dict[str, _Swap]:
    """The swap of one rule, per relation it writes (``InferenceRule.written`` order), as
    ``_planned_edges`` plans it."""

    rule = plan.rule
    template = rule.template
    planned = _planned_edges(plan)
    old, kept, taken = planned.old, planned.kept, planned.taken
    old_keys = {edge.edge_key for edge in old}
    swaps = {relation: _Swap() for relation in rule.written}
    for edge in old:
        if edge.edge_key not in kept and edge.edge_key not in plan.accepted:
            swaps[edge.relation].deletes.append(
                {"edge_key": edge.edge_key, "uuid": edge.uuid, "rule": rule.id, "ref": edge.ref}
            )
    used = {edge.uuid for edge in plan.current if edge.uuid}
    old_by_key = {edge.edge_key: edge for edge in old}
    for key in sorted(plan.accepted):
        proposal = plan.accepted[key]
        evidence = [{"fragment_id": fragment_id, "quote": quote} for fragment_id, quote in proposal.evidence]
        if key in taken or (proposal.reused and _in_place(old_by_key.get(key), proposal.confidence, evidence)):
            continue  # already stated, implied or another rule's; or a reused judgement's edge in place
        row = {
            "source": proposal.source,
            "target": proposal.target,
            "edge_key": key,
            "ref": proposal.ref,
            "uuid": _new_uuid(project_id, key, used),
            "created_at": at,
            "rule": rule.id,
            "either_direction": template in (ENDPOINTS, CROSS_DOCUMENT),
            "props": {
                "basis": INFERRED,
                "inference_rule": rule.id,
                "confidence": proposal.confidence,
                "evidence": json.dumps(evidence, ensure_ascii=False),
                "judge_model": judge_model,
                "source_fragment_ids": list(dict.fromkeys(fragment_id for fragment_id, _ in proposal.evidence)),
                "updated_at": at,
                "last_build_id": build_id,
            },
        }
        swap = swaps[proposal.relation]
        (swap.refreshes if key in old_keys else swap.creates).append(row)
    return swaps


def _in_place(edge: _CurrentEdge | None, confidence: float, evidence: list[dict[str, str]]) -> bool:
    """Whether ``edge`` already holds a reused judgement's edge as the swap would write it."""

    return (
        edge is not None and edge.confidence == confidence and edge.evidence == json.dumps(evidence, ensure_ascii=False)
    )


def _new_uuid(project_id: str, key: str, used: set[str]) -> str:
    """``edge_uuid`` of ``key``; one a current edge holds is not reused (``<key>|2``, ``|3``...)."""

    uuid, number = edge_uuid(project_id, key), 1
    while uuid in used:
        number += 1
        uuid = edge_uuid(project_id, f"{key}|{number}")
    used.add(uuid)
    return uuid


@dataclass(frozen=True)
class _Applied:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    updated: int


def _planned(swaps: Mapping[str, _Swap]) -> _Applied:
    """What a dry run would add, remove and update (nothing is written)."""

    return _Applied(
        tuple(row["ref"] for swap in swaps.values() for row in swap.creates),
        tuple(row["ref"] for swap in swaps.values() for row in swap.deletes),
        sum(len(swap.refreshes) for swap in swaps.values()),
    )


async def _apply(
    executor: Executor, project_id: str, swaps: Sequence[tuple[_RulePlan, Mapping[str, _Swap]]]
) -> dict[str, _Applied]:
    """Every rule's deletes and writes, over every relation it writes, in one transaction; what was
    added, removed (``edge_ref``s) and updated, per rule id. ``inferred_at`` is the time of the write."""

    applied: dict[str, _Applied] = {}
    inferred_at = datetime.now(UTC).isoformat(timespec="seconds")
    async with executor.tx(project_id) as transaction:
        for plan, by_relation in swaps:
            added: list[str] = []
            removed: list[str] = []
            updated = 0
            for relation, swap in by_relation.items():
                if swap.deletes:
                    records = await transaction.run(infer_delete_edges(relation), rows=swap.deletes, inferred=INFERRED)
                    removed.extend(records[0]["refs"] if records else [])
                for rows, creating in ((swap.creates, True), (swap.refreshes, False)):
                    if not rows:
                        continue
                    stamped = [{**row, "props": {**row["props"], "inferred_at": inferred_at}} for row in rows]
                    records = await transaction.run(infer_write_edges(relation), rows=stamped, inferred=INFERRED)
                    if creating:
                        added.extend(records[0]["refs"] if records else [])
                    else:
                        updated += int(records[0]["written"]) if records else 0
            applied[plan.rule.id] = _Applied(tuple(added), tuple(removed), updated)
    return applied


# Entry point


def _error_code(error: Exception, default: str) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code else default


@dataclass(frozen=True)
class InferenceStamp:
    """What the inferred edges of one build carry: ``last_build_id``, the time ``at`` and the judge model."""

    build_id: str
    at: str
    judge_model: str


@dataclass(frozen=True)
class _Inference:
    """One build's inference: where it reads and judges, the graph read once, the shared validator,
    what the build changed (with the latest finished build's queues), the build's input budget so far
    and whether judge memory is written (false: a dry run)."""

    executor: Executor
    project_id: str
    ontology: Ontology
    judge: JudgeRunner
    memory: JudgeMemory
    loaded: _Loaded
    validator: _Validator
    build_id: str
    changes: BuildChanges = field(default_factory=BuildChanges)
    budget: _Budget = field(default_factory=_Budget)
    write: bool = True


@dataclass
class _Judged:
    """Every rule's selection and plan (rules that did not fail) and report, the first rule failure's
    code and each rule's queue after the build (a failed rule keeps its previous one)."""

    plans: list[_RulePlan] = field(default_factory=list)
    reports: dict[str, RuleReport] = field(default_factory=dict)
    error_code: str | None = None
    queues: dict[str, BacklogQueue] = field(default_factory=dict)
    selections: list[_Selection] = field(default_factory=list)


def _overlay(loaded: _Loaded, rule: InferenceRule, plans: Iterable[_RulePlan]) -> tuple[SpineEdge, ...]:
    """The inferred edges of the rules ``rule`` reads, as this build plans them (``_planned_edges``); a
    read rule without a plan (it failed, or a replay left it out) keeps its current inferred edges."""

    planned = {plan.rule.id: plan for plan in plans}
    edges: list[SpineEdge] = []
    for rule_id in rule.reads:
        plan = planned.get(rule_id)
        if plan is not None:
            edges.extend(_planned_edges(plan).edges)
        else:
            edges.extend(edge for edge in loaded.spine.edges if _basis(edge.basis) == INFERRED and edge.rule == rule_id)
    return tuple(edges)


async def _judge_rules(run: _Inference, currents: Mapping[str, tuple[_CurrentEdge, ...]]) -> _Judged:
    """Select and judge every rule in order; a rule selects when it runs, after the rules before it were judged: a rule
    with ``reads`` sees their planned edges (``_overlay``) and a ``cross_document`` rule chooses its pairs counting the
    owner edges the earlier rules accepted.
    """

    judged = _Judged()
    for rule in run.ontology.inference.rules:
        overlay = _overlay(run.loaded, rule, judged.plans)
        selection = await _select(
            run.executor, run.project_id, run.ontology, rule, run.loaded, current=currents[rule.id], overlay=overlay
        )
        judged.selections.append(selection)
        owners: dict[str, set[str]] | None = None
        chosen = selection
        if rule.template == CROSS_DOCUMENT:
            owners = _owners_now(run.ontology, run.loaded, judged.plans)
            chosen = await _cross_document_pairs(
                run.executor, run.project_id, run.ontology, selection, loaded=run.loaded, owners=owners
            )
        run.validator.hold(_held(run.ontology.inference.rules, rule, judged.plans, currents))
        before = run.validator.snapshot()
        try:
            plan = await _judge_rule(run, selection=chosen, owners=owners)
        except LLMError as error:
            if isinstance(error, LLMFatalError):
                raise
            run.validator.commit(before)  # the failed rule's accepted edges are not written
            code = _error_code(error, "inference_rule_failed")
            judged.error_code = judged.error_code or code
            kept = infer_bound.carried(run.changes.backlog.get(rule.id))
            if kept is not None:
                judged.queues[rule.id] = kept
            judged.reports[rule.id] = RuleReport(
                rule=rule.id,
                subjects=len(chosen.subjects),
                error_code=code,
                backlog=len(kept.spec_ids) if kept is not None else 0,
            )
            log_event(logger, WARNING, "graph.infer.rule_failed", build_id=run.build_id, rule=rule.id, error_code=code)
            continue
        judged.plans.append(plan)
        judged.reports[rule.id] = plan.report
        if plan.queue is not None:
            judged.queues[rule.id] = plan.queue
    return judged


async def infer(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    judge: JudgeRunner,
    *,
    stamp: InferenceStamp,
    memory: JudgeMemory | None = None,
    changes: BuildChanges | None = None,
    write: bool = True,
) -> InferenceReport:
    """Run every inference rule of ``ontology`` once, in the ontology's order, and swap in the new inferred edges
    (module doc).
    """

    # Fail before any read or judge call when a rule asks for what this code cannot do.
    for rule in ontology.inference.rules:
        if rule.template == REALIZATION:
            _realization_queries(ontology, rule)
    build_id, at, judge_model = stamp.build_id, stamp.at, stamp.judge_model
    memory = memory or JudgeMemory(None, project_id, judge_model)
    # Read once per build: selection writes nothing, so every rule reads the same graph.
    spine = await load_spine_graph(executor, project_id, ontology)
    spine_before = len(metrics.spine_unreached(spine, ontology, READ_BASES))
    loaded = _Loaded.of(ontology, spine, await _inferred_owner_edges(executor, project_id, ontology))
    # The current edges the validator checks against are read once, up front; each rule selects lazily.
    currents = {rule.id: await _rule_current(executor, project_id, rule) for rule in ontology.inference.rules}
    validator = _Validator.over(ontology, loaded, (edge for current in currents.values() for edge in current))
    # A resumed infer step charges its budget from the input its completed calls were counted at.
    resumed = await memory.spent(build_id) if write else 0
    run = _Inference(
        executor,
        project_id,
        ontology,
        judge,
        memory,
        loaded,
        validator,
        build_id,
        changes=changes or BuildChanges(),
        budget=_Budget(spent=resumed, done=resumed),
        write=write,
    )
    judged = await _judge_rules(run, currents)
    selections = judged.selections

    reports = judged.reports
    added: list[str] = []
    removed: list[str] = []
    if judged.plans:
        swaps = [
            (plan, _swap_rows(plan, project_id=project_id, build_id=build_id, at=at, judge_model=judge_model))
            for plan in judged.plans
        ]
        if write:
            applied = await _apply(executor, project_id, swaps)
        else:
            applied = {plan.rule.id: _planned(by_relation) for plan, by_relation in swaps}
        for plan in judged.plans:
            result = applied.get(plan.rule.id, _Applied((), (), 0))
            added.extend(result.added)
            removed.extend(result.removed)
            reports[plan.rule.id] = plan.report.model_copy(
                update={
                    "edges_added": len(result.added),
                    "edges_removed": len(result.removed),
                    "edges_updated": result.updated,
                }
            )
            if write:
                await memory.remember(plan.rule, plan.memory)

    for report in reports.values():
        log_event(logger, INFO, "graph.infer.rule_done", build_id=build_id, write=write, **report.model_dump())
    items = sorted((item for plan in judged.plans for item in plan.items), key=infer_xdoc.item_order)
    if write:  # quotes are document text: only a dry run's report keeps them, for labelling
        items = [item.model_copy(update={"evidence": None}) for item in items]
    return InferenceReport(
        enabled=True,
        rules=tuple(reports[rule.id] for rule in ontology.inference.rules if rule.id in reports),
        integration_outliers=tuple(dict.fromkeys(item for s in selections for item in s.outliers)),
        orphans_without_candidate=tuple(dict.fromkeys(item for s in selections for item in s.without_candidate)),
        spine_unreachable_before=spine_before,
        spine_unreachable_after=_spine_after(ontology, loaded, judged.plans),
        inferred_added=tuple(sorted(set(added))),
        inferred_removed=tuple(sorted(set(removed) - set(added))),
        error_code=judged.error_code,
        quarantine=_quarantine(entry for plan in judged.plans for entry in plan.quarantine),
        judged_items=tuple(items),
        backlog={rule.id: judged.queues[rule.id] for rule in ontology.inference.rules if rule.id in judged.queues},
    )


def _spine_after(ontology: Ontology, loaded: _Loaded, plans: Iterable[_RulePlan]) -> int:
    """Spine sources that reach no root over the loaded graph with every rule's planned inferred edges
    (``_planned_edges``) in place of its current ones (a rule without a plan keeps its own); a dry run
    predicts it the same way (``InferenceReport.spine_unreachable_after``)."""

    planned = list(plans)
    replaced = {plan.rule.id for plan in planned}
    edges = [edge for edge in loaded.spine.edges if not (_basis(edge.basis) == INFERRED and edge.rule in replaced)]
    edges.extend(edge for plan in planned for edge in _planned_edges(plan).edges)
    return len(metrics.spine_unreached(SpineGraph(loaded.spine.nodes, tuple(edges)), ontology))


async def previous_backlog(builds: BuildStore, project_id: str) -> dict[str, BacklogQueue]:
    """The queues (``InferenceReport.backlog``) of the project's latest finished build; empty without
    one, or when its infer artifact holds none (a lost queue loses only its order)."""

    latest = await builds.latest_build(project_id, finished_only=True)
    payload = None if latest is None else await builds.get_artifact(project_id, latest.build_id, "infer")
    if payload is None:
        return {}
    return dict(InferenceReport.model_validate(payload).backlog or {})


def _quarantine(entries: Iterable[QuarantinedEdge]) -> tuple[QuarantinedEdge, ...]:
    """Once per edge (the first entry), by source ``spec_id``, relation and target ``spec_id``."""

    unique: dict[tuple[str, str, str], QuarantinedEdge] = {}
    for entry in entries:
        unique.setdefault((entry.source_spec_id or "", entry.relation, entry.target_spec_id or ""), entry)
    return tuple(unique[key] for key in sorted(unique))
