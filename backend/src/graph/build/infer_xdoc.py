"""Template ``cross_document`` of light inference ("Cross-document links";; the shipped rule is ``INF-XDOC-01``, last in
the ontology file).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any, Final

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from graph.build.models import JudgedItem, JudgedItemKind
from graph.build.queries import INFER_XDOC_DOCUMENTS, INFER_XDOC_EMBEDDINGS, INFER_XDOC_MEMBERS
from graph.build.resolve import hub_words
from graph.build.terms import NameIndex, hub_name_index
from graph.core.db import Executor
from graph.core.llm import Evidence, Verdict, meets_floor, quotes_valid
from graph.core.ontology import QUOTE_ATTRIBUTE, InferenceRule, Ontology

MAX_FRAGMENTS: Final = 20
"""Most evidence fragments one ``cross_document`` payload shows."""
MIN_DOCUMENTS: Final = 2
DOCUMENT_SEPARATOR: Final = "; "
"""Joins the titles of a member grounded in several documents."""

LINK: Final[JudgedItemKind] = "link"
OWNER: Final[JudgedItemKind] = "owner"

ACCEPTED: Final = "accepted"
DOWNGRADED: Final = "downgraded"
BELOW_FLOOR: Final = "below_floor"
QUOTE_MISSING: Final = "quote_missing"
INVALID: Final = "invalid"
ADMITTED: Final = frozenset({ACCEPTED, DOWNGRADED})
"""Item outcomes whose edge is written (after the shared validator)."""


# Verdict model


class CrossDocumentLink(BaseModel):
    """One link between the two members of a pair."""

    model_config = ConfigDict(frozen=True)

    a: str = Field(description="id of one member of the pair")
    b: str = Field(description="id of the other member of the pair")
    relation: str = Field(description="one of the payload's relations")
    source: str = Field(description="id of the member the relation starts from: for a dependency, the one that waits")
    confidence: float = Field(ge=0.0, le=1.0, description="how sure you are of this link, from 0 to 1")
    evidence: list[Evidence] = Field(description="quotes, at least one from a fragment of each member")


class CrossDocumentOwner(BaseModel):
    """The owner candidate one orphan member belongs to."""

    model_config = ConfigDict(frozen=True)

    member: str = Field(description="id of the member with no owner")
    owner: str = Field(description="id of one of that member's candidates")
    confidence: float = Field(ge=0.0, le=1.0, description="how sure you are of this owner, from 0 to 1")
    evidence: list[Evidence] = Field(description="quotes from the member's fragment and from the owner's")


class CrossDocumentVerdict(Verdict):
    """Template cross_document: the links between member pairs and the owners of orphan members; each
    item is judged on its own (its own confidence and evidence), the top-level fields are not used."""

    confidence: float = Field(
        ge=0.0, le=1.0, description="how sure you are of the answer as a whole; each link and owner has its own"
    )
    evidence: list[Evidence] = Field(
        description="passages the answer as a whole rests on; each link and owner cites its own"
    )
    links: list[CrossDocumentLink] = Field(description="one per pair the fragments link; empty when none")
    owners: list[CrossDocumentOwner] = Field(
        description="one per member the fragments place in one of its candidates; empty when none"
    )


# Selection


@dataclass(frozen=True)
class Hub:
    """A hub with its members: member uuid to the ids of the documents of its evidence fragments."""

    uuid: str
    spec_id: str
    members: Mapping[str, frozenset[str]]


async def read_hubs(
    executor: Executor, project_id: str, rule: InferenceRule, *, bases: Sequence[str], statuses: Sequence[str]
) -> list[Hub]:
    """Every hub of the rule's ``subject`` labels with at least two members (module doc: a member by a
    shared evidence fragment only when its own text names the hub), in ``spec_id`` order; read through
    ``bases`` edges and ``statuses`` fragments only."""

    rows = await executor.read(
        INFER_XDOC_MEMBERS,
        project_id,
        subject_labels=sorted(rule.subject),
        member_labels=sorted(rule.targets),
        via=list(rule.via),
        mentions=rule.mentions,
        bases=list(bases),
        statuses=list(statuses),
        quote=QUOTE_ATTRIBUTE,
    )
    hubs: list[Hub] = []
    for row in rows:
        index = hub_name_index([(row["uuid"], row.get("name") or "", row.get("keys") or ())])
        members = {
            item["uuid"]: frozenset(item["documents"])
            for item in row["members"]
            if item.get("shared") is None or names_hub(index, own_texts(item))
        }
        if len(members) > 1:
            hubs.append(Hub(row["uuid"], row["spec_id"], members))
    return hubs


def own_texts(item: Mapping[str, Any]) -> list[str]:
    """The texts of a member grounded in a fragment of the hub's own (``shared``: those fragment ids):
    its name, its quote and its statements in those fragments."""

    shared = set(item.get("shared") or ())
    texts = [item.get("name") or "", item.get("quote") or ""]
    statements = item.get("statements")
    if isinstance(statements, str):
        try:
            statements = json.loads(statements)
        except ValueError:
            statements = None
    texts.extend(
        entry["text"]
        for entry in (statements if isinstance(statements, list) else ())
        if isinstance(entry, dict) and entry.get("fragment_id") in shared and isinstance(entry.get("text"), str)
    )
    return [text for text in texts if isinstance(text, str) and text]


def names_hub(index: NameIndex, texts: Iterable[str]) -> bool:
    """Whether one of ``texts`` names the hub of ``index`` (its name or a ``surface`` alias, in hub form)."""

    return bool(index) and any(index.count(hub_words(text)) for text in texts)


def _documents_apart(documents: Mapping[str, frozenset[str]], first: str, second: str) -> bool:
    return not documents[first] & documents[second]


def candidate_hubs(rule: InferenceRule, hubs: Iterable[Hub], keep: Callable[[str], bool]) -> list[Hub]:
    """The hubs that are subjects, each with the members ``keep`` keeps: their documents number at
    least ``min_documents`` and two of them share no document."""

    found: list[Hub] = []
    for hub in hubs:
        members = {uuid: documents for uuid, documents in hub.members.items() if documents and keep(uuid)}
        documents = frozenset().union(*members.values())
        if len(documents) < (rule.min_documents or MIN_DOCUMENTS):
            continue
        if any(_documents_apart(members, a, b) for a, b in combinations(sorted(members), 2)):
            found.append(Hub(hub.uuid, hub.spec_id, members))
    return found


def open_pairs(
    members: Iterable[str],
    documents: Mapping[str, frozenset[str]],
    *,
    linked: set[frozenset[str]],
    owners: Mapping[str, Iterable[str]],
) -> list[tuple[str, str]]:
    """The member pairs that may be judged, before the cosine: no shared document, not ``linked``,
    not of one owner."""

    pairs: list[tuple[str, str]] = []
    for a, b in combinations(sorted(members), 2):
        if not _documents_apart(documents, a, b) or frozenset((a, b)) in linked:
            continue
        if set(owners.get(a, ())) & set(owners.get(b, ())):
            continue
        pairs.append((a, b))
    return pairs


async def member_embeddings(executor: Executor, project_id: str, uuids: Iterable[str]) -> dict[str, np.ndarray]:
    """The embedding of each node of ``uuids`` (one read for all, as float64); a node without one has none."""

    wanted = sorted(set(uuids))
    if not wanted:
        return {}
    rows = await executor.read(INFER_XDOC_EMBEDDINGS, project_id, uuids=wanted)
    return {row["uuid"]: np.asarray(row["embedding"], dtype=np.float64) for row in rows if row["embedding"]}


def pair_cosines(pairs: Sequence[tuple[str, str]], embeddings: Mapping[str, np.ndarray]) -> dict[frozenset[str], float]:
    """The exact embedding cosine of each of one hub's ``pairs`` as Neo4j's ``vector.similarity.cosine``
    gives it (up to its float32 rounding), (1 + cos) / 2, the scale of ``pair_floor``; a pair with an
    end without an embedding (or with a zero one) has none."""

    ends = sorted({uuid for pair in pairs for uuid in pair if uuid in embeddings})
    if not ends:
        return {}
    matrix = np.stack([embeddings[uuid] for uuid in ends])
    norms = np.linalg.norm(matrix, axis=1)
    usable = norms > 0.0
    unit = np.divide(matrix, norms[:, None], out=np.zeros_like(matrix), where=usable[:, None])
    scores = (1.0 + np.clip(unit @ unit.T, -1.0, 1.0)) / 2.0
    index = {uuid: position for position, uuid in enumerate(ends) if usable[position]}
    return {frozenset((a, b)): float(scores[index[a], index[b]]) for a, b in pairs if a in index and b in index}


async def document_titles(executor: Executor, project_id: str, document_ids: Iterable[str]) -> dict[str, str]:
    """Each document's title (else its path, else its id); a document not found has none."""

    wanted = sorted(set(document_ids))
    if not wanted:
        return {}
    rows = await executor.read(INFER_XDOC_DOCUMENTS, project_id, document_ids=wanted)
    return {row["document_id"]: row["title"] for row in rows}


def document_label(documents: Iterable[str], titles: Mapping[str, str]) -> str:
    """The ``document`` a payload shows for a member: its documents' titles (ids when unknown), sorted."""

    return DOCUMENT_SEPARATOR.join(sorted({titles.get(document, document) for document in documents}))


def owner_relation(ontology: Ontology, rule: InferenceRule, source_label: str, target_label: str) -> str | None:
    """The first relation the rule writes with an ``owner`` pair from ``source_label`` to ``target_label``."""

    for relation in rule.written:
        pair = ontology.pair_rule(relation, source_label, target_label)
        if pair is not None and pair.owner:
            return relation
    return None


def _owner_fallback(ontology: Ontology, rule: InferenceRule) -> str:
    """The relation an owner item names when its ends are unknown: the rule's first owner relation."""

    return next(
        (relation for relation in rule.written if any(pair.owner for pair in ontology.pairs(relation))), rule.writes
    )


@dataclass(frozen=True)
class Choice:
    """A subject's judged pairs (best first), their members (``spec_id`` order) and each orphan
    member's owner candidates."""

    pairs: tuple[tuple[str, str], ...]
    members: tuple[str, ...]
    candidates: Mapping[str, tuple[str, ...]]


def choose(
    ontology: Ontology,
    rule: InferenceRule,
    hub: str,
    pairs: Iterable[tuple[str, str]],
    cosines: Mapping[frozenset[str], float],
    *,
    owners: Mapping[str, Iterable[str]],
    labels: Mapping[str, str],
    spec_ids: Mapping[str, str],
) -> Choice | None:
    """The ``max_pairs`` best ``pairs`` at least ``pair_floor`` and the owner candidates of their
    orphan members (module doc); None when no pair qualifies."""

    floor = rule.pair_floor or 0.0
    scored = [(score, pair) for pair in pairs if (score := cosines.get(frozenset(pair))) is not None and score >= floor]
    scored.sort(key=lambda item: (-item[0], spec_ids[item[1][0]], spec_ids[item[1][1]]))
    chosen = tuple(pair for _, pair in scored[: rule.max_pairs or 1])
    if not chosen:
        return None
    members = tuple(sorted({uuid for pair in chosen for uuid in pair}, key=lambda uuid: (spec_ids[uuid], uuid)))
    candidates: dict[str, tuple[str, ...]] = {}
    for member in members:
        if set(owners.get(member, ())):
            continue  # owned: no owner candidates
        found: list[str] = []
        for a, b in chosen:
            if member in (a, b):
                other = b if member == a else a
                found.extend(sorted(owners.get(other, ()), key=lambda uuid: (spec_ids.get(uuid, ""), uuid)))
        found.append(hub)
        legal = [
            uuid
            for uuid in dict.fromkeys(found)
            if uuid in labels and owner_relation(ontology, rule, labels[member], labels[uuid]) is not None
        ]
        if legal:
            candidates[member] = tuple(legal[: rule.max_candidates or 1])
    return Choice(chosen, members, candidates)


def link_relations(ontology: Ontology, rule: InferenceRule, label_pairs: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """The relations the rule writes that are legal, not as an owner pair, between the ends of one of
    ``label_pairs`` in either direction (in the rule's order)."""

    def legal(relation: str, source: str, target: str) -> bool:
        pair = ontology.pair_rule(relation, source, target)
        return pair is not None and not pair.owner

    wanted = set(label_pairs)
    return tuple(
        relation
        for relation in rule.written
        if any(legal(relation, a, b) or legal(relation, b, a) for a, b in sorted(wanted))
    )


# Payload


@dataclass(frozen=True)
class Shown:
    """A node as a payload shows it: ``view`` (``name``, ``label``, ``statements``) and its first
    grounding fragments (id, content hash) in document order."""

    uuid: str
    view: Mapping[str, Any]
    fragments: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class View:
    """What one subject's payload shows: the hub, the ``members`` (``spec_id`` order), the ``pairs``
    (best first), each member's ``documents`` title and ``owners`` name, each orphan member's owner
    ``candidates`` and the ``labels`` of the shown nodes."""

    hub: Shown
    members: Sequence[Shown]
    pairs: Sequence[tuple[str, str]]
    documents: Mapping[str, str]
    owners: Mapping[str, str]
    candidates: Mapping[str, Sequence[Shown]]
    labels: Mapping[str, str]


@dataclass(frozen=True)
class Parts:
    """A judgement's payload (without ``fragments``), the fragments it shows (id to content hash) and
    its local ids (to node uuid)."""

    payload: dict[str, Any]
    fragments: dict[str, str]
    ids: dict[str, str]


def judgement_parts(ontology: Ontology, rule: InferenceRule, view: View) -> Parts | None:
    """The payload of one subject (module doc); None when no pair has both members shown."""

    members, candidates, labels = view.members, view.candidates, view.labels
    member_ids = {member.uuid: f"M{index}" for index, member in enumerate(members, 1)}
    shown_pairs = [(a, b) for a, b in view.pairs if a in member_ids and b in member_ids]
    if not shown_pairs:
        return None
    owner_ids: dict[str, str] = {}
    owner_nodes: list[Shown] = []
    for member in members:
        for candidate in candidates.get(member.uuid, ()):
            if candidate.uuid not in owner_ids:
                owner_ids[candidate.uuid] = f"O{len(owner_ids) + 1}"
                owner_nodes.append(candidate)
    fragments = _fragments(members, owner_nodes, shown_pairs)

    def blocks(node: Shown) -> list[str]:
        return [fragment_id for fragment_id, _ in node.fragments if fragment_id in fragments]

    payload: dict[str, Any] = {
        "hub": dict(view.hub.view),
        "members": [
            {
                "id": member_ids[member.uuid],
                "name": member.view["name"],
                "label": member.view["label"],
                "document": view.documents.get(member.uuid, ""),
                "owner": view.owners.get(member.uuid),
                "statements": list(member.view["statements"]),
                "fragments": blocks(member),
            }
            for member in members
        ],
        "pairs": [sorted((member_ids[a], member_ids[b])) for a, b in shown_pairs],
        "relations": list(link_relations(ontology, rule, ((labels[a], labels[b]) for a, b in shown_pairs))),
        "owners": [
            {
                "member": member_ids[member.uuid],
                "candidates": [
                    {
                        "id": owner_ids[node.uuid],
                        "name": node.view["name"],
                        "label": node.view["label"],
                        "fragments": blocks(node),
                    }
                    for node in candidates[member.uuid]
                ],
            }
            for member in members
            if candidates.get(member.uuid)
        ],
    }
    ids = {local: uuid for uuid, local in (*member_ids.items(), *owner_ids.items())}
    return Parts(payload, fragments, ids)


def _fragments(members: Sequence[Shown], owners: Sequence[Shown], pairs: Sequence[tuple[str, str]]) -> dict[str, str]:
    """At most ``MAX_FRAGMENTS``, in this order: each member's first, each owner candidate's first, then
    the members' further fragments in pair order. Each owner candidate's first is reserved, so the
    members' fragments fill only the room the owner candidates leave."""

    by_uuid = {member.uuid: member for member in members}
    firsts = [member.fragments[0] for member in members if member.fragments]
    reserved = list(dict.fromkeys(owner.fragments[0] for owner in owners if owner.fragments))[:MAX_FRAGMENTS]
    further = [fragment for pair in pairs for uuid in pair for fragment in by_uuid[uuid].fragments[1:]]
    taken = set(reserved)
    for fragment in (*firsts, *further):
        if len(taken) == MAX_FRAGMENTS:
            break
        taken.add(fragment)
    return {
        fragment_id: content_hash
        for fragment_id, content_hash in (*firsts, *reserved, *further)
        if (fragment_id, content_hash) in taken
    }


# Items (checks 1-4)


@dataclass(frozen=True)
class Item:
    """One verdict item after checks 1-4. ``source`` and ``target``: node uuids (None for an id it was
    not given); ``relation``: as it is to be written (the rule's ``writes`` when downgraded; as given
    when invalid); ``evidence``: ``(fragment id, quote)``, the source side first."""

    kind: JudgedItemKind
    relation: str
    source: str | None
    target: str | None
    outcome: str
    confidence: float
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Given:
    """What one judgement was given, by node uuid: its local ``ids`` (to uuid), ``pairs``, each orphan
    member's owner ``candidates``, the payload's ``relations``, each shown node's ``labels`` and its
    fragments in the payload (``ends``), and the shown fragments' ``texts``."""

    ids: Mapping[str, str]
    pairs: Sequence[tuple[str, str]]
    candidates: Mapping[str, Sequence[str]]
    relations: Sequence[str]
    labels: Mapping[str, str]
    ends: Mapping[str, frozenset[str]]
    texts: Mapping[str, str]


def _quotes(
    confidence: float,
    evidence: Sequence[Evidence],
    ends: Sequence[frozenset[str]],
    texts: Mapping[str, str],
    *,
    floor: float,
) -> str:
    """Checks 2 and 3: every quote found, one in each end's fragments; then the rule's ``floor``."""

    quoted = Verdict(confidence=confidence, evidence=list(evidence))
    if not quotes_valid(quoted, texts):
        return QUOTE_MISSING
    if not all(any(item.fragment_id in end for item in evidence) for end in ends):
        return QUOTE_MISSING
    return ACCEPTED if meets_floor(quoted, floor) else BELOW_FLOOR


def _source_first(evidence: Sequence[Evidence], source: frozenset[str]) -> tuple[tuple[str, str], ...]:
    return _sorted_source_first([(item.fragment_id, item.quote) for item in evidence], source)


def _sorted_source_first(quotes: Iterable[tuple[str, str]], source: frozenset[str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(quotes, key=lambda quote: quote[0] not in source))


def reversed_item(item: Item, source: frozenset[str]) -> Item:
    """``item`` with its ends swapped (a symmetric relation stored the other way round, a downgraded
    item included) and its evidence with the new source's fragments (``source``) first."""

    return replace(item, source=item.target, target=item.source, evidence=_sorted_source_first(item.evidence, source))


def _cued(ontology: Ontology, relation: str, evidence: Sequence[Evidence], source: frozenset[str]) -> bool:
    return any(item.fragment_id in source and ontology.cue_matches(relation, item.quote) for item in evidence)


def _invalid(kind: JudgedItemKind, relation: str, ends: tuple[str | None, str | None], answer: Any) -> Item:
    """An item that fails check 1 (no edge, so never quarantined)."""

    evidence = _source_first(answer.evidence, frozenset())
    return Item(kind, relation, ends[0], ends[1], INVALID, answer.confidence, evidence)


def _owner_items(ontology: Ontology, rule: InferenceRule, verdict: CrossDocumentVerdict, given: Given) -> list[Item]:
    ids, labels, ends = given.ids, given.labels, given.ends
    items: list[Item] = []
    placed: set[str] = set()
    for answer in verdict.owners:
        member, owner = ids.get(answer.member), ids.get(answer.owner)
        if member is None or owner is None or owner not in given.candidates.get(member, ()) or member in placed:
            items.append(_invalid(OWNER, _owner_fallback(ontology, rule), (member, owner), answer))
            continue
        relation = owner_relation(ontology, rule, labels[member], labels[owner])
        if relation is None:
            items.append(_invalid(OWNER, _owner_fallback(ontology, rule), (member, owner), answer))
            continue
        placed.add(member)
        outcome = _quotes(
            answer.confidence,
            answer.evidence,
            (ends[member], ends[owner]),
            given.texts,
            floor=ontology.inference.floor,
        )
        evidence = _source_first(answer.evidence, ends[member])
        items.append(Item(OWNER, relation, member, owner, outcome, answer.confidence, evidence))
    return items


def _link_items(ontology: Ontology, rule: InferenceRule, verdict: CrossDocumentVerdict, given: Given) -> list[Item]:
    ids, ends = given.ids, given.ends
    allowed = {frozenset(pair) for pair in given.pairs}
    linked: set[frozenset[str]] = set()
    items: list[Item] = []
    for answer in verdict.links:
        a, b, source = ids.get(answer.a), ids.get(answer.b), ids.get(answer.source)
        if a is None or b is None or source not in (a, b) or answer.relation not in given.relations:
            items.append(_invalid(LINK, answer.relation, (a, b), answer))
            continue
        pair = frozenset((a, b))
        if pair not in allowed or pair in linked:
            items.append(_invalid(LINK, answer.relation, (a, b), answer))
            continue
        linked.add(pair)
        target = b if source == a else a
        outcome = _quotes(
            answer.confidence,
            answer.evidence,
            (ends[source], ends[target]),
            given.texts,
            floor=ontology.inference.floor_of(rule),
        )
        relation = answer.relation
        if outcome == ACCEPTED and ontology.relation(relation).stated_only:
            gated = rule.id in ontology.cue_gated_rules(relation)
            if not (gated and _cued(ontology, relation, answer.evidence, ends[source])):
                relation, outcome = rule.writes, DOWNGRADED
        evidence = _source_first(answer.evidence, ends[source])
        items.append(Item(LINK, relation, source, target, outcome, answer.confidence, evidence))
    return items


def check_items(ontology: Ontology, rule: InferenceRule, verdict: CrossDocumentVerdict, given: Given) -> list[Item]:
    """The verdict's items after checks 1-4 (module doc), owners first, then links, each in the
    verdict's order."""

    return [*_owner_items(ontology, rule, verdict, given), *_link_items(ontology, rule, verdict, given)]


def judged_item(
    rule: InferenceRule,
    subject: str,
    item: Item,
    *,
    outcome: str,
    ends: tuple[str | None, str | None],
    spec_ids: Mapping[str, str],
) -> JudgedItem:
    """The report entry of ``item`` (``JudgedItem``): ``ends`` as written (a symmetric relation's
    stored order) or as judged, by ``spec_id``; ``outcome`` after check 5; with its quotes
    (``evidence``), which ``infer`` keeps only in a dry run's report."""

    source, target = ends
    return JudgedItem(
        rule=rule.id,
        kind=item.kind,
        subject=subject,
        source_spec_id=spec_ids.get(source) if source is not None else None,
        relation=item.relation,
        target_spec_id=spec_ids.get(target) if target is not None else None,
        outcome=outcome,
        confidence=item.confidence,
        fragment_ids=tuple(dict.fromkeys(fragment_id for fragment_id, _ in item.evidence)),
        evidence=item.evidence,
    )


def items_by_outcome(items: Iterable[JudgedItem]) -> dict[str, int]:
    """Judged items per outcome, sorted by outcome."""

    return dict(sorted(Counter(item.outcome for item in items).items()))


def item_order(item: JudgedItem) -> tuple[str, str, str, str, str]:
    """The report order of judged items: rule, subject, source, relation, target."""

    return (item.rule, item.subject or "", item.source_spec_id or "", item.relation, item.target_spec_id or "")
