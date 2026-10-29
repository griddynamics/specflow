"""Resolve: raw nodes to canonical nodes (docs/graph/build.md step 3; FR-BLD-07, FR-BLD-17, FR-BLD-18).

1. Every raw node gets an identity key (data-model.md "Identity"): an explicit id that leads its name
   (``<ID>: name``, ``<ID> - name``, ``**<ID>**``, after list or heading markup), when the id family
   names a type of the node's identity scope (kind ``id``, label from the family); else, for types
   keyed by id with a ``span`` fallback, a provisional key from the normalised name that Conform
   replaces by the defining line; else the normalised name (``surface``). An id anywhere else in the
   name is a reference to another node (Conform's ``id_references``), never the node's own id. A raw
   node without an id whose normalised name is empty gets no key and is dropped (``empty_names``).
   A hub name (roles anchor and group) is normalised further (``hub_surface``): ``-`` and ``_`` become
   spaces, a leading article goes, and each word's plural is folded, so "The Orders service",
   "Order Service" and "order-service" share ``surface|order service``. Hub names are compared in
   this form everywhere (the cross-label check, Conform's hub grounding, mentions and known terms).
2. Raw nodes with equal keys merge (all types of one identity scope share keys).
3. One name under several hub labels: hub clusters of different hub labels that hold one normalised
   member name ("Request counter" as Module, Feature and Capability) become one node
   (``cross_label_hub_merges``). Its label is the one most raw nodes of the merged clusters carry;
   ties go to role anchor over group, then to ontology order. Only the explicit-id guard applies
   (``guard_rejections``).
4. Hub consolidation (``consolidate_hubs.1``, ``graph.build.consolidate``): every hub cluster left
   is one item, shown with its label, its name and up to ``EVIDENCE_LINES`` source lines of its raw
   nodes' fragments that name it (each at most ``LINE_MAX_CHARS``). All items, whatever their label,
   are sorted by decision key (the lowest identity key of the cluster) and then ordered by one greedy
   nearest-neighbour chain over their ``name_embedding`` vectors, so near-duplicates sit next to each
   other even under different labels; the sequence is cut into blocks of at most ``BLOCK_SIZE`` items
   overlapping by ``BLOCK_OVERLAP``, one judge call each. A proposed group merges, across hub labels
   too, member by member in decision-key order; a member stays out when a veto refuses it: two
   different explicit ids, a cluster of more than ``MAX_CLUSTER`` groups, or a stored ``different``
   decision with a member already accepted. The merged node's label is the proposed one when it is a
   hub label, else the majority of its raw labels. A failed call merges nothing. Decisions are
   remembered per project (``DecisionMemory``, kind ``decisions``) and override the model in later
   builds. Requirements are atomic, so they never take part in steps 3 and 4.
5. Each canonical node gets its label (the explicit id's family, else the label step 3 or 4 chose,
   else the most frequent extracted label, ties to the first seen), its name (the clearest surface
   of its groups of that label, when there are any, else of all its groups: the most frequent, then
   the longest, then the first in raw order), its identity key (that of a group of its label that
   holds the name; the other groups' keys become ``identity_aliases``) and a ``name_embedding`` of
   ``"<Label>: <name>"``: the artifact keeps the text (``name_embedding_text``); the vector goes to
   the vector store (``graph.build.vectors``).
6. Persisted labels (``update`` builds, ``persisted``: the label and identity key of every persisted
   spec node by its identity key and identity aliases). A canonical node that continues a persisted
   node keeps that node's label, so Conform legalises its edges against the label it carries and
   Diff updates it in place: its identity key or an alias equals a persisted key or alias, or, for
   a hub, the same key under another hub label's scope does (``Module|surface|x`` for
   ``Feature|surface|x``). The first match in the order own key, its hub rescopes, then each alias
   and its rescopes wins. When the persisted label differs, is an extracted label and keeps the
   node's kind (a hub stays a hub; any other label stays in its identity scope), the node takes it
   and its key scope: a hub takes the persisted identity key when it is among the candidates, else
   its own key under the persisted label's scope; its former key becomes an alias. The extracted
   label goes to ``label_disagreement`` (``labels_kept``). A node whose label an explicit id fixes,
   or whose new key another node holds, stays as it is.

Canonical nodes are ordered by identity key; ``cid`` is their zero-padded position.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np
from pydantic import BaseModel, ConfigDict

from graph.build.consolidate import (
    BLOCK_OVERLAP,
    BLOCK_SIZE,
    HUBS_PASS,
    HUBS_PROMPT,
    LINE_MAX_CHARS,
    DecisionMemory,
    PassSpec,
    Proposal,
    RawGroup,
    blocks,
    consolidate,
    nn_chain,
    pair,
    short_id,
)
from graph.build.models import (
    CanonicalNode,
    ConsolidationCounters,
    NodesArtifact,
    PlanArtifact,
    RawNode,
    ResolveArtifact,
    ResolveCounters,
)
from graph.build.vectors import VectorCache
from graph.core.identity import (
    bounded_key_value,
    identity_key,
    normalise_id,
    normalise_surface,
)
from graph.core.llm import Gateway
from graph.core.llm.gateway import prompt_name
from graph.core.ontology import Ontology
from logger.logger import INFO, get_logger, log_event

logger = get_logger("graph.build.resolve")

EVIDENCE_LINES: Final = 2
"""Source lines per hub shown to ``consolidate_hubs.1`` (each at most ``LINE_MAX_CHARS``)."""
MAX_CLUSTER: Final = 12
"""Largest number of groups one cluster may hold: exact-merged groups of a hub-consolidation cluster
(Resolve 4), requirement nodes of a dedup cluster (Conform 2)."""

_WORD: Final = re.compile(r"\w+")
_LEAD_MARKUP: Final = re.compile(r"^[\s\-*+#>|`_~\[(]*(?:\d+[.)]\s+)?[\s*_`\[(]*")
_AFTER_ID: Final = re.compile(r"^[\s:;,.*_`)\]|\-\u2013\u2014]*")


# Shared helpers (also used by Conform)


def tokens(text: str) -> list[str]:
    """Word tokens of ``text``: NFKC, case-folded, punctuation dropped."""

    return _WORD.findall(unicodedata.normalize("NFKC", text).casefold())


_ARTICLES: Final = frozenset({"the", "a", "an"})
_HUB_SEPARATORS: Final = re.compile(r"[-_]+")
_VOWELS: Final = frozenset("aeiouy")
MIN_PLURAL_LETTERS: Final = 3
"""Words up to this many letters are never folded to a singular."""
MIN_IES_LETTERS: Final = 4
"""Words of more letters than this fold ``-ies`` to ``-y``."""
MIN_ACRONYM_PLURAL: Final = 2
"""An acronym plural (``APIs``) has more letters than this."""
KEY_FIELDS: Final = 3
"""Fields of an identity key ``<project>|<kind>|<value>`` (the value may itself hold ``|``)."""
MIN_ITEMS: Final = 2
"""Items a consolidation (or labels a cross-label merge) needs to have anything to join."""


def fold_plural(word: str) -> str:
    """A case-folded word's simple singular: ``-ies`` to ``-y``, ``-sses`` to ``-ss``, ``-uses``,
    ``-xes``, ``-shes`` and ``-ches`` (after a consonant) drop ``es``, any other ``-s`` drops ``s``
    unless the word ends in ``-ss``, ``-us`` or ``-is``. Words of at most three letters, or with a
    non-letter, stay as they are."""

    if len(word) <= MIN_PLURAL_LETTERS or not word.isalpha() or not word.endswith("s"):
        return word
    return _singular(word)


def _singular(word: str) -> str:
    """``fold_plural`` of a word known to end in ``s``, to be folded."""

    if word.endswith("ies") and len(word) > MIN_IES_LETTERS:
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith(("uses", "xes", "shes")):
        return word[:-2]
    if word.endswith("ches") and word[-5] not in _VOWELS:
        return word[:-2]
    if word.endswith(("ss", "us", "is")):
        return word
    return word[:-1]


def hub_surface(name: str) -> str:
    """The identity value of a hub name: ``-``/``_`` as spaces, no leading article, plurals folded
    (``_hub_word``), then ``normalise_surface``."""

    words = _HUB_SEPARATORS.sub(" ", unicodedata.normalize("NFKC", name)).split()
    if len(words) > 1 and words[0].casefold() in _ARTICLES:
        words = words[1:]
    return normalise_surface(" ".join(_hub_word(word) for word in words))


def _hub_word(word: str) -> str:
    """An acronym keeps its form ("CORS"); its lower-case plural drops the ``s`` ("APIs"); any other
    word is case-folded and ``fold_plural``-ed."""

    if word.isupper():
        return word.casefold()
    if word.endswith("s") and len(word) > MIN_ACRONYM_PLURAL and word[:-1].isupper():
        return word[:-1].casefold()
    return fold_plural(word.casefold())


def hub_tokens(text: str) -> list[str]:
    """Word tokens of ``text`` with plurals folded (``tokens`` then ``fold_plural``): the form in which
    hub names are matched against each other and against source lines."""

    return [fold_plural(token) for token in tokens(text)]


def hub_words(text: str) -> list[str]:
    """Word tokens of ``text`` in ``hub_surface``'s word form: NFKC, ``-``/``_`` as spaces, each word
    ``_hub_word``-ed (acronym plurals and plurals folded, case-folded). A name's index form is
    ``hub_words(hub_surface(name))`` (folding twice changes nothing), so a hub name and the text
    that mentions it meet in one form."""

    spaced = _HUB_SEPARATORS.sub(" ", unicodedata.normalize("NFKC", text))
    return [_hub_word(word) for word in _WORD.findall(spaced)]


def hub_value_words(value: str) -> list[str]:
    """Word tokens of a hub identity value (already ``hub_surface``-d, e.g. a ``surface`` alias) in
    ``hub_words``' form, without folding again (a folded acronym such as ``cors`` stays as it is)."""

    return _WORD.findall(_HUB_SEPARATORS.sub(" ", value))


def surface_values(keys: Iterable[str]) -> list[str]:
    """The values of the ``surface``-kind identity keys among ``keys``, in order, each once."""

    values: list[str] = []
    for key in keys:
        parts = key.split("|", 2)
        if len(parts) == KEY_FIELDS and parts[1] == "surface" and parts[2] and parts[2] not in values:
            values.append(parts[2])
    return values


def embedding_text(label: str, name: str) -> str:
    """Text of a ``name_embedding`` (data-model.md "Spec node")."""

    return f"{label}: {name}"


class IdFinder:
    """Finds explicit ids of the ontology's id families in text (``FR-CHK-002``, ``FR_CHK_006``, ``NFR 12``).

    An id is a family exactly as the ontology writes it (upper case), a separator (``-``, ``_``, or a
    space directly before the digits), optional upper-case letter segments each ending in ``-`` or
    ``_``, then digits (``.``, ``-`` or ``_`` may join more digit groups). It must stand alone: no
    word character or ``-`` right before it, and no word character (or ``-`` and one) right after it,
    so ``DATA-2024-q1`` or ``SFR-1`` hold no id.
    """

    def __init__(self, ontology: Ontology) -> None:
        families = sorted(
            {family for node_type in ontology.node_types.values() for family in node_type.id_families},
            key=lambda family: (-len(family), family),
        )
        self._pattern: re.Pattern[str] | None = None
        if families:
            alternatives = "|".join(re.escape(family) for family in families)
            self._pattern = re.compile(
                rf"(?<![\w-])(?:{alternatives})(?:[-_](?:[A-Z]{{1,10}}[-_])*| )\d+(?:[-_.]\d+)*(?!\w|-\w)"
            )

    def all(self, text: str) -> list[str]:
        """Every explicit id in ``text``, normalised, in order of first occurrence."""

        if self._pattern is None or not text:
            return []
        return list(dict.fromkeys(normalise_id(match.group(0)) for match in self._pattern.finditer(text)))

    def own(self, text: str) -> str | None:
        """The id that leads ``text`` (list, heading, table and emphasis markup ignored), normalised.

        An id anywhere else in the text is a reference to another node, never the text's own id.
        """

        return self.split(text)[0]

    def split(self, text: str) -> tuple[str | None, str]:
        """``(own id, text without it)``: the id as ``own`` finds it and the rest of ``text``, trimmed
        of the separators after the id ("FR-CHK-001: Users can ..." gives "Users can ...")."""

        if self._pattern is None or not text:
            return None, text
        start = _LEAD_MARKUP.match(text)
        head = self._pattern.match(text, start.end() if start else 0)
        if head is None:
            return None, text
        return normalise_id(head.group(0)), _AFTER_ID.sub("", text[head.end() :]).strip()


def explicit_label(ontology: Ontology, explicit_id: str, scope: str) -> str | None:
    """The label the id's family names, when that label keys by id within ``scope``; else None."""

    label = ontology.label_for_id_family(explicit_id)
    if label is None:
        return None
    rule = ontology.identity_rule(label)
    return label if rule.key == "id" and rule.scope == scope else None


def clearest_name(surfaces: Sequence[str]) -> str:
    """Most frequent surface, then the longest, then the first."""

    counts = Counter(surfaces)
    first = {surface: index for index, surface in reversed(list(enumerate(surfaces)))}
    return min(counts, key=lambda surface: (-counts[surface], -len(surface), first[surface]))


def display_name(surfaces: Sequence[str], explicit_ids: Iterable[str]) -> str:
    """The clearest non-empty surface; the explicit id when every member name was the id alone."""

    named = [surface for surface in surfaces if surface]
    if named:
        return clearest_name(named)
    return min(explicit_ids, default="")


def majority_label(labels: Sequence[str]) -> str:
    """Most frequent label, ties to the first seen."""

    counts = Counter(labels)
    first = {label: index for index, label in reversed(list(enumerate(labels)))}
    return min(counts, key=lambda label: (-counts[label], first[label]))


# Resolve


@dataclass
class _Group:
    """Raw nodes under one identity key (step 2)."""

    key: str
    scope: str
    provisional: bool
    explicit_ids: set[str] = field(default_factory=set)
    id_label: str | None = None
    raws: list[RawNode] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return display_name(self.surfaces, self.explicit_ids)

    @property
    def label(self) -> str:
        return self.id_label or majority_label(self.labels)


class _Clusters:
    """Union-find over group indexes with the cluster-level vetoes."""

    def __init__(self, groups: Sequence[_Group]) -> None:
        self._parent = list(range(len(groups)))
        self._size = [1] * len(groups)
        self._ids = [set(group.explicit_ids) for group in groups]

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def union(self, first: int, second: int, *, size: bool = True) -> bool | None:
        """Merge; None when already one cluster, False when a veto refuses: two different explicit
        ids, or (``size``) more than ``MAX_CLUSTER`` groups. ``size=False``: equal names (step 3)."""

        a, b = self.find(first), self.find(second)
        if a == b:
            return None
        if self._ids[a] and self._ids[b] and self._ids[a] != self._ids[b]:
            return False
        if size and self._size[a] + self._size[b] > MAX_CLUSTER:
            return False
        root, child = (a, b) if a < b else (b, a)
        self._parent[child] = root
        self._size[root] += self._size[child]
        self._ids[root] |= self._ids[child]
        return True


@dataclass(frozen=True, slots=True)
class PersistedIdentity:
    """The identity key and label of a persisted spec node (Resolve step 6), and its identity aliases
    (hub mentions also match their ``surface`` values: ``persisted_aliases``)."""

    identity_key: str
    label: str
    aliases: tuple[str, ...] = ()


def persisted_identities(nodes: Iterable[tuple[str, Sequence[str], str]]) -> dict[str, PersistedIdentity]:
    """``(identity key, identity aliases, label)`` of persisted spec nodes as the ``persisted`` mapping
    of ``resolve``: every key, then every alias not already a key, each to the first node holding it."""

    rows = [(key, tuple(aliases), label) for key, aliases, label in nodes if key and label]
    mapping: dict[str, PersistedIdentity] = {}
    for key, aliases, label in rows:
        mapping.setdefault(key, PersistedIdentity(identity_key=key, label=label, aliases=aliases))
    for key, aliases, label in rows:
        for alias in aliases:
            mapping.setdefault(alias, PersistedIdentity(identity_key=key, label=label, aliases=aliases))
    return mapping


def persisted_aliases(keys: Iterable[str], persisted: Mapping[str, PersistedIdentity]) -> tuple[str, ...]:
    """The identity key and aliases of the persisted node that the first of ``keys`` (a node's own key,
    then its aliases) names; empty when none does."""

    for key in keys:
        match = persisted.get(key)
        if match is not None:
            return (match.identity_key, *match.aliases)
    return ()


async def resolve(
    plan: PlanArtifact,
    nodes: NodesArtifact,
    *,
    gateway: Gateway,
    ontology: Ontology,
    vectors: VectorCache,
    memory: DecisionMemory | None = None,
    persisted: Mapping[str, PersistedIdentity] | None = None,
) -> ResolveArtifact:
    """Canonical nodes of one build from its raw nodes (build.md step 3); name vectors come from and
    go to ``vectors``; ``memory`` holds the hub consolidation decisions (None: none are kept);
    ``persisted`` (``update`` builds) the persisted labels by identity key and alias (step 6)."""

    fragment_rank = {fragment.fragment_id: index for index, fragment in enumerate(plan.fragments)}
    groups, empty_names = _exact_groups(nodes.raw_nodes, ontology, IdFinder(ontology))
    exact_merges = len(nodes.raw_nodes) - empty_names - len(groups)

    fragment_lines = {fragment.fragment_id: fragment.text.split("\n") for fragment in plan.fragments}
    clusters = _Clusters(groups)
    chosen, cross_merges, cross_rejections = _cross_label_merge(groups, clusters, ontology)
    consolidation = await _consolidate_hubs(
        groups,
        clusters,
        chosen,
        gateway=gateway,
        ontology=ontology,
        vectors=vectors,
        memory=memory,
        evidence=lambda group: evidence_lines(group, fragment_lines, fragment_rank),
    )

    members: dict[int, list[int]] = {}
    for index in range(len(groups)):
        members.setdefault(clusters.find(index), []).append(index)
    drafts = [_draft(groups, indexes, fragment_rank, chosen.get(root)) for root, indexes in members.items()]
    labels_kept = _keep_persisted_labels(drafts, persisted or {}, ontology)
    drafts.sort(key=lambda draft: str(draft["identity_key"]))
    texts = [embedding_text(str(draft["label"]), str(draft["name"])) for draft in drafts]
    # Stores the vector of every canonical name (a merged node may take a name not embedded yet).
    await vectors.mapping(texts)
    width = max(5, len(str(len(drafts))))
    canonical = tuple(
        CanonicalNode.model_validate({"cid": f"c{index:0{width}d}", "name_embedding_text": text, **draft})
        for index, (draft, text) in enumerate(zip(drafts, texts, strict=True))
    )
    result = ResolveArtifact(
        canonical=canonical,
        counters=ResolveCounters(
            exact_merges=exact_merges,
            empty_names=empty_names,
            cross_label_hub_merges=cross_merges,
            guard_rejections=cross_rejections,
            labels_kept=labels_kept,
            consolidation=consolidation,
        ),
    )
    log_event(
        logger,
        INFO,
        "graph.resolve.done",
        raw_nodes=len(nodes.raw_nodes),
        canonical_nodes=len(canonical),
        provisional=sum(1 for node in canonical if node.provisional),
        **result.counters.model_dump(exclude={"consolidation"}),
        hub_merges=consolidation.merges,
        hub_cross_label_merges=consolidation.cross_label_merges,
        hub_vetoes=consolidation.vetoes,
    )
    return result


def _exact_groups(
    raw_nodes: Iterable[RawNode],
    ontology: Ontology,
    ids: IdFinder,
) -> tuple[list[_Group], int]:
    """Groups by identity key and the number of raw nodes dropped for an empty normalised name."""

    groups: dict[str, _Group] = {}
    empty = 0
    hubs = ontology.hub_labels
    for raw in raw_nodes:
        rule = ontology.identity_rule(raw.label)
        surface = " ".join(raw.name.split())
        explicit, rest = ids.split(surface) if rule.key == "id" else (None, surface)
        id_label = explicit_label(ontology, explicit, rule.scope) if explicit else None
        if id_label is not None and explicit is not None:
            surface = rest
            key = identity_key(rule.scope, "id", explicit)
            provisional = False
        else:
            value = (
                hub_surface(raw.name) if raw.label in hubs and rule.key == "surface" else normalise_surface(raw.name)
            )
            if not value:
                empty += 1
                continue
            provisional = rule.key == "id" and rule.fallback == "span"
            key = (
                identity_key(rule.scope, "span", bounded_key_value(value))
                if provisional
                else identity_key(rule.scope, "surface", value)
            )
        group = groups.get(key)
        if group is None:
            group = groups[key] = _Group(key=key, scope=rule.scope, provisional=provisional)
        if id_label is not None and explicit is not None:
            group.explicit_ids.add(explicit)
            group.id_label = id_label
        group.raws.append(raw)
        group.surfaces.append(surface)
        group.labels.append(raw.label)
    return list(groups.values()), empty


def unit_rows(vectors: Sequence[Sequence[float] | np.ndarray]) -> np.ndarray:
    """``vectors`` as a new matrix of float32 rows of length one (a zero row stays zero)."""

    matrix = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    matrix /= norms[:, None]
    return matrix


def evidence_lines(
    group: _Group,
    fragment_lines: Mapping[str, Sequence[str]],
    fragment_rank: Mapping[str, int],
) -> list[str]:
    """Up to ``EVIDENCE_LINES`` distinct source lines of the group's fragments (in plan order) that
    hold one of its names as whole words (``hub_tokens``); each defused (``prompt_name``) and at most
    ``LINE_MAX_CHARS`` characters."""

    needles = [needle for needle in dict.fromkeys(tuple(hub_tokens(surface)) for surface in group.surfaces) if needle]
    fragment_ids = sorted(
        {fragment_id for raw in group.raws for fragment_id in raw.fragment_ids},
        key=lambda fragment_id: (
            fragment_rank.get(fragment_id, len(fragment_rank)),
            fragment_id,
        ),
    )
    found: list[str] = []
    for fragment_id in fragment_ids:
        for line in fragment_lines.get(fragment_id, ()):
            words = hub_tokens(line)
            if any(_holds(needle, words) for needle in needles):
                text = prompt_name(line, LINE_MAX_CHARS)
                if text and text not in found:
                    found.append(text)
                    if len(found) == EVIDENCE_LINES:
                        return found
    return found


def _holds(needle: Sequence[str], words: Sequence[str]) -> bool:
    size = len(needle)
    return any(tuple(words[index : index + size]) == tuple(needle) for index in range(len(words) - size + 1))


def _joined_labels(labels: Mapping[int, Counter[str]], roots: Iterable[int], ontology: Ontology) -> dict[int, str]:
    """The label of each joined cluster: the one most raw nodes carry (ties: role anchor, then ontology order)."""

    order = {label: index for index, label in enumerate(ontology.declared_labels)}
    roles = {node_type.label: node_type.role for node_type in ontology.node_types.values()}
    chosen: dict[int, str] = {}
    for root in sorted(roots):
        counts = labels[root]
        chosen[root] = min(
            counts,
            key=lambda label: (
                -counts[label],
                roles.get(label) != "anchor",
                order.get(label, len(order)),
                label,
            ),
        )
    return chosen


def _cross_label_merge(
    groups: Sequence[_Group], clusters: _Clusters, ontology: Ontology
) -> tuple[dict[int, str], int, int]:
    """Step 3: join hub clusters of different labels that share a normalised member name.

    Returns ``(label by final cluster root, merges, guard rejections)``; only joined clusters get a
    label, the one most raw nodes carry (ties: role anchor, then group, then ontology order).
    """

    hubs = ontology.hub_labels
    by_name: dict[str, list[int]] = {}
    labels: dict[int, Counter[str]] = {}
    for index, group in enumerate(groups):
        if group.label in hubs and not group.provisional:
            labels.setdefault(clusters.find(index), Counter()).update(group.labels)
            name = hub_surface(group.name)
            if name:
                by_name.setdefault(name, []).append(index)

    merges = 0
    rejections = 0
    joined: set[int] = set()
    for name in sorted(by_name):
        roots = sorted({clusters.find(index) for index in by_name[name]})
        if len({majority_label(list(labels[root].elements())) for root in roots}) < MIN_ITEMS:
            continue
        for other in roots[1:]:
            first = clusters.find(roots[0])
            outcome = clusters.union(first, other, size=False)
            if outcome is True:
                merges += 1
                root = clusters.find(first)
                labels[root] = labels.pop(first) + labels.pop(other)
                joined.add(root)
            elif outcome is False:
                rejections += 1

    chosen = _joined_labels(labels, {clusters.find(index) for index in joined}, ontology)
    if merges:
        log_event(
            logger,
            INFO,
            "graph.resolve.cross_label_hubs",
            merges=merges,
            labels=sorted(set(chosen.values())),
        )
    return chosen, merges, rejections


# Hub consolidation (step 4)

_ROLE_SUBJECT: Final = {"anchor": "part of the system", "group": "grouping"}


class HubGroup(BaseModel):
    """One group of a ``consolidate_hubs.1`` answer: block ids of one hub and its label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ids: list[str]
    label: str


class HubGroups(BaseModel):
    """Answer of ``consolidate_hubs.1``: groups of hubs that are one node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: list[HubGroup]


@dataclass(frozen=True)
class _HubItem:
    """A hub cluster at the start of the pass: its root group, decision key, label, name, evidence."""

    root: int
    key: str
    label: str
    name: str
    evidence: list[str]


def _cluster_label(groups: Sequence[_Group], indexes: Sequence[int], chosen_label: str | None) -> str:
    """The label ``_draft`` gives a cluster: explicit id family, else the chosen label, else majority."""

    members = [groups[index] for index in indexes]
    id_labels = [group.id_label for group in members if group.id_label]
    if id_labels:
        return id_labels[0]
    return chosen_label or majority_label([label for group in members for label in group.labels])


def _hub_system(ontology: Ontology) -> str:
    hub_types = [node_type for node_type in ontology.node_types.values() if node_type.label in ontology.hub_labels]
    definitions = "\n\n".join(
        f"{node_type.label} (a {_ROLE_SUBJECT.get(node_type.role or '', 'thing')}):\n{node_type.description.strip()}"
        for node_type in hub_types
    )
    labels = ", ".join(node_type.label for node_type in hub_types)
    return (
        f"Hub nodes were extracted from {ontology.framing}, document by document, so one part of "
        "the system or one grouping may occur several times, under different names or labels.\n"
        f"Hub types:\n{definitions}\n\n"
        "Each line of the block is one node as JSON: its id, label, name and up to two source "
        "lines that name it. Find the nodes that are the same part of the system or the same "
        "grouping: a short name can be an alias of a longer one (a table row or service name for "
        "the full name), and one thing can be extracted under different labels. Answer groups: "
        "each group lists the ids of the nodes that are one thing and the label the merged node "
        f"should have (one of: {labels}). Never group different things, such as a part and one of "
        "its sub-parts or two features with similar names. Leave out nodes without a duplicate. "
        "Use only ids of the block."
    )


def hub_block_messages(
    ontology: Ontology, gateway: Gateway, items: Sequence[tuple[str, str, Sequence[str]]]
) -> list[dict[str, str]]:
    """Messages of one ``consolidate_hubs.1`` call over a block of ``(label, name, evidence lines)``
    items, shown with the short ids of their positions."""

    lines = [
        json.dumps(
            {"id": short_id(position), "label": label, "name": prompt_name(name), "evidence": list(evidence)},
            ensure_ascii=False,
        )
        for position, (label, name, evidence) in enumerate(items)
    ]
    return [
        {"role": "system", "content": _hub_system(ontology)},
        {"role": "user", "content": gateway.untrusted("hubs", "\n".join(lines))},
    ]


def _hub_items(
    groups: Sequence[_Group],
    clusters: _Clusters,
    chosen: Mapping[int, str],
    hubs: Collection[str],
    evidence: Callable[[_Group], list[str]],
) -> tuple[dict[int, list[int]], list[_HubItem]]:
    """The hub clusters (group indexes by cluster root) and one consolidation item per cluster."""

    members: dict[int, list[int]] = {}
    for index, group in enumerate(groups):
        if group.label in hubs and not group.provisional:
            members.setdefault(clusters.find(index), []).append(index)
    items: list[_HubItem] = []
    for root in sorted(members):
        indexes = members[root]
        surfaces = [surface for index in indexes for surface in groups[index].surfaces]
        explicit = sorted({value for index in indexes for value in groups[index].explicit_ids})
        key = min(groups[index].key for index in indexes)
        merged = _Group(
            key=key,
            scope="",
            provisional=False,
            raws=[raw for index in indexes for raw in groups[index].raws],
            surfaces=surfaces,
        )
        items.append(
            _HubItem(
                root=root,
                key=key,
                label=_cluster_label(groups, indexes, chosen.get(root)),
                name=display_name(surfaces, explicit),
                evidence=evidence(merged),
            )
        )
    return members, items


async def _consolidate_hubs(
    groups: Sequence[_Group],
    clusters: _Clusters,
    chosen: dict[int, str],
    *,
    gateway: Gateway,
    ontology: Ontology,
    vectors: VectorCache,
    memory: DecisionMemory | None,
    evidence: Callable[[_Group], list[str]],
) -> ConsolidationCounters:
    """Step 4 (module docstring): every hub cluster left after steps 2 and 3 is one item, keyed for
    decisions by the lowest identity key of its groups. Items of all hub labels are sorted by that
    key and ordered by one nearest-neighbour chain over their name vectors, then judged block by
    block. A proposed group merges under the explicit-id and cluster-size vetoes (labels may differ);
    a member refused by a veto stays out. The merged node's label goes to ``chosen`` (by cluster
    root); the members' identity keys become identity aliases (``_draft``)."""

    hubs = ontology.hub_labels
    members, items = _hub_items(groups, clusters, chosen, hubs, evidence)
    tally = ConsolidationCounters(items=len(items))
    if len(items) < MIN_ITEMS:
        return tally
    texts = [embedding_text(item.label, item.name) for item in items]
    found = await vectors.mapping(texts)
    unit = unit_rows([found[text] for text in texts])
    by_key = sorted(range(len(items)), key=lambda position: items[position].key)
    order = [by_key[row] for row in nn_chain(unit[by_key])]

    applied = {"vetoes": 0, "merges": 0, "cross_label_merges": 0}

    def apply(proposal: Proposal, cannot: frozenset[tuple[int, int]]) -> list[tuple[int, ...]]:
        ordered = sorted(dict.fromkeys(proposal.members), key=lambda item: items[item].key)
        anchor = ordered[0]
        accepted = [anchor]
        for member in ordered[1:]:
            if any(pair(member, other) in cannot for other in accepted):
                applied["vetoes"] += 1
                continue
            first = clusters.find(items[anchor].root)
            second = clusters.find(items[member].root)
            differ = first != second and _cluster_label(groups, members[first], chosen.get(first)) != _cluster_label(
                groups, members[second], chosen.get(second)
            )
            outcome = clusters.union(first, second)
            if outcome is False:
                applied["vetoes"] += 1
                continue
            accepted.append(member)
            if outcome:
                applied["merges"] += 1
                applied["cross_label_merges"] += int(differ)
                root = clusters.find(first)
                members[root] = members[root] + members.pop(second if root == first else first)
        if len(accepted) < MIN_ITEMS:
            return []
        root = clusters.find(items[anchor].root)
        chosen[root] = (
            proposal.label
            if proposal.label in hubs
            else majority_label([label for index in members[root] for label in groups[index].labels])
        )
        return [tuple(accepted)]

    async def ask(block: Sequence[int]) -> list[RawGroup]:
        messages = hub_block_messages(
            ontology, gateway, [(items[item].label, items[item].name, items[item].evidence) for item in block]
        )
        answer = await gateway.structured("judge", None, messages, HubGroups, prompt=HUBS_PROMPT)
        return [(group.ids, group.label) for group in answer.groups]

    result = await consolidate(
        spec=PassSpec(name=HUBS_PASS, prompt=HUBS_PROMPT),
        keys=[item.key for item in items],
        unit=unit,
        block_list=blocks(order, BLOCK_SIZE, BLOCK_OVERLAP),
        ask=ask,
        apply=apply,
        memory=memory,
        tally=tally,
    )
    return result.model_copy(update=applied)


def _strings(value: object) -> tuple[str, ...]:
    """The items of a draft field (aliases, explicit ids) as strings."""

    return tuple(str(item) for item in value) if isinstance(value, Iterable) else ()


def _persisted_candidates(key: str, aliases: Sequence[str], *, hub: bool, hub_scopes: Sequence[str]) -> list[str]:
    """The keys a draft may continue a persisted node under: its own and aliases (a hub in every hub scope)."""

    candidates: list[str] = []
    for own in (key, *aliases):
        candidates.append(own)
        scope, _, rest = own.partition("|")
        if hub and scope in hub_scopes:
            candidates.extend(f"{other}|{rest}" for other in hub_scopes if other != scope)
    return candidates


def _kept_key(
    match: PersistedIdentity, key: str, label: str, candidates: Sequence[str], ontology: Ontology
) -> str | None:
    """The identity key a draft of ``label`` takes when it continues ``match`` of another label; None when
    the two labels do not share a key scope (a hub continues a hub only)."""

    if label in ontology.hub_labels:
        if match.label not in ontology.hub_labels:
            return None
        _, _, rest = key.partition("|")
        return (
            match.identity_key
            if match.identity_key in candidates
            else f"{ontology.identity_rule(match.label).scope}|{rest}"
        )
    if ontology.identity_rule(match.label).scope == ontology.identity_rule(label).scope:
        return key
    return None


def _keep_persisted_labels(
    drafts: list[dict[str, object]], persisted: Mapping[str, PersistedIdentity], ontology: Ontology
) -> int:
    """Step 6 (module docstring): give each draft that continues a persisted node of another label
    that label and its key scope, in place; returns the number of drafts changed."""

    if not persisted:
        return 0
    hubs = ontology.hub_labels
    extracted = set(ontology.extracted_labels)
    hub_scopes = sorted({ontology.identity_rule(label).scope for label in hubs})
    taken = {str(draft["identity_key"]) for draft in drafts}
    kept = 0
    for draft in drafts:
        label, key = str(draft["label"]), str(draft["identity_key"])
        aliases = _strings(draft["identity_aliases"])
        if any(ontology.label_for_id_family(value) for value in _strings(draft["explicit_ids"])):
            continue
        candidates = _persisted_candidates(key, aliases, hub=label in hubs, hub_scopes=hub_scopes)
        match = next((persisted[candidate] for candidate in candidates if candidate in persisted), None)
        if match is None or match.label == label or match.label not in extracted:
            continue
        new_key = _kept_key(match, key, label, candidates, ontology)
        if new_key is None or (new_key != key and new_key in taken):
            continue
        taken.discard(key)
        taken.add(new_key)
        draft["label"] = match.label
        draft["identity_key"] = new_key
        draft["identity_aliases"] = tuple(sorted({key, *aliases} - {new_key}))
        draft["label_disagreement"] = label
        kept += 1
    if kept:
        log_event(logger, INFO, "graph.resolve.labels_kept", nodes=kept)
    return kept


def _draft(
    groups: Sequence[_Group],
    indexes: Sequence[int],
    fragment_rank: Mapping[str, int],
    chosen_label: str | None = None,
) -> dict[str, object]:
    """Fields of one canonical node (all but ``cid`` and ``name_embedding_text``) from its groups
    (step 5); ``chosen_label``: the label step 3 or 4 chose for the cluster.

    A hub consolidation group may join different names under different labels, so the name comes
    from the groups of the node's label when there are any, and the lead (whose key the node keeps)
    is one of them, so the key's scope matches the label."""

    members = [groups[index] for index in indexes]
    raws = [raw for group in members for raw in group.raws]
    explicit = sorted({value for group in members for value in group.explicit_ids})
    id_labels = [group.id_label for group in members if group.id_label]
    label = (
        id_labels[0]
        if id_labels
        else chosen_label or majority_label([label for group in members for label in group.labels])
    )
    own = [group for group in members if group.label == label]
    if own:
        members = own + [group for group in members if group.label != label]
    name = display_name([surface for group in own or members for surface in group.surfaces], explicit)
    lead = next(
        (group for group in members if name in group.surfaces and group.label == label),
        next((group for group in members if name in group.surfaces), members[0]),
    )
    fragments = sorted(
        {fragment_id for raw in raws for fragment_id in raw.fragment_ids},
        key=lambda fragment_id: (
            fragment_rank.get(fragment_id, len(fragment_rank)),
            fragment_id,
        ),
    )
    return {
        "identity_key": lead.key,
        "identity_aliases": tuple(sorted(group.key for group in members if group is not lead)),
        "provisional": lead.provisional,
        "name": name,
        "label": label,
        "member_ids": tuple(raw.raw_id for raw in raws),
        "explicit_ids": tuple(explicit),
        "fragment_ids": tuple(fragments),
    }
