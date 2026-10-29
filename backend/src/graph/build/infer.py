"""Light inference: domain edges between existing spec nodes, judged by a model (docs/graph/analysis.md
"Light inference"; FR-INF-01..05).

Inference never creates, merges or relabels nodes. Each ontology inference rule names a template
(code, below), a judge prompt, the relation it writes and the template's parameters; no rule id, label
or relation name appears here as a literal. Per rule:

1. Selection reads only ``stated`` and ``implied`` edges (an edge without ``basis`` counts as
   stated), so an edge inferred earlier never makes or unmakes a subject or a candidate (FR-INF-02).
   A subject is a node the template selects that has at least one candidate:

   - ``endpoints``: a ``subject`` node with between ``min_targets`` and ``max_targets`` ``target``
     nodes: those it is linked to by one edge of a ``via`` relation, and with ``mentions`` those
     mentioned (``MENTIONED_IN``) in its ``active`` or ``skipped`` evidence fragments; candidates are
     the pairs of those targets not yet linked by the written relation in either direction. A node
     with at least one target but fewer than ``min_targets`` or more than ``max_targets`` (counted
     over that union) is reported in ``integration_outliers``.
   - ``realization``: a ``subject`` node; candidates are the container nodes reached from its members
     (nodes with a ``member_relation`` edge to it) by 1 to ``max_hops`` ``container_relation`` edges,
     less those it already has the written relation to. At most ``MAX_SHOWN`` candidates (those
     reached from the most members) and ``MAX_SHOWN`` members are shown; a cut subject counts as
     ``truncated``.
   - ``attach_orphan``: a ``subject`` node that reaches no spine root by any spine path and has no
     stated or implied edge of the written relation on one of its ``exclusive`` pairs (attaching it
     would make an exclusive conflict); candidates are the ``max_candidates`` target nodes most
     similar to it by exact ``embedding`` cosine over the project's target nodes (ties by ``spec_id``;
     never the database-wide vector index, so other projects never change them), less those it is
     already linked to by the written relation. One without any candidate is reported in ``orphans_without_candidate``.

2. Judge memory (``JudgeMemory``, ExtractionStore kind ``decisions``): each subject's judgement is
   keyed by the hash of its inputs (the payload without fragment texts, the fragments' ids and
   content hashes, the shown nodes' uuids, the prompt reference and the judge model). A subject with
   a stored judgement for its current inputs is ``reused``: no call, its stored outcome stands. The
   others are judged, at most ``cap`` per rule: first those judged before whose inputs changed, then
   those never judged, each in ``spec_id`` order; the rest are ``deferred``. A build whose diff changed
   nothing judges none of them (no call; all deferred). Each
   judgement is one ``JudgeRunner`` call (payload below). A verdict is ``accepted`` when it names at
   least one candidate, its confidence meets the ``floor`` and every quote is found in its cited
   fragment (FR-INF-03); ``declined`` when it names none (a valid "no link"); ``invalid`` when it names
   an id it was not given (or an illegal direction), or when every ``endpoints`` link it gives is on
   a pair it links both ways (one direction per pair: such a pair is never written, and its other
   links are judged without it); else ``rejected``. A judgement without a
   verdict (``JudgeFailure``) is ``failed`` and is not remembered.

3. The swap (FR-INF-04) writes the new set of inferred edges of every rule in one transaction. A
   judged or reused subject gets exactly the edges of its (stored) accepted verdict; a deferred or
   failed subject keeps its inferred edges, except an ``endpoints`` edge whose two nodes are now linked
   by a non-inferred edge of the relation in either direction. An inferred edge no current subject
   owns (its subject no longer qualifies) is removed. Ownership is read from the template: an
   ``endpoints`` edge belongs to every subject both of whose ends are among its targets, any other
   edge to the subject at one of its ends. An edge whose key already exists as ``stated`` or
   ``implied`` (or as an inferred edge of another rule) is not written; nor is an ``endpoints`` edge
   between nodes already linked by a non-inferred edge of the relation in either direction. A reused
   subject's edges that exist are left as they are. Judge memory is written after the swap commits.

Failure: a judgement that fails with a non-fatal ``LLMError`` is a ``JudgeFailure`` (the subject keeps
its edges); a non-fatal ``LLMError`` that escapes a rule anyway sets that rule's ``error_code`` and
leaves its edges unchanged (FR-INF-05, the build still succeeds). Every other error propagates
(``LLMFatalError``, ``InfrastructureUnavailableError``, ``GraphWriteError``, ``ConformanceError``,
programming errors: tech-specs 7, build.md "Failures"); the swap is one transaction, so the previous
inferred edges are intact whenever ``infer`` raises.

Judge payload (``JudgeRunner`` convention): ``relation`` (the written relation's name), ``subject``
(``name``, ``label``, ``statements``) and, per template, ``targets`` and ``candidate_pairs``
(``endpoints``), ``members`` and ``candidates`` with ``via`` (``realization``), or ``candidates``
(``attach_orphan``); every shown node but the subject has a short local id (``T1``, ``M1``, ``C1``),
never a ``spec_id`` or uuid. ``fragments`` maps fragment id to text: up to ``SUBJECT_FRAGMENTS`` of
the subject's grounding fragments, then ``OTHER_FRAGMENTS`` of each other shown node, at most
``MAX_FRAGMENTS`` in all.

Inferred edge properties (data-model.md "Edges"): ``uuid``, ``edge_key``, ``group_id``, ``basis =
inferred``, ``inference_rule``, ``confidence``, ``evidence`` (JSON list of ``{fragment_id, quote}``),
``judge_model``, ``inferred_at`` (time of the write), ``source_fragment_ids`` (the evidence
fragments), ``created_at``, ``updated_at`` and ``last_build_id``. Reports name edges in
``edge_ref`` form, never by edge key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from graph.build.conform import GROUNDING_STATUSES
from graph.build.consolidate import DECISION_KIND
from graph.build.models import InferenceReport, RuleReport, edge_ref
from graph.build.ports import ExtractionStore
from graph.build.queries import (
    READ_INFER_FRAGMENTS,
    READ_INFER_NODES,
    infer_delete_edges,
    infer_endpoint_subjects,
    infer_exclusive_holders,
    infer_realization_subjects,
    infer_similar_targets,
    infer_write_edges,
    read_current_edges,
)
from graph.build.spine import SpineGraph, load_spine_graph
from graph.build.vectors import get_many, put_many
from graph.core.db import Executor
from graph.core.errors import ConformanceError, LLMError, LLMFatalError
from graph.core.identity import edge_uuid
from graph.core.llm import JudgeFailure, JudgeRunner, Verdict, meets_floor, quotes_valid
from graph.core.ontology import InferenceRule, Ontology
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.infer")

QUOTE_FIELDS: Final = 2
"""Fields of a stored evidence item: fragment id and quote."""
READ_BASES: Final = ("stated", "implied")
"""Edge bases selection reads (FR-INF-02); an edge without ``basis`` counts as the first."""
INFERRED: Final = "inferred"
SUBJECT_FRAGMENTS: Final = 3
OTHER_FRAGMENTS: Final = 1
MAX_FRAGMENTS: Final = 8
STATEMENTS_SHOWN: Final = 3
MAX_SHOWN: Final = 12
"""Most members and most candidates a ``realization`` payload shows."""

SEEN: Final = "seen"
"""Last segment of a judge memory key recording that a subject was judged."""

ENDPOINTS: Final = "endpoints"
REALIZATION: Final = "realization"
ATTACH_ORPHAN: Final = "attach_orphan"

Outcome = Literal["accepted", "declined", "rejected", "invalid"]
OUTCOMES: Final[tuple[Outcome, ...]] = ("accepted", "declined", "rejected", "invalid")


# Verdict models (plan 4.1). Fields after the base's ``confidence`` and ``evidence`` carry local ids.


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


VERDICT_TYPES: Final[dict[str, type[Verdict]]] = {
    ENDPOINTS: EndpointsVerdict,
    REALIZATION: RealizationVerdict,
    ATTACH_ORPHAN: AttachOrphanVerdict,
}


# Judge memory


class JudgeMemory:
    """Stored judgements of one project: ExtractionStore kind ``decisions``, key
    ``<project_id>|<rule id>|<subject spec_id>|<input hash>``, fingerprint ``<prompt>|<judge model>``.
    ``<project_id>|<rule id>|<subject spec_id>|seen`` records that the subject was judged (with its
    last input hash), so a subject whose inputs changed is told from one never judged. Without a store
    nothing is remembered."""

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

    async def recall(self, rule: InferenceRule, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
        if self._store is None or not keys:
            return {}
        return await get_many(self._store, DECISION_KIND, list(keys), self._fingerprint(rule.prompt))

    async def remember(self, rule: InferenceRule, entries: Mapping[str, dict[str, Any]]) -> None:
        if self._store is not None and entries:
            await put_many(self._store, DECISION_KIND, self._fingerprint(rule.prompt), dict(entries))


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


@dataclass(frozen=True)
class _Subject:
    """One subject with its candidates (uuids); ``pairs`` and ``targets`` for ``endpoints``,
    ``members`` and ``via`` (candidate uuid to member uuids) for ``realization``."""

    uuid: str
    spec_id: str
    candidates: tuple[str, ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()
    targets: tuple[str, ...] = ()
    members: tuple[str, ...] = ()
    via: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    truncated: bool = False

    def owns(self, template: str, source: str, target: str) -> bool:
        if template == ENDPOINTS:
            return source in self.targets and target in self.targets
        return self.uuid in (source, target)


@dataclass(frozen=True)
class _CurrentEdge:
    uuid: str | None
    edge_key: str
    source: str
    target: str
    basis: str
    rule: str | None
    ref: str


@dataclass(frozen=True)
class _Proposal:
    source: str
    target: str
    edge_key: str
    ref: str
    confidence: float
    evidence: tuple[tuple[str, str], ...]
    reused: bool = False


@dataclass(frozen=True)
class _Selection:
    """One rule's selection: current edges of its relation, subjects (``spec_id`` order) and lists."""

    current: tuple[_CurrentEdge, ...]
    subjects: tuple[_Subject, ...]
    outliers: tuple[str, ...] = ()
    without_candidate: tuple[str, ...] = ()


@dataclass
class _RulePlan:
    """What one rule decided, before the swap; ``memory``: entries to store once the swap committed."""

    rule: InferenceRule
    report: RuleReport
    subjects: tuple[_Subject, ...]
    judged: frozenset[str]
    accepted: dict[str, _Proposal]
    current: tuple[_CurrentEdge, ...]
    memory: dict[str, dict[str, Any]] = field(default_factory=dict)


def _bases() -> list[str]:
    return list(READ_BASES)


def _basis(value: Any) -> str:
    return str(value) if value else READ_BASES[0]


async def spine_unreached_ids(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    sources: Iterable[str],
    bases: Sequence[str],
    *,
    graph: SpineGraph | None = None,
) -> list[tuple[str, str]]:
    """``(uuid, spec_id)`` of the nodes with a label in ``sources`` that reach no spine root by any
    spine path over edges whose ``basis`` is in ``bases``, in ``spec_id`` order (``graph.build.spine``,
    shared with verification). ``graph``: the build's spine graph when already read."""

    if graph is None:
        graph = await load_spine_graph(executor, project_id, ontology)
    return [(node.key, node.spec_id) for node in graph.unreached(ontology, frozenset(sources), bases)]


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
) -> tuple[list[_Subject], list[str]]:
    """Subjects of an ``endpoints`` rule, and the ``spec_id``s of those outside the target bounds."""

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
        targets = tuple(sorted(set(row["targets"])))
        if not low <= len(targets) <= high:
            outliers.append(row["spec_id"])
            continue
        pairs = tuple(pair for pair in combinations(targets, 2) if frozenset(pair) not in linked)
        if pairs:
            subjects.append(_Subject(row["uuid"], row["spec_id"], candidates=targets, pairs=pairs, targets=targets))
    return subjects, outliers


async def _realization_subjects(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    rule: InferenceRule,
    current: Sequence[_CurrentEdge],
) -> list[_Subject]:
    member, container = rule.member_relation or "", rule.container_relation or ""
    rows = await executor.read(
        infer_realization_subjects(
            member,
            container,
            member_symmetric=ontology.is_symmetric(member),
            container_symmetric=ontology.is_symmetric(container),
            max_hops=rule.max_hops or 1,
        ),
        project_id,
        subject_labels=sorted(rule.subject),
        target_labels=sorted(rule.targets),
        bases=_bases(),
    )
    linked = _linked(current)
    subjects: list[_Subject] = []
    for row in rows:
        via = {
            item["uuid"]: tuple(sorted(item["via"]))
            for item in row["containers"]
            if frozenset((row["uuid"], item["uuid"])) not in linked
        }
        if not via:
            continue
        # Bounded payload: the candidates reached from the most members, then their members first.
        shown = sorted(via, key=lambda uuid: (-len(via[uuid]), uuid))[:MAX_SHOWN]
        members = sorted(set(row["members"]))
        leading = [uuid for uuid in members if any(uuid in via[candidate] for candidate in shown)]
        kept_members = (leading + [uuid for uuid in members if uuid not in leading])[:MAX_SHOWN]
        subjects.append(
            _Subject(
                row["uuid"],
                row["spec_id"],
                candidates=tuple(sorted(shown)),
                members=tuple(sorted(kept_members)),
                via={uuid: via[uuid] for uuid in shown},
                truncated=len(shown) < len(via) or len(kept_members) < len(members),
            )
        )
    return subjects


async def _orphan_subjects(
    executor: Executor, project_id: str, ontology: Ontology, rule: InferenceRule, spine: SpineGraph | None
) -> tuple[list[_Subject], list[str]]:
    """Subjects of an ``attach_orphan`` rule, and the ``spec_id``s of orphans without a candidate;
    ``spine``: the spine graph read at the start of inference (selection writes nothing)."""

    orphans = await spine_unreached_ids(executor, project_id, ontology, rule.subject, READ_BASES, graph=spine)
    exclusive = sorted(
        [source, target] for relation, source, target in ontology.exclusive_pairs if relation == rule.writes
    )
    if orphans and exclusive:
        held = await executor.read(
            infer_exclusive_holders(rule.writes),
            project_id,
            uuids=[uuid for uuid, _ in orphans],
            pairs=exclusive,
            bases=_bases(),
        )
        holders = {row["uuid"] for row in held}
        orphans = [(uuid, spec_id) for uuid, spec_id in orphans if uuid not in holders]
    if not orphans:
        return [], []
    limit = rule.max_candidates or 1
    rows = await executor.read(
        infer_similar_targets(rule.writes, symmetric=ontology.is_symmetric(rule.writes)),
        project_id,
        uuids=[uuid for uuid, _ in orphans],
        target_labels=sorted(rule.targets),
        bases=_bases(),
        limit=limit,
    )
    candidates = {row["uuid"]: tuple(item["uuid"] for item in row["candidates"]) for row in rows}
    subjects: list[_Subject] = []
    without: list[str] = []
    for uuid, spec_id in orphans:
        found = candidates.get(uuid, ())
        if found:
            subjects.append(_Subject(uuid, spec_id, candidates=found))
        else:
            without.append(spec_id)
    return subjects, without


async def _select(
    executor: Executor, project_id: str, ontology: Ontology, rule: InferenceRule, spine: SpineGraph | None = None
) -> _Selection:
    current = await _current_edges(executor, project_id, rule.writes)
    if rule.template == ENDPOINTS:
        subjects, outliers = await _endpoint_subjects(executor, project_id, ontology, rule, current)
        return _Selection(current, tuple(subjects), outliers=tuple(outliers))
    if rule.template == REALIZATION:
        subjects = await _realization_subjects(executor, project_id, ontology, rule, current)
        return _Selection(current, tuple(subjects))
    if rule.template == ATTACH_ORPHAN:
        subjects, without = await _orphan_subjects(executor, project_id, ontology, rule, spine)
        return _Selection(current, tuple(subjects), without_candidate=tuple(without))
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
    )
    nodes: dict[str, _Node] = {}
    for row in rows:
        labels = row["labels"] or []
        if len(labels) != 1 or not row["identity_key"] or not row["spec_id"]:
            continue  # not a valid spec node (verification reports it); never judged or linked
        nodes[row["uuid"]] = _Node(
            uuid=row["uuid"],
            spec_id=row["spec_id"],
            name=row["name"],
            label=labels[0],
            identity_key=row["identity_key"],
            statements=_statement_texts(row["statements"]),
            fragments=tuple(
                (item["fragment_id"], item.get("content_hash") or "")
                for item in row["fragments"] or ()
                if item.get("fragment_id")
            ),
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


def _shown(node: _Node, local_id: str | None = None) -> dict[str, Any]:
    shown: dict[str, Any] = {} if local_id is None else {"id": local_id}
    shown.update({"name": node.name, "label": node.label, "statements": list(node.statements)})
    return shown


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


def _judgement(
    rule: InferenceRule, subject: _Subject, nodes: Mapping[str, _Node], judge_model: str
) -> _Judgement | None:
    node = nodes.get(subject.uuid)
    if node is None:
        return None
    payload: dict[str, Any] = {"relation": rule.writes, "subject": _shown(node)}
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
            _shown(candidate, local_id) for local_id, candidate in zip(ids, candidates, strict=True)
        ]
        others = candidates
    fragments = _fragment_refs(node, others)
    inputs = {
        "prompt": rule.prompt,
        "judge_model": judge_model,
        "subject": subject.uuid,
        "payload": payload,
        "fragments": sorted(fragments.items()),
        "ids": sorted(ids.items()),
    }
    digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return _Judgement(payload, fragments, ids, digest)


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


def _proposal(
    ontology: Ontology,
    relation: str,
    source: _Node,
    target: _Node,
    *,
    confidence: float,
    evidence: Sequence[tuple[str, str]],
    reused: bool = False,
) -> _Proposal | None:
    """The edge as stored (a symmetric relation puts the lower identity key first); None when the
    pair is not legal for the relation."""

    if not ontology.is_legal(relation, source.label, target.label):
        return None
    if ontology.is_symmetric(relation) and target.identity_key < source.identity_key:
        source, target = target, source
    return _Proposal(
        source=source.uuid,
        target=target.uuid,
        edge_key=f"{source.identity_key}|{relation}|{target.identity_key}",
        ref=edge_ref(source.spec_id, relation, target.spec_id),
        confidence=float(confidence),
        evidence=tuple(evidence),
        reused=reused,
    )


def _evaluate(
    ontology: Ontology,
    rule: InferenceRule,
    subject: _Subject,
    judgement: _Judgement,
    verdict: Verdict,
    *,
    nodes: Mapping[str, _Node],
    texts: Mapping[str, str],
) -> tuple[Outcome, list[_Proposal]]:
    pairs = _proposed_pairs(rule, subject, verdict, judgement.ids)
    if pairs is None:
        return "invalid", []
    if not pairs:
        return "declined", []
    evidence = [(item.fragment_id, item.quote) for item in verdict.evidence]
    proposals: list[_Proposal] = []
    for source_uuid, target_uuid in pairs:
        proposal = _proposal(
            ontology,
            rule.writes,
            nodes[source_uuid],
            nodes[target_uuid],
            confidence=verdict.confidence,
            evidence=evidence,
        )
        if proposal is None:
            return "invalid", []
        proposals.append(proposal)
    if not meets_floor(verdict, ontology.inference.floor) or not quotes_valid(verdict, texts):
        return "rejected", []
    return "accepted", proposals


def _memory_entry(outcome: Outcome, proposals: Sequence[_Proposal]) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "edges": [
            {
                "source": p.source,
                "target": p.target,
                "confidence": p.confidence,
                "evidence": [list(e) for e in p.evidence],
            }
            for p in proposals
        ],
    }


def _recalled(
    ontology: Ontology, rule: InferenceRule, entry: Mapping[str, Any], nodes: Mapping[str, _Node]
) -> list[_Proposal] | None:
    """The accepted edges of a stored judgement; None when it does not read back."""

    edges = entry.get("edges")
    if entry.get("outcome") not in OUTCOMES or not isinstance(edges, list):
        return None
    proposals: list[_Proposal] = []
    for item in edges:
        if not isinstance(item, dict):
            return None
        source, target = nodes.get(item.get("source", "")), nodes.get(item.get("target", ""))
        confidence, evidence = item.get("confidence"), item.get("evidence")
        if (
            source is None
            or target is None
            or not isinstance(confidence, int | float)
            or not isinstance(evidence, list)
        ):
            return None
        quotes = [(str(e[0]), str(e[1])) for e in evidence if isinstance(e, list) and len(e) == QUOTE_FIELDS]
        proposal = _proposal(ontology, rule.writes, source, target, confidence=confidence, evidence=quotes, reused=True)
        if proposal is None:
            return None
        proposals.append(proposal)
    if rule.template == ENDPOINTS and _both_ways((p.source, p.target) for p in proposals):
        return None  # stored before one direction per pair: judged again
    return proposals


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


async def _judge_rule(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    judge: JudgeRunner,
    memory: JudgeMemory,
    *,
    rule: InferenceRule,
    selection: _Selection,
    judge_new: bool = True,
) -> _RulePlan:
    """Reuse remembered judgements, judge up to ``cap`` of the other subjects and collect the accepted
    edges: subjects judged before whose inputs changed first, then subjects never judged, each in
    ``spec_id`` order. With ``judge_new`` false (the build changed nothing) no subject is judged and
    every one without a stored judgement for its inputs is deferred. A subject whose nodes cannot be
    read any more (or are malformed) is not judged and counts as deferred: it keeps its inferred
    edges."""

    nodes = await _nodes(
        executor,
        project_id,
        ontology,
        (uuid for subject in selection.subjects for uuid in (subject.uuid, *subject.candidates, *subject.members)),
    )
    judgements = [(subject, _judgement(rule, subject, nodes, memory.judge_model)) for subject in selection.subjects]
    readable = [(subject, judgement) for subject, judgement in judgements if judgement is not None]
    keys = {subject.uuid: memory.key(rule, subject.spec_id, judgement.digest) for subject, judgement in readable}
    stored = await memory.recall(rule, list(keys.values()))

    judged: set[str] = set()
    accepted: dict[str, _Proposal] = {}
    fresh: list[tuple[_Subject, _Judgement]] = []
    reused = 0
    for subject, judgement in readable:
        entry = stored.get(keys[subject.uuid])
        proposals = None if entry is None else _recalled(ontology, rule, entry, nodes)
        if proposals is None:
            fresh.append((subject, judgement))
            continue
        reused += 1
        judged.add(subject.uuid)
        for proposal in proposals:
            _keep_best(accepted, proposal)

    seen = await memory.seen(rule, (subject.spec_id for subject, _ in fresh)) if judge_new else set()
    fresh.sort(key=lambda item: item[0].spec_id not in seen)  # stable: spec_id order within each group
    cap = ontology.inference.cap if judge_new else 0
    chosen, deferred = fresh[:cap], fresh[cap:]
    texts = await _fragment_texts(
        executor, project_id, (fragment_id for _, judgement in chosen for fragment_id in judgement.fragments)
    )
    shown = [
        (subject, judgement, {fid: texts[fid] for fid in judgement.fragments if fid in texts})
        for subject, judgement in chosen
    ]
    verdict_type = VERDICT_TYPES[rule.template]
    results = await _gather(
        [
            judge.judge(rule.prompt, {**judgement.payload, "fragments": fragments}, verdict_type)
            for _, judgement, fragments in shown
        ]
    )
    counts: dict[str, int] = dict.fromkeys((*OUTCOMES, "failed"), 0)
    remember: dict[str, dict[str, Any]] = {}
    for (subject, judgement, fragments), result in zip(shown, results, strict=True):
        if isinstance(result, JudgeFailure):
            counts["failed"] += 1
            continue
        outcome, proposals = _evaluate(ontology, rule, subject, judgement, result, nodes=nodes, texts=fragments)
        counts[outcome] += 1
        judged.add(subject.uuid)
        remember[keys[subject.uuid]] = _memory_entry(outcome, proposals)
        remember[memory.seen_key(rule, subject.spec_id)] = {"digest": judgement.digest}
        for proposal in proposals:
            _keep_best(accepted, proposal)

    report = RuleReport(
        rule=rule.id,
        subjects=len(selection.subjects),
        judged=sum(counts[name] for name in OUTCOMES),
        accepted=counts["accepted"],
        declined=counts["declined"],
        rejected=counts["rejected"],
        invalid=counts["invalid"],
        failed=counts["failed"],
        deferred=len(deferred) + len(judgements) - len(readable),
        reused=reused,
        truncated=sum(1 for subject in selection.subjects if subject.truncated),
    )
    return _RulePlan(rule, report, selection.subjects, frozenset(judged), accepted, selection.current, remember)


# Swap


@dataclass
class _Swap:
    deletes: list[dict[str, Any]] = field(default_factory=list)
    creates: list[dict[str, Any]] = field(default_factory=list)
    refreshes: list[dict[str, Any]] = field(default_factory=list)


def _swap_rows(plan: _RulePlan, *, project_id: str, build_id: str, at: str, judge_model: str) -> _Swap:
    rule = plan.rule
    template = rule.template
    unjudged = [subject for subject in plan.subjects if subject.uuid not in plan.judged]
    old = [edge for edge in plan.current if edge.basis == INFERRED and edge.rule == rule.id]
    old_keys = {edge.edge_key for edge in old}
    taken = {edge.edge_key for edge in plan.current if edge.edge_key not in old_keys}
    linked = _linked(plan.current) if template == ENDPOINTS else set()
    kept = {
        edge.edge_key
        for edge in old
        if frozenset((edge.source, edge.target)) not in linked
        and any(subject.owns(template, edge.source, edge.target) for subject in unjudged)
    }
    swap = _Swap()
    for edge in old:
        if edge.edge_key not in kept and edge.edge_key not in plan.accepted:
            swap.deletes.append({"edge_key": edge.edge_key, "uuid": edge.uuid, "rule": rule.id, "ref": edge.ref})
    used = {edge.uuid for edge in plan.current if edge.uuid}
    for key in sorted(plan.accepted):
        proposal = plan.accepted[key]
        if key in taken or (proposal.reused and key in old_keys):
            continue  # already stated, implied or another rule's; or a reused judgement's edge in place
        evidence = [{"fragment_id": fragment_id, "quote": quote} for fragment_id, quote in proposal.evidence]
        row = {
            "source": proposal.source,
            "target": proposal.target,
            "edge_key": key,
            "ref": proposal.ref,
            "uuid": _new_uuid(project_id, key, used),
            "created_at": at,
            "rule": rule.id,
            "either_direction": template == ENDPOINTS,
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
        (swap.refreshes if key in old_keys else swap.creates).append(row)
    return swap


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


async def _apply(executor: Executor, project_id: str, swaps: Sequence[tuple[_RulePlan, _Swap]]) -> dict[str, _Applied]:
    """Every rule's deletes and writes in one transaction; what was added, removed (``edge_ref``s) and
    updated, per rule id. ``inferred_at`` is the time of the write."""

    applied: dict[str, _Applied] = {}
    inferred_at = datetime.now(UTC).isoformat(timespec="seconds")
    async with executor.tx(project_id) as transaction:
        for plan, swap in swaps:
            relation = plan.rule.writes
            added: list[str] = []
            removed: list[str] = []
            updated = 0
            if swap.deletes:
                records = await transaction.run(infer_delete_edges(relation), rows=swap.deletes, inferred=INFERRED)
                removed = list(records[0]["refs"]) if records else []
            for rows, creating in ((swap.creates, True), (swap.refreshes, False)):
                if not rows:
                    continue
                stamped = [{**row, "props": {**row["props"], "inferred_at": inferred_at}} for row in rows]
                records = await transaction.run(infer_write_edges(relation), rows=stamped, inferred=INFERRED)
                if creating:
                    added = list(records[0]["refs"]) if records else []
                else:
                    updated = int(records[0]["written"]) if records else 0
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


async def infer(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    judge: JudgeRunner,
    *,
    stamp: InferenceStamp,
    memory: JudgeMemory | None = None,
    graph_changed: bool = True,
) -> InferenceReport:
    """Run every inference rule of ``ontology`` once and swap in the new inferred edges (module doc).

    A non-fatal model error of a rule is reported (FR-INF-05); every other error propagates with the
    previous inferred edges intact. ``memory`` defaults to none (every subject judged).
    ``graph_changed`` false (the build's diff changed nothing): no judge call; remembered judgements
    are reused and every other subject stays deferred (the backlog waits for a build that changes
    something). ``stamp`` gives the build id, time and judge model the swapped-in edges carry.
    """

    build_id, at, judge_model = stamp.build_id, stamp.at, stamp.judge_model
    memory = memory or JudgeMemory(None, project_id, judge_model)
    # Read once per build: selection writes nothing, so the orphan rule walks the same graph.
    spine = await load_spine_graph(executor, project_id, ontology)
    spine_before = len(spine.unreached(ontology, ontology.spine.sources, READ_BASES))

    error_code: str | None = None
    plans: list[_RulePlan] = []
    reports: dict[str, RuleReport] = {}
    outliers: list[str] = []
    orphans: list[str] = []
    for rule in ontology.inference.rules:
        selection: _Selection | None = None
        try:
            selection = await _select(executor, project_id, ontology, rule, spine)
            outliers.extend(selection.outliers)
            orphans.extend(selection.without_candidate)
            plan = await _judge_rule(
                executor, project_id, ontology, judge, memory, rule=rule, selection=selection, judge_new=graph_changed
            )
        except LLMError as error:
            if isinstance(error, LLMFatalError):
                raise
            code = _error_code(error, "inference_rule_failed")
            error_code = error_code or code
            partial = (
                RuleReport(rule=rule.id)
                if selection is None
                else RuleReport(rule=rule.id, subjects=len(selection.subjects))
            )
            reports[rule.id] = partial.model_copy(update={"error_code": code})
            log_event(logger, WARNING, "graph.infer.rule_failed", build_id=build_id, rule=rule.id, error_code=code)
            continue
        plans.append(plan)
        reports[rule.id] = plan.report

    added: list[str] = []
    removed: list[str] = []
    if plans:
        swaps = [
            (plan, _swap_rows(plan, project_id=project_id, build_id=build_id, at=at, judge_model=judge_model))
            for plan in plans
        ]
        applied = await _apply(executor, project_id, swaps)
        for plan in plans:
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
            await memory.remember(plan.rule, plan.memory)

    for report in reports.values():
        log_event(logger, INFO, "graph.infer.rule_done", build_id=build_id, **report.model_dump())
    return InferenceReport(
        enabled=True,
        rules=tuple(reports[rule.id] for rule in ontology.inference.rules if rule.id in reports),
        integration_outliers=tuple(dict.fromkeys(outliers)),
        orphans_without_candidate=tuple(dict.fromkeys(orphans)),
        spine_unreachable_before=spine_before,
        inferred_added=tuple(sorted(set(added))),
        inferred_removed=tuple(sorted(set(removed) - set(added))),
        error_code=error_code,
    )
