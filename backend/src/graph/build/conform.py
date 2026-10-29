"""Conform: the target graph from canonical nodes, raw edges and attributes (docs/graph/build.md step 5).

1. Grounding (tech-specs 5.4 "Quote snapping", "Edge facts"). A node's quote (attribute ``quote`` of
   the attributes pass) is searched, token by token, in the unmasked lines of every fragment the node
   is attributed to; each fragment whose best contiguous line span holds at least ``SNAP_OVERLAP`` of
   the quote's tokens becomes evidence with that span. When no attributed fragment matches, the other
   fragments of the node's units are searched and the single best span is taken. A node whose quote
   matches nowhere (or that has none) is grounded in its attributed fragments with their full line
   ranges and keeps no ``quote``. A hub node (roles anchor and group) is grounded in every fragment it
   is attributed to: where its quote snaps, those lines; else the first line of the fragment that holds
   its name or a ``surface`` alias as whole words; else the fragment's full line range. An edge's fact is snapped the
   same way (its attributed fragments,
   then its unit); the persisted ``fact`` is the snapped source text (at most ``FACT_MAX_CHARS``) and
   its fragment is the evidence; an unmatched fact leaves the attributed fragments (or, without
   attribution, the unit's fragments) as evidence and no ``fact``. Best span: highest token overlap,
   then fewer lines, then the earlier fragment and line.
   A provisional requirement gets its final key from its defining line (the best span's lines): kind
   ``id`` when an explicit id of its scope leads that line (after list, heading, table or emphasis
   markup; an id elsewhere in the line is a reference), else kind ``span`` with the normalised
   sentence(s) of those lines that hold the quote (else the normalised lines), at most
   ``KEY_VALUE_MAX_CHARS`` characters (``bounded_key_value``); without a matched quote its
   provisional key (the normalised name) stays. In ``update`` builds a node, not a hub, whose final
   key or an alias names a persisted node of another label of its identity scope keeps that label
   (Resolve step 6 matches only the keys Resolve gives); this is checked again after the merges
   below and dedup, whose split keys and aliases can name a persisted node too
   (``_keep_persisted_labels``).
   Nodes with equal final keys merge (the non-provisional or first node by ``cid`` survives); on a
   ``span`` key only nodes that are alike merge: same label, and name token Jaccard at least
   ``ALIKE_TOKEN_SIMILARITY`` or ``name_embedding`` cosine at least ``ALIKE_VECTOR_SIMILARITY``
   (each node joins the first cluster, in ``cid`` order, whose first node it is alike). When a span
   key holds several clusters, each keeps its own key ``<span key>|<normalised name>`` (name at most
   ``NAME_KEY_MAX_CHARS``, ``bounded_key_value``; a clash appends ``|<label>``, then ``|2``, ...),
   so distinct commitments of one sentence stay separate and no label is lost (``span_splits``).
   Hub names are matched in Resolve's folded form (``hub_tokens``: plurals folded) on both sides.
2. Requirement dedup: among id-less requirements (kind ``span``, no explicit id) of one identity
   scope and label, across documents, each node's ``NEAREST_NAMES`` nearest names with
   ``name_embedding`` cosine at least ``DEDUP_CANDIDATE_COSINE`` (0.85) are candidates. Guards refuse
   a pair when:
   - numbers: both sides state a number of the same unit kind and the values differ (neither side's
     values of that kind contain the other's). Numbers are read from the name and from the quote
     (the ``quote`` attribute, else the snapped lines) separately, line by line, so a unit never
     comes from across the name/quote boundary or a line break. A unit is a currency sign before the
     number or a word or symbol of ``_UNITS`` right after it (time, size, percent, rate, currency
     codes and counted things such as ``requests`` or ``users``); one kind compares in one base unit
     (``5 s`` equals ``5000 ms``). Other numbers are unitless and compare only name with name and
     quote with quote. Thousands commas are dropped; digits after ``<letter>-`` belong to an id. A
     number on one side only never refuses;
   - negation: one name negates its main predicate and the other does not (a token ``not``,
     ``never``, ``no``, ``without``, ``cannot``, ``nor``, ``none`` or an ``n't`` contraction, after
     ``_NOT_NEGATION`` bounds and alternatives such as "whether or not", "not only", "no later
     than" and "no more than" are removed);
   - modality: strong ``must``/``shall`` versus weak ``should``/``may`` in name plus quote (a
     statement with neither is compatible with both, but a cluster never holds both classes);
   - ids: the explicit ids they reference differ.
   A pair with cosine at least ``DEDUP_MERGE_COSINE`` and name token Jaccard at least
   ``DEDUP_MERGE_TOKENS`` merges; any other candidate goes to the small model
   (``dedup_adjudicate.2``: the same commitment, where one statement may be shorter or more
   detailed than the other, and the two do not conflict; names and quotes in an untrusted block; a
   failed call means no merge). A verdict is remembered per project (``DedupMemory``,
   ``ExtractionStore`` kind ``decisions``, key ``<project>|dedup|<key a>|<key b>`` with the identity
   keys sorted, fingerprint ``<prompt version>|<small model>``); a stored verdict replaces the call
   (``dedup_verdicts_reused``); a failed call stores nothing. Confirmed pairs are applied in a fixed order (direct
   first, then by
   cosine, Jaccard and key order), clusters at most ``MAX_CLUSTER``. The node with the lowest
   identity key survives; the other keys become its aliases, evidence (hence statements, one per
   fragment) is joined, its quote stays, missing attributes come from the others.
3. Implied edges: each ``derive`` rule's reference attribute resolved by normalised name (a hub also
   by its folded name, ``hub_surface``) or explicit id to a node of the rule's target labels; and,
   for ``id_references`` relations, every explicit id (not the node's own) in a node's quote or
   ``stmt`` that names another node on a legal pair, only between spine sources (requirements), so
   a hub's quote never links it to a requirement by id.
4. Legalisation with ``Ontology.legalise`` against the snapped evidence lines (no snapped line: no
   cue); drops are counted per relation and reason (also ``self_loop``, ``locality``). Symmetric edges
   are put in canonical order (lower identity key first); equal edge keys merge (fragments joined,
   ``stated`` beats ``implied``, the first stated fact wins).
5. Locality: ``strict`` keeps an edge only when one unit grounds both endpoints; ``grounded`` keeps an
   edge whose endpoints are both grounded target nodes (every target node is, by step 1).
6. Statements (one per evidence fragment; at most ``MAX_STATEMENTS``, the first and last of each
   document always kept; a hub's only from the evidence fragments that name it, as ``MENTIONED_IN``
   counts names: its span there, else the first line naming it, so a hub named nowhere has none),
   ``MENTIONED_IN`` counts of hub nodes (roles anchor and group): whole-word
   occurrences of the name's words in fragments the node is not grounded in, both in Resolve's hub
   form (``hub_words``, ``hub_surface``), longest name first (``terms.NameIndex``; other nodes get
   none, their mentions are noise), ``exclusive`` conflicts
   (extra targets per source, relation and target label), normalised attributes, search
   ``embedding``. A node all of whose canonical nodes' attributes batches failed is marked
   ``attributes_missing``; a node that kept its persisted label (Resolve step 6, or step 1 here)
   carries the extracted one as ``label_disagreement``. Target nodes keep the texts of their ``embedding`` and
   ``name_embedding``; every
   vector goes through the build's vector store (``graph.build.vectors``), never into the artifact.
7. Invariants D1-D4 (data-model.md); a violation raises ``ConformanceError``.

Nodes are ordered by identity key and edges by edge key; evidence and fragment lists follow document
and fragment order.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

import numpy as np
from pydantic import BaseModel, ConfigDict

from graph.build.consolidate import DECISION_KIND
from graph.build.models import (
    AttributeValue,
    Basis,
    CanonicalNode,
    EdgesArtifact,
    EvidenceLines,
    FragmentRec,
    PlanArtifact,
    ResolveArtifact,
    StatementRec,
    TargetCounters,
    TargetEdge,
    TargetGraph,
    TargetNode,
)
from graph.build.options import StepServices
from graph.build.ports import ExtractionStore
from graph.build.resolve import (
    KEY_FIELDS,
    MAX_CLUSTER,
    IdFinder,
    PersistedIdentity,
    embedding_text,
    explicit_label,
    fold_plural,
    hub_surface,
    hub_tokens,
    hub_words,
    persisted_aliases,
    tokens,
    unit_rows,
)
from graph.build.terms import NameIndex, hub_name_index, named_in
from graph.build.vectors import Vector, VectorCache, get_many, put_many
from graph.core.errors import ConformanceError, LLMError, LLMFatalError
from graph.core.identity import (
    bounded_key_value,
    identity_key,
    normalise_id,
    normalise_surface,
)
from graph.core.llm import Gateway
from graph.core.llm.gateway import prompt_name
from graph.core.ontology import Dropped, Ontology
from logger.logger import DEBUG, INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.conform")

MIN_DEDUP_GROUP: Final = 2
"""Nodes a dedup group needs to hold a pair."""

QUOTE_ATTRIBUTE: Final = "quote"
STMT_ATTRIBUTE: Final = "stmt"
"""Attribute names the data model gives a meaning (data-model.md "Spec node", "Provenance")."""

SNAP_OVERLAP: Final = 0.85
MAX_SPAN_LINES: Final = 12
FACT_MAX_CHARS: Final = 300
STATEMENT_MAX_CHARS: Final = 300
MAX_STATEMENTS: Final = 50
BASIS_RANK: Final[dict[Basis, int]] = {"stated": 0, "implied": 1, "inferred": 2}
GROUNDING_STATUSES: Final = frozenset({"active", "skipped"})
ALIKE_TOKEN_SIMILARITY: Final = 0.6
ALIKE_VECTOR_SIMILARITY: Final = 0.9
"""Nodes on one span key merge only when alike: same label and name token Jaccard at least
``ALIKE_TOKEN_SIMILARITY`` or ``name_embedding`` cosine at least ``ALIKE_VECTOR_SIMILARITY``."""
NAME_KEY_MAX_CHARS: Final = 200
"""Longest name part of a name-qualified span key (``bounded_key_value``)."""
DEDUP_PROMPT: Final = "dedup_adjudicate.2"
DEDUP_CANDIDATE_COSINE: Final = 0.85
DEDUP_MERGE_COSINE: Final = 0.95
DEDUP_MERGE_TOKENS: Final = 0.7
"""Requirement dedup: candidates at ``name_embedding`` cosine >= 0.85 (each node's ``NEAREST_NAMES``
nearest); merge at cosine >= 0.95 and token Jaccard >= 0.7, otherwise the small model decides."""
NEAREST_NAMES: Final = 10
"""Nearest names by ``name_embedding`` cosine that each node pairs with as dedup candidates."""
COSINE_CHUNK: Final = 256
"""Rows per block of the chunked cosine products (memory: ``COSINE_CHUNK`` x names floats)."""
_COSINE_SLACK: Final = 1e-3
"""Float32 nearest-neighbour scores within this of the threshold are kept; the exact cosine decides."""
_STRONG_MODALS: Final = frozenset(["must", "shall"])
_WEAK_MODALS: Final = frozenset(["should", "may"])
_NEGATIONS: Final = frozenset(re.findall(r"\w+", "not never no without cannot nor none"))
_NEGATED_CONTRACTION: Final = re.compile(r"n['\u2019]t\b", re.IGNORECASE)
_NOT_NEGATION: Final = re.compile(
    r"\b(?:(?:whether\s+)?or\s+not|not\s+only|not\s+just|no\s+matter"
    r"|(?:no|not)\s+(?:more|less|fewer|later|earlier|sooner|longer|shorter|greater|higher|lower"
    r"|older|newer|bigger|smaller|larger)\s+than|not\s+(?:to\s+)?exceed(?:ing)?)\b",
    re.IGNORECASE,
)
"""Phrases that hold a negation word but bound or enumerate instead of negating the predicate."""
_NUMBER: Final = re.compile(
    r"(?<![\w.:])(?<![^\W\d_]-)([$\u20ac\u00a3\u00a5])?[ \t]?(?<![\w.:])(\d+(?:[.,:]\d+)*)"
    r"(?:[ \t]?(%|[^\W\d_]+(?:/[^\W\d_]+)?))?"
)
"""A number: an optional currency sign before it, digits (``.``, ``,`` or ``:`` may join groups),
then an optional ``%`` or word (``req/s`` style allowed) right after, on the same line; digits glued
to a word or to ``<letter>-`` (an id such as ``FR-CHK-001``) are not numbers. The word is a unit only
when ``_UNITS`` knows it."""
_CURRENCY_SIGNS: Final = {"$": "usd", "\u20ac": "eur", "\u00a3": "gbp", "\u00a5": "jpy"}


def _unit_table() -> dict[str, tuple[str, float]]:
    """Unit word (case-folded) to ``(kind, factor to the kind's base unit)``."""

    table: dict[str, tuple[str, float]] = {}
    groups: list[tuple[str, float, str]] = [
        ("time", 0.001, "ms msec msecs millisecond milliseconds"),
        ("time", 1.0, "s sec secs second seconds"),
        ("time", 60.0, "min mins minute minutes"),
        ("time", 3600.0, "h hr hrs hour hours"),
        ("time", 86400.0, "d day days"),
        ("time", 604800.0, "wk week weeks"),
        ("time", 2592000.0, "month months"),
        ("time", 31536000.0, "y yr yrs year years"),
        ("percent", 1.0, "% percent"),
        ("size", 1.0, "byte bytes"),
        ("size", 1e3, "kb"),
        ("size", 1e6, "mb"),
        ("size", 1e9, "gb"),
        ("size", 1e12, "tb"),
        ("size", 1024.0, "kib"),
        ("size", 1024.0**2, "mib"),
        ("size", 1024.0**3, "gib"),
        ("rate", 1.0, "req/s rps qps requests/s"),
        ("rate", 1 / 60, "req/min rpm requests/min"),
    ]
    for kind, factor, words in groups:
        for word in words.split():
            table[word] = (kind, factor)
    for code in ("usd", "eur", "gbp", "jpy", "pln", "chf"):
        table[code] = (f"currency:{code}", 1.0)
    counted = (
        "item user request row record retry retries attempt time character char job file "
        "order line entry entries call connection session digit decimal page event message "
        "document account"
    )
    for word in counted.split():
        singular = fold_plural(word)
        table[word] = (f"count:{singular}", 1.0)
        if not word.endswith("s"):
            table[f"{word}s"] = (f"count:{singular}", 1.0)
    return table


_UNITS: Final = _unit_table()
_NUMBER_SEPARATOR: Final = re.compile(r"(?<=\d),(?=\d{3}\b)")
_LIST_MARKER: Final = re.compile(r"^(?:[-*+>]|\d+[.)])\s+")
_SENTENCE_END: Final = re.compile(r"(?<=[.!?;])\s+")


# Corpus view


@dataclass(frozen=True)
class _Fragment:
    rec: FragmentRec
    rank: int
    lines: tuple[str, ...]
    line_tokens: tuple[tuple[str, ...], ...]

    def source_line(self, index: int) -> int:
        return self.rec.line_start + index

    def text(self, first: int, last: int) -> str:
        """Verbatim text of fragment line indexes ``first..last``: blank lines dropped, a list marker cut."""

        text = "\n".join(line.strip() for line in self.lines[first : last + 1] if line.strip())
        return _LIST_MARKER.sub("", text, count=1)


@dataclass(frozen=True)
class _Span:
    """A snapped span: fragment line indexes ``first..last`` with the quote token overlap."""

    fragment: _Fragment
    first: int
    last: int
    overlap: float

    @property
    def rank(self) -> tuple[float, int, int, int]:
        return (-self.overlap, self.last - self.first, self.fragment.rank, self.first)

    @property
    def evidence(self) -> EvidenceLines:
        return EvidenceLines(
            fragment_id=self.fragment.rec.fragment_id,
            line_start=self.fragment.source_line(self.first),
            line_end=self.fragment.source_line(self.last),
        )

    @property
    def text(self) -> str:
        return self.fragment.text(self.first, self.last)

    @property
    def lines(self) -> list[str]:
        return [line for line in self.fragment.lines[self.first : self.last + 1] if line.strip()]


class _Corpus:
    def __init__(self, plan: PlanArtifact) -> None:
        self.fragments: dict[str, _Fragment] = {}
        for rank, rec in enumerate(plan.fragments):
            lines = tuple(rec.text.split("\n"))
            self.fragments[rec.fragment_id] = _Fragment(rec, rank, lines, tuple(tuple(tokens(line)) for line in lines))
        self.documents = {document.document_id: document for document in plan.documents}
        self.document_rank = {document.document_id: index for index, document in enumerate(plan.documents)}
        self.units: dict[str, tuple[str, ...]] = {unit.unit_id: unit.fragment_ids for unit in plan.units}
        self.units_of: dict[str, list[str]] = {}
        for unit in plan.units:
            for fragment_id in unit.fragment_ids:
                self.units_of.setdefault(fragment_id, []).append(unit.unit_id)

    def groundable(self, fragment_id: str) -> _Fragment | None:
        fragment = self.fragments.get(fragment_id)
        return fragment if fragment is not None and fragment.rec.status in GROUNDING_STATUSES else None

    def ordered(self, fragment_ids: Iterable[str]) -> list[str]:
        known = [fragment_id for fragment_id in set(fragment_ids) if fragment_id in self.fragments]
        return sorted(known, key=lambda fragment_id: self.fragments[fragment_id].rank)

    def unit_fragments(self, unit_ids: Iterable[str]) -> list[str]:
        return self.ordered(fragment_id for unit_id in unit_ids for fragment_id in self.units.get(unit_id, ()))


def _best_span(quote: Counter[str], fragment: _Fragment) -> _Span | None:
    """Best contiguous span of ``fragment`` for the quote tokens, or None (no token in common)."""

    total = sum(quote.values())
    best: _Span | None = None
    count = len(fragment.line_tokens)
    for first in range(count):
        if not any(token in quote for token in fragment.line_tokens[first]):
            continue
        seen: Counter[str] = Counter()
        hits = 0
        for last in range(first, min(count, first + MAX_SPAN_LINES)):
            seen.update(fragment.line_tokens[last])
            new_hits = sum((seen & quote).values())
            if new_hits > hits or last == first:
                hits = new_hits
                span = _Span(fragment, first, last, hits / total)
                if best is None or span.rank < best.rank:
                    best = span
            if hits == total:
                break
    return best


def _snap_each(text: str | None, fragments: Sequence[_Fragment]) -> list[_Span]:
    """Accepted spans of ``text``, at most one per fragment, in fragment order."""

    quote = Counter(tokens(text or ""))
    if not quote:
        return []
    spans = [_best_span(quote, fragment) for fragment in fragments]
    return [span for span in spans if span is not None and span.overlap >= SNAP_OVERLAP]


def _snap_best(text: str | None, fragments: Sequence[_Fragment]) -> _Span | None:
    spans = _snap_each(text, fragments)
    return min(spans, key=lambda span: span.rank) if spans else None


# Nodes


@dataclass
class _Node:
    key: str
    label: str
    name: str
    provisional: bool
    cids: list[str]
    canonical: CanonicalNode
    attributes: dict[str, AttributeValue]
    aliases: set[str] = field(default_factory=set)
    explicit_ids: set[str] = field(default_factory=set)
    spans: list[_Span] = field(default_factory=list)
    primary: _Span | None = None
    fallback: list[_Fragment] = field(default_factory=list)
    label_disagreement: str | None = None

    @property
    def evidence(self) -> list[EvidenceLines]:
        items = [span.evidence for span in self.spans]
        items += [
            EvidenceLines(
                fragment_id=f.rec.fragment_id,
                line_start=f.rec.line_start,
                line_end=f.rec.line_end,
            )
            for f in self.fallback
        ]
        return items

    @property
    def fragment_ids(self) -> set[str]:
        return {span.fragment.rec.fragment_id for span in self.spans} | {f.rec.fragment_id for f in self.fallback}


def _ground_node(
    node: CanonicalNode,
    attributes: Mapping[str, AttributeValue],
    corpus: _Corpus,
    *,
    hub: bool,
) -> _Node:
    attributed = [f for f in (corpus.groundable(fid) for fid in node.fragment_ids) if f is not None]
    if not attributed:
        raise ConformanceError("conform_node_without_fragments", label=node.label)
    raw_quote = attributes.get(QUOTE_ATTRIBUTE)
    quote = raw_quote if isinstance(raw_quote, str) else None
    spans = _snap_each(quote, attributed)
    if not spans:
        attributed_ids = {f.rec.fragment_id for f in attributed}
        units = {unit for f in attributed for unit in corpus.units_of.get(f.rec.fragment_id, [])}
        others = [
            fragment
            for fid in corpus.unit_fragments(units)
            if fid not in attributed_ids and (fragment := corpus.groundable(fid)) is not None
        ]
        best = _snap_best(quote, others)
        spans = [best] if best is not None else []
    result = _Node(
        key=node.identity_key,
        label=node.label,
        name=node.name,
        provisional=node.provisional,
        cids=[node.cid],
        canonical=node,
        attributes=dict(attributes),
        aliases=set(node.identity_aliases),
        explicit_ids=set(node.explicit_ids),
        label_disagreement=node.label_disagreement,
    )
    if spans:
        result.spans = spans
        result.primary = min(spans, key=lambda span: span.rank)
    if not hub:
        if not spans:
            result.fallback = attributed
        return result
    snapped = {span.fragment.rec.fragment_id for span in spans}
    needles = _name_needles(node)
    for fragment in attributed:
        if fragment.rec.fragment_id in snapped:
            continue
        line = _name_line(needles, fragment)
        if line is None:
            result.fallback.append(fragment)
        else:
            result.spans.append(_Span(fragment, line, line, 0.0))
    result.spans.sort(key=lambda span: (span.fragment.rank, span.first, span.last))
    return result


def _name_needles(node: CanonicalNode) -> list[tuple[str, ...]]:
    """Folded token sequences (``hub_tokens``) of a hub's names: its name, its key's surface and the
    surfaces of its ``surface`` aliases."""

    surfaces = [node.name]
    for key in (node.identity_key, *node.identity_aliases):
        parts = key.split("|", 2)
        if len(parts) == KEY_FIELDS and parts[1] == "surface":
            surfaces.append(parts[2])
    needles = [tuple(hub_tokens(surface)) for surface in surfaces]
    return [needle for needle in dict.fromkeys(needles) if needle]


def _name_line(needles: Sequence[tuple[str, ...]], fragment: _Fragment) -> int | None:
    """Index of the first line of ``fragment`` holding one of ``needles`` as whole folded words, or
    None."""

    for index, line_tokens in enumerate(fragment.line_tokens):
        folded = [fold_plural(token) for token in line_tokens]
        if any(_count(needle, folded) for needle in needles):
            return index
    return None


def _final_key(node: _Node, ontology: Ontology, ids: IdFinder) -> None:
    """Replace a provisional key by the defining line's id or span key (build.md Conform 1)."""

    if not node.provisional:
        return
    node.provisional = False
    if node.primary is None:
        return
    scope = ontology.identity_rule(node.label).scope
    lines = node.primary.lines
    own = ids.own(lines[0]) if lines else None
    label = explicit_label(ontology, own, scope) if own else None
    if own is not None and label is not None:
        node.key = identity_key(scope, "id", own)
        node.label = label
        node.explicit_ids.add(own)
        return
    defining = _quote_in_lines(node.attributes.get(QUOTE_ATTRIBUTE), lines)
    if defining:
        node.key = identity_key(scope, "span", bounded_key_value(defining))


def _keep_persisted_labels(
    nodes: Iterable[_Node], persisted: Mapping[str, PersistedIdentity], ontology: Ontology
) -> int:
    """Resolve step 6 for the keys Conform gives (an id-less requirement's ``span`` key, a split key,
    a dedup alias): a node, not a hub, whose key or an alias names a persisted node of another extracted
    label of its identity scope takes that label, in place, and keeps the extracted one as
    ``label_disagreement``; its key stays. A node whose label an explicit id fixes stays as it is.
    Returns the number of nodes changed."""

    if not persisted:
        return 0
    hubs = ontology.hub_labels
    extracted = set(ontology.extracted_labels)
    kept = 0
    for node in nodes:
        if node.label in hubs or any(ontology.label_for_id_family(value) for value in node.explicit_ids):
            continue
        match = next((persisted[key] for key in (node.key, *sorted(node.aliases)) if key in persisted), None)
        if (
            match is None
            or match.label == node.label
            or match.label not in extracted
            or match.label in hubs
            or ontology.identity_rule(match.label).scope != ontology.identity_rule(node.label).scope
        ):
            continue
        node.label_disagreement = node.label_disagreement or node.label
        node.label = match.label
        kept += 1
    return kept


def _quote_in_lines(quote: AttributeValue, lines: Sequence[str]) -> str:
    """The span key value: the normalised sentence(s) of the snapped lines that hold the quote.

    Sentences end at ``.``, ``!``, ``?`` or ``;`` followed by whitespace, and at line ends; a list
    marker is not part of a sentence. So two wordings of one sentence share a key and two sentences
    on one line do not. When the quote is not inside the lines, the whole lines are the value.
    """

    whole = normalise_surface(" ".join(line.strip() for line in lines))
    located = normalise_surface(quote) if isinstance(quote, str) else ""
    if not located:
        return whole
    sentences = [
        sentence
        for line in lines
        for sentence in _SENTENCE_END.split(_LIST_MARKER.sub("", line.strip()))
        if sentence.strip()
    ]
    folded = [_fold(sentence) for sentence in sentences]
    joined = " ".join(folded)
    at = joined.find(located)
    if at < 0:
        return whole
    chosen: list[str] = []
    offset = 0
    for sentence, text in zip(sentences, folded, strict=True):
        if offset < at + len(located) and at < offset + len(text):
            chosen.append(sentence)
        offset += len(text) + 1
    return normalise_surface(" ".join(chosen)) or whole


def _fold(text: str) -> str:
    """NFKC, case-folded, single spaces; inner and end punctuation kept (for locating a quote)."""

    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _kind(key: str) -> str:
    parts = key.split("|", 2)
    return parts[1] if len(parts) == KEY_FIELDS else ""


def _merge_equal_keys(nodes: Sequence[_Node], vectors: Mapping[str, np.ndarray]) -> tuple[list[_Node], int, int]:
    """Merge nodes with equal final keys; ``(nodes, merges, split nodes)``.

    Nodes on one ``span`` key merge only when alike (``_alike`` to the first node of a cluster, in
    node order); when a span key holds more than one cluster, every cluster's survivor gets the key
    ``<span key>|<normalised name>`` (``_unique_key``) and counts as split.
    """

    by_key: dict[str, list[_Node]] = {}
    for node in nodes:
        by_key.setdefault(node.key, []).append(node)
    taken = set(by_key)
    merged: list[_Node] = []
    merges = 0
    splits = 0
    for key, group in by_key.items():
        clusters = _alike_clusters(group, vectors) if len(group) > 1 and _kind(key) == "span" else [group]
        for cluster in clusters:
            survivor = next((node for node in cluster if not node.canonical.provisional), cluster[0])
            for other in cluster:
                if other is not survivor:
                    _absorb(survivor, other, keep_primary=False)
            merges += len(cluster) - 1
            if len(clusters) > 1:
                splits += 1
                survivor.key = _unique_key(key, survivor, taken)
            merged.append(survivor)
    return merged, merges, splits


def _alike_clusters(group: Sequence[_Node], vectors: Mapping[str, np.ndarray]) -> list[list[_Node]]:
    clusters: list[list[_Node]] = []
    for node in group:
        home = next((cluster for cluster in clusters if _alike(cluster[0], node, vectors)), None)
        if home is None:
            clusters.append([node])
        else:
            home.append(node)
    return clusters


def _alike(first: _Node, second: _Node, vectors: Mapping[str, np.ndarray]) -> bool:
    """Same label, and names close: token Jaccard >= ``ALIKE_TOKEN_SIMILARITY`` or
    ``name_embedding`` cosine >= ``ALIKE_VECTOR_SIMILARITY``."""

    if first.label != second.label:
        return False
    if token_jaccard(frozenset(tokens(first.name)), frozenset(tokens(second.name))) >= ALIKE_TOKEN_SIMILARITY:
        return True
    a = vectors.get(embedding_text(first.label, first.name))
    b = vectors.get(embedding_text(second.label, second.name))
    return a is not None and b is not None and _cosine(a, b) >= ALIKE_VECTOR_SIMILARITY


def _cosine(first: Vector, second: Vector) -> float:
    a = np.asarray(first, dtype=np.float64)
    b = np.asarray(second, dtype=np.float64)
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / norm if norm else 0.0


def _unique_key(key: str, node: _Node, taken: set[str]) -> str:
    """``<key>|<normalised name>`` (bounded); on a clash ``|<label>``, then ``|2``, ``|3``, ..."""

    base = f"{key}|{bounded_key_value(normalise_surface(node.name), NAME_KEY_MAX_CHARS)}"
    candidate = base
    if candidate in taken:
        base = candidate = f"{base}|{node.label}"
    number = 1
    while candidate in taken:
        number += 1
        candidate = f"{base}|{number}"
    taken.add(candidate)
    return candidate


def _absorb(survivor: _Node, other: _Node, *, keep_primary: bool) -> None:
    """Merge ``other`` into ``survivor``: cids, aliases, ids, missing attributes, evidence."""

    survivor.cids.extend(other.cids)
    survivor.aliases |= {alias for alias in other.aliases if alias != survivor.key}
    survivor.explicit_ids |= other.explicit_ids
    for name, value in other.attributes.items():
        survivor.attributes.setdefault(name, value)
    _join_evidence(survivor, other, keep_primary=keep_primary)


def _join_evidence(survivor: _Node, other: _Node, *, keep_primary: bool) -> None:
    spans = {(s.fragment.rec.fragment_id, s.first, s.last): s for s in [*survivor.spans, *other.spans]}
    survivor.spans = sorted(spans.values(), key=lambda s: (s.fragment.rank, s.first, s.last))
    candidates = [span for span in (survivor.primary, other.primary) if span is not None]
    if not (keep_primary and survivor.primary is not None):
        survivor.primary = min(candidates, key=lambda span: span.rank) if candidates else None
    span_fragments = {span.fragment.rec.fragment_id for span in survivor.spans}
    fallback = {f.rec.fragment_id: f for f in [*survivor.fallback, *other.fallback]}
    survivor.fallback = sorted(
        (f for fid, f in fallback.items() if fid not in span_fragments),
        key=lambda f: f.rank,
    )


# Requirement dedup


def token_jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    """Jaccard of two token sets; 0 when either is empty."""

    return len(first & second) / len(first | second) if first and second else 0.0


def nearest_pairs(unit: np.ndarray, k: int, threshold: float) -> set[tuple[int, int]]:
    """Position pairs ``(i, j)``, ``i < j``, of each row with its ``k`` nearest other rows whose
    float32 cosine is at least ``threshold - _COSINE_SLACK`` (the exact cosine decides later;
    ``unit``: float32 unit rows; products in blocks of ``COSINE_CHUNK`` rows, never n x n at once)."""

    floor = threshold - _COSINE_SLACK
    count = unit.shape[0]
    k = min(k, count - 1)
    close: set[tuple[int, int]] = set()
    if k <= 0:
        return close
    for start in range(0, count, COSINE_CHUNK):
        block = unit[start : start + COSINE_CHUNK] @ unit.T
        rows = np.arange(block.shape[0])
        block[rows, start + rows] = -np.inf
        hits = block >= floor
        many = hits.sum(axis=1) > k
        found: list[tuple[np.ndarray, np.ndarray]] = []
        few_rows, few_cols = np.nonzero(hits & ~many[:, None])
        found.append((few_rows, few_cols))
        crowded = np.nonzero(many)[0]
        if crowded.size:
            top = np.argpartition(-block[crowded], k - 1, axis=1)[:, :k]
            found.append((np.repeat(crowded, k), top.ravel()))
        for found_rows, found_cols in found:
            for row, column in zip((found_rows + start).tolist(), found_cols.tolist(), strict=True):
                close.add((row, column) if row < column else (column, row))
    return close


def pair_cosines(unit: np.ndarray, pairs: Sequence[tuple[int, int]], chunk: int = 1024) -> list[float]:
    """Cosine of each pair, summed in float64 (one row product per pair, never a full matrix)."""

    result: list[float] = []
    if not pairs:
        return result
    for start in range(0, len(pairs), chunk):
        part = np.asarray(pairs[start : start + chunk], dtype=np.int64)
        first = unit[part[:, 0]].astype(np.float64)
        second = unit[part[:, 1]].astype(np.float64)
        result.extend(np.einsum("ij,ij->i", first, second).tolist())
    return result


async def gather_verdicts(
    calls: Sequence[Coroutine[object, object, tuple[bool, str | None]]], *, event: str
) -> list[bool]:
    """Await ``(verdict, error code)`` calls concurrently, in order; a failed call is ``False``.

    When one call raises (``LLMFatalError``), the others are cancelled and the error propagates.
    Failures are logged as ``event`` with counts and error codes only.
    """

    if not calls:
        return []
    tasks = [asyncio.ensure_future(call) for call in calls]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    failures = [code for _, code in results if code is not None]
    if failures:
        log_event(
            logger,
            WARNING,
            event,
            failed=len(failures),
            adjudications=len(calls),
            error_codes=sorted(set(failures)),
        )
    return [same for same, _ in results]


class SameCommitment(BaseModel):
    """Small-model verdict of ``dedup_adjudicate.2``: do two statements make one commitment?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    same: bool


_Values = Mapping[str, frozenset[str | float]]
"""Number values by kind: a unit kind (``time``, ``count:request``, ...) or a unitless role."""


@dataclass(frozen=True)
class _Signature:
    """What a restatement must keep (the dedup guards), read from a node's name and quote."""

    numbers: _Values
    negated: bool
    modal: frozenset[str]
    references: frozenset[str]


_NO_SIGNATURE: Final = _Signature(numbers={}, negated=False, modal=frozenset(), references=frozenset())


@dataclass(frozen=True)
class _DedupPair:
    first: int
    second: int
    cosine: float
    jaccard: float

    @property
    def direct(self) -> bool:
        return self.cosine >= DEDUP_MERGE_COSINE and self.jaccard >= DEDUP_MERGE_TOKENS


@dataclass
class _DedupCounts:
    candidates: int = 0
    merges: int = 0
    adjudications: int = 0
    guard_refusals: int = 0
    reused: int = 0


_DEDUP_NAME: Final = "dedup"
DEDUP_PASS: Final = _DEDUP_NAME


class DedupMemory:
    """Stored requirement dedup verdicts of one project (module docstring, step 2)."""

    def __init__(self, store: ExtractionStore, project_id: str, model: str) -> None:
        self._store = store
        self._project_id = project_id
        self.fingerprint = f"{DEDUP_PROMPT}|{model}"

    def key(self, first: str, second: str) -> str:
        low, high = sorted((first, second))
        return f"{self._project_id}|{DEDUP_PASS}|{low}|{high}"

    async def verdicts(self, pairs: Sequence[tuple[str, str]]) -> dict[str, bool]:
        """Stored verdicts by store key (``key``) of the pairs that have one."""

        found = await get_many(self._store, DECISION_KIND, sorted({self.key(a, b) for a, b in pairs}), self.fingerprint)
        return {key: payload["same"] for key, payload in found.items() if isinstance(payload.get("same"), bool)}

    async def write(self, verdicts: Mapping[str, bool]) -> None:
        await put_many(
            self._store,
            DECISION_KIND,
            self.fingerprint,
            {key: {"same": same} for key, same in sorted(verdicts.items())},
        )


def _dedup_quote(node: _Node) -> str:
    """The node's quote attribute, else its snapped quote lines, else empty."""

    quote = node.attributes.get(QUOTE_ATTRIBUTE)
    if isinstance(quote, str) and quote.strip():
        return quote
    return node.primary.text if node.primary else ""


def _dedup_text(node: _Node) -> str:
    return f"{node.name}\n{_dedup_quote(node)}"


def _signature(node: _Node, ids: IdFinder) -> _Signature:
    text = _dedup_text(node)
    words = set(tokens(text))
    modal = frozenset(name for name, cue in (("strong", _STRONG_MODALS), ("weak", _WEAK_MODALS)) if words & cue)
    numbers: dict[str, set[str | float]] = {}
    for role, part in (("name", node.name), ("quote", _dedup_quote(node))):
        for kind, value in _numbers(part, role):
            numbers.setdefault(kind, set()).add(value)
    return _Signature(
        numbers={kind: frozenset(values) for kind, values in numbers.items()},
        negated=_negated(node.name),
        modal=modal,
        references=frozenset(ids.all(text)),
    )


def _numbers(text: str, role: str) -> list[tuple[str, str | float]]:
    """``(kind, value)`` of every number in ``text``, read line by line: a unit number's kind and
    its value in the kind's base unit; a unitless number's kind ``plain:<role>`` and its digits."""

    found: list[tuple[str, str | float]] = []
    for line in unicodedata.normalize("NFKC", text).splitlines():
        for match in _NUMBER.finditer(line):
            digits = _NUMBER_SEPARATOR.sub("", match.group(2))
            sign, word = match.group(1), (match.group(3) or "").casefold()
            unit = (f"currency:{_CURRENCY_SIGNS[sign]}", 1.0) if sign else _UNITS.get(word)
            value = _float(digits)
            if unit is None or value is None:
                found.append((f"plain:{role}", digits))
            else:
                kind, factor = unit
                found.append((kind, round(value * factor, 9)))
    return found


def _float(digits: str) -> float | None:
    try:
        return float(digits)
    except ValueError:
        return None


def _negated(name: str) -> bool:
    """True when the name negates its predicate (bounds such as "no more than" and alternatives
    such as "whether or not" removed first)."""

    text = _NOT_NEGATION.sub(" ", unicodedata.normalize("NFKC", name))
    return bool(set(tokens(text)) & _NEGATIONS) or bool(_NEGATED_CONTRACTION.search(text))


def _numbers_conflict(first: _Values, second: _Values) -> bool:
    """True when both state values of one kind and neither side's values contain the other's."""

    return any(
        not (first[kind] <= second[kind] or second[kind] <= first[kind]) for kind in first.keys() & second.keys()
    )


def _refusal(first: _Signature, second: _Signature) -> str | None:
    """The guard that refuses the merge (numbers, negation, modal strength, referenced ids), or None."""

    if _numbers_conflict(first.numbers, second.numbers):
        return "numbers"
    if first.negated != second.negated:
        return "negation"
    if len(first.modal | second.modal) > 1:
        return "modality"
    if (
        first.references
        and second.references
        and not (first.references <= second.references or second.references <= first.references)
    ):
        return "ids"
    return None


def _dedup_eligible(node: _Node, ontology: Ontology) -> bool:
    rule = ontology.identity_rule(node.label)
    return rule.key == "id" and rule.fallback == "span" and not node.explicit_ids and _kind(node.key) == "span"


def _dedup_candidates(
    ordered: Sequence[_Node],
    groups: Mapping[tuple[str, str], list[int]],
    vectors: Mapping[str, np.ndarray],
    ids: IdFinder,
    counts: _DedupCounts,
) -> tuple[dict[int, _Signature], list[_DedupPair], list[_DedupPair]]:
    """The candidate pairs of the dedup groups: ``(signatures, direct pairs, pairs to ask the model)``."""

    signatures: dict[int, _Signature] = {}
    direct: list[_DedupPair] = []
    asked: list[_DedupPair] = []
    for group_key in sorted(groups):
        indexes = groups[group_key]
        if len(indexes) < MIN_DEDUP_GROUP:
            continue
        unit = unit_rows([vectors[embedding_text(ordered[i].label, ordered[i].name)] for i in indexes])
        close = sorted(nearest_pairs(unit, NEAREST_NAMES, DEDUP_CANDIDATE_COSINE))
        for (a, b), cosine in zip(close, pair_cosines(unit, close), strict=True):
            if cosine < DEDUP_CANDIDATE_COSINE:
                continue
            first, second = indexes[a], indexes[b]
            counts.candidates += 1
            for index in (first, second):
                if index not in signatures:
                    signatures[index] = _signature(ordered[index], ids)
            reason = _refusal(signatures[first], signatures[second])
            if reason is not None:
                counts.guard_refusals += 1
                log_event(
                    logger,
                    DEBUG,
                    "graph.conform.dedup_refused",
                    reason=reason,
                    cosine=round(cosine, 3),
                )
                continue
            pair = _DedupPair(
                first,
                second,
                cosine,
                token_jaccard(
                    frozenset(tokens(ordered[first].name)),
                    frozenset(tokens(ordered[second].name)),
                ),
            )
            (direct if pair.direct else asked).append(pair)
    return signatures, direct, asked


async def _dedup_verdicts(
    asked: Sequence[_DedupPair],
    ordered: Sequence[_Node],
    *,
    gateway: Gateway,
    ontology: Ontology,
    memory: DedupMemory | None,
    counts: _DedupCounts,
) -> dict[int, bool]:
    """The verdict on each pair in ``asked`` (by index): stored ones reused, the others asked of the model."""

    keys = [(ordered[p.first].key, ordered[p.second].key) for p in asked]
    stored = await memory.verdicts(keys) if memory is not None and keys else {}
    known = {
        index: stored[memory.key(*keys[index])]
        for index in range(len(asked))
        if memory is not None and memory.key(*keys[index]) in stored
    }
    pending = [index for index in range(len(asked)) if index not in known]
    failed: set[int] = set()

    async def ask(index: int) -> tuple[bool, str | None]:
        pair = asked[index]
        same, code = await _adjudicate_dedup(gateway, ontology, ordered[pair.first], ordered[pair.second])
        if code is not None:
            failed.add(index)
        return same, code

    counts.adjudications = len(pending)
    counts.reused = len(known)
    answers = await gather_verdicts([ask(index) for index in pending], event="graph.conform.dedup_adjudication_failed")
    verdicts = dict(known)
    verdicts.update(zip(pending, answers, strict=True))
    if memory is not None:
        await memory.write({memory.key(*keys[index]): verdicts[index] for index in pending if index not in failed})
    return verdicts


def _dedup_clusters(
    ordered: Sequence[_Node],
    confirmed: Sequence[_DedupPair],
    signatures: Mapping[int, _Signature],
    counts: _DedupCounts,
) -> list[_Node]:
    """Merge the confirmed pairs (clusters at most ``MAX_CLUSTER``, one modality) into their survivors."""

    parent = list(range(len(ordered)))
    members: dict[int, list[int]] = {index: [index] for index in range(len(ordered))}
    modal = {index: signatures.get(index, _NO_SIGNATURE).modal for index in parent}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for pair in confirmed:
        a, b = find(pair.first), find(pair.second)
        if a == b:
            continue
        if len(modal[a] | modal[b]) > 1 or len(members[a]) + len(members[b]) > MAX_CLUSTER:
            counts.guard_refusals += 1
            continue
        root, child = (a, b) if a < b else (b, a)
        parent[child] = root
        members[root].extend(members.pop(child))
        modal[root] = modal[root] | modal[child]
        counts.merges += 1

    result: list[_Node] = []
    for root in sorted(members):
        survivor, *others = (ordered[index] for index in sorted(members[root]))
        for other in others:
            survivor.aliases.add(other.key)
            _absorb(survivor, other, keep_primary=True)
        result.append(survivor)
    return result


async def _dedup(
    nodes: Sequence[_Node],
    vectors: Mapping[str, np.ndarray],
    *,
    gateway: Gateway,
    ontology: Ontology,
    ids: IdFinder,
    memory: DedupMemory | None = None,
) -> tuple[list[_Node], _DedupCounts]:
    """Merge near-identical restatements among id-less requirements (build.md Conform 2); ``memory``
    holds the verdicts of earlier builds (None: none are kept)."""

    counts = _DedupCounts()
    ordered = sorted(nodes, key=lambda node: node.key)
    groups: dict[tuple[str, str], list[int]] = {}
    for index, node in enumerate(ordered):
        if _dedup_eligible(node, ontology):
            scope = ontology.identity_rule(node.label).scope
            groups.setdefault((scope, node.label), []).append(index)

    signatures, direct, asked = _dedup_candidates(ordered, groups, vectors, ids, counts)
    verdicts = await _dedup_verdicts(asked, ordered, gateway=gateway, ontology=ontology, memory=memory, counts=counts)
    confirmed = direct + [p for index, p in enumerate(asked) if verdicts[index]]
    confirmed.sort(key=lambda p: (not p.direct, -p.cosine, -p.jaccard, p.first, p.second))
    return _dedup_clusters(ordered, confirmed, signatures, counts), counts


async def _adjudicate_dedup(
    gateway: Gateway, ontology: Ontology, first: _Node, second: _Node
) -> tuple[bool, str | None]:
    """The small model's verdict on one pair; a failed call is no merge (with its error code)."""

    messages = dedup_messages(
        ontology, gateway, first.label, [(node.name, _dedup_quote(node)) for node in (first, second)]
    )
    try:
        verdict = await gateway.structured("small", None, messages, SameCommitment, prompt=DEDUP_PROMPT)
    except LLMFatalError:
        raise
    except LLMError as error:
        return False, error.code
    return verdict.same, None


def dedup_messages(
    ontology: Ontology, gateway: Gateway, label: str, statements: Sequence[tuple[str, str]]
) -> list[dict[str, str]]:
    """Messages of one ``dedup_adjudicate.2`` call over two ``(name, quote)`` statements of ``label``."""

    description = ontology.node_type(label).description.strip()
    shown = [
        {"name": prompt_name(name), "quote": prompt_name(quote, STATEMENT_MAX_CHARS)} for name, quote in statements
    ]
    return [
        {
            "role": "system",
            "content": (
                f"Two statements were extracted from {ontology.framing} as nodes of type "
                f"{label}.\nType definition:\n{description}\n\n"
                "Decide whether both state the same commitment, where one statement may be shorter "
                "or more detailed than the other, and the two do not conflict. Answer same=true "
                "(yes) when one restates the other, summarises it or adds detail to it. Answer "
                "same=false (no) when they are different commitments (another actor, behaviour, "
                "object or scope) or when they conflict (different values, limits, polarity or "
                "strength)."
            ),
        },
        {
            "role": "user",
            "content": gateway.untrusted(
                "statements",
                json.dumps({"statements": shown}, ensure_ascii=False),
            ),
        },
    ]


# Edges


@dataclass
class _EdgeDraft:
    relation: str
    source: str
    target: str
    basis: Basis
    fact: str | None
    evidence_lines: list[str]
    fragment_ids: list[str]


@dataclass
class _EdgeAcc:
    relation: str
    source: str
    target: str
    basis: Basis
    fact: str | None
    fragment_ids: set[str]


def _stated_edges(edges: EdgesArtifact, key_of: Mapping[str, str], corpus: _Corpus) -> tuple[list[_EdgeDraft], int]:
    drafts: list[_EdgeDraft] = []
    fallbacks = 0
    for index, raw in enumerate(edges.raw_edges):
        if raw.source_cid not in key_of or raw.target_cid not in key_of:
            raise ConformanceError("conform_unknown_cid", relation=raw.relation, edge=index)
        attributed = [f for f in (corpus.groundable(fid) for fid in raw.fragment_ids) if f is not None]
        span = _snap_best(raw.fact, attributed)
        if span is None:
            attributed_ids = {f.rec.fragment_id for f in attributed}
            others = [
                fragment
                for fid in corpus.unit_fragments([raw.unit_id])
                if fid not in attributed_ids and (fragment := corpus.groundable(fid)) is not None
            ]
            span = _snap_best(raw.fact, others)
        if span is not None:
            fact: str | None = span.text[:FACT_MAX_CHARS]
            evidence_lines = span.lines
            fragment_ids = [span.fragment.rec.fragment_id]
        else:
            fallbacks += 1
            fact = None
            evidence_lines = []
            fragment_ids = [f.rec.fragment_id for f in attributed] or [
                fid for fid in corpus.unit_fragments([raw.unit_id]) if corpus.groundable(fid) is not None
            ]
        drafts.append(
            _EdgeDraft(
                raw.relation,
                key_of[raw.source_cid],
                key_of[raw.target_cid],
                "stated",
                fact,
                evidence_lines,
                fragment_ids,
            )
        )
    return drafts, fallbacks


def _surface_index(nodes: Sequence[_Node], ontology: Ontology) -> tuple[dict[str, list[_Node]], dict[str, _Node]]:
    """The nodes by surface form (name, hub surface, ``surface`` keys) and by explicit id."""

    by_surface: dict[str, list[_Node]] = {}
    by_id: dict[str, _Node] = {}
    hubs = ontology.hub_labels
    for node in nodes:
        surfaces = {normalise_surface(node.name)}
        if node.label in hubs:
            surfaces.add(hub_surface(node.name))
        for key in (node.key, *node.aliases):
            parts = key.split("|", 2)
            if len(parts) == KEY_FIELDS and parts[1] == "surface":
                surfaces.add(parts[2])
        surfaces.discard("")
        for surface in surfaces:
            by_surface.setdefault(surface, []).append(node)
        for explicit in node.explicit_ids:
            by_id.setdefault(explicit, node)
    return by_surface, by_id


def _derived_drafts(
    node: _Node,
    ontology: Ontology,
    by_surface: Mapping[str, Sequence[_Node]],
    by_id: Mapping[str, _Node],
) -> list[_EdgeDraft]:
    """The edges the ontology's derive rules give ``node`` (the attribute value names the target)."""

    drafts: list[_EdgeDraft] = []
    evidence_lines = node.primary.lines if node.primary else []
    fragment_ids = sorted(node.fragment_ids)
    for rule in ontology.derive_rules:
        if node.label not in rule.source_labels:
            continue
        value = ontology.normalise_attribute(node.label, rule.attribute, node.attributes.get(rule.attribute))
        if not value:
            continue
        explicit = normalise_id(value)
        names = dict.fromkeys([normalise_surface(value), hub_surface(value)])
        matches = [
            target
            for target in [
                *(found for name in names for found in by_surface.get(name, [])),
                *([by_id[explicit]] if explicit in by_id else []),
            ]
            if target is not node and target.label in rule.target_labels
        ]
        if matches:
            target = min(matches, key=lambda candidate: candidate.key)
            drafts.append(
                _EdgeDraft(
                    rule.relation,
                    node.key,
                    target.key,
                    "implied",
                    None,
                    evidence_lines,
                    fragment_ids,
                )
            )
    return drafts


def _id_reference_drafts(
    node: _Node, ontology: Ontology, ids: IdFinder, by_id: Mapping[str, _Node]
) -> list[_EdgeDraft]:
    """The edges ``node`` implies by naming another spine node's id in its text."""

    spine_sources = ontology.spine.sources
    relations = ontology.id_reference_relations
    if not relations or node.label not in spine_sources:
        return []
    drafts: list[_EdgeDraft] = []
    evidence_lines = node.primary.lines if node.primary else []
    fragment_ids = sorted(node.fragment_ids)
    texts = [node.primary.text if node.primary else _text(node.attributes.get(QUOTE_ATTRIBUTE))]
    texts.append(_text(node.attributes.get(STMT_ATTRIBUTE)))
    named = [explicit for text in texts for explicit in ids.all(text) if explicit not in node.explicit_ids]
    for explicit in dict.fromkeys(named):
        target = by_id.get(explicit)
        if target is None or target is node or target.label not in spine_sources:
            continue
        fact = node.primary.text[:FACT_MAX_CHARS] if node.primary and explicit in ids.all(node.primary.text) else None
        drafts.extend(
            _EdgeDraft(relation, node.key, target.key, "implied", fact, evidence_lines, fragment_ids)
            for relation in relations
            if ontology.is_legal(relation, node.label, target.label)
        )
    return drafts


def _implied_edges(nodes: Sequence[_Node], ontology: Ontology, ids: IdFinder) -> list[_EdgeDraft]:
    by_surface, by_id = _surface_index(nodes, ontology)
    drafts: list[_EdgeDraft] = []
    for node in nodes:
        drafts.extend(_derived_drafts(node, ontology, by_surface, by_id))
        drafts.extend(_id_reference_drafts(node, ontology, ids, by_id))
    return drafts


def _text(value: AttributeValue) -> str:
    return value if isinstance(value, str) else ""


def _legalise(
    drafts: Iterable[_EdgeDraft],
    nodes: Mapping[str, _Node],
    ontology: Ontology,
    counters: dict[str, Counter[str]],
) -> tuple[dict[str, _EdgeAcc], int]:
    merged: dict[str, _EdgeAcc] = {}
    flipped = 0
    for draft in drafts:
        source, target = nodes[draft.source], nodes[draft.target]
        outcome = ontology.legalise(draft.relation, source.label, target.label, draft.evidence_lines)
        if isinstance(outcome, Dropped):
            counters.setdefault(draft.relation, Counter())[outcome.reason] += 1
            continue
        relation = outcome.relation
        source_key, target_key = draft.source, draft.target
        if outcome.flipped:
            flipped += 1
            source_key, target_key = target_key, source_key
        if source_key == target_key:
            counters.setdefault(draft.relation, Counter())["self_loop"] += 1
            continue
        if ontology.is_symmetric(relation) and target_key < source_key:
            source_key, target_key = target_key, source_key
        edge_key = f"{source_key}|{relation}|{target_key}"
        existing = merged.get(edge_key)
        if existing is None:
            merged[edge_key] = _EdgeAcc(
                relation,
                source_key,
                target_key,
                draft.basis,
                draft.fact,
                set(draft.fragment_ids),
            )
            continue
        existing.fragment_ids |= set(draft.fragment_ids)
        if BASIS_RANK[draft.basis] < BASIS_RANK[existing.basis]:
            existing.basis = draft.basis
            existing.fact = draft.fact or existing.fact
        elif existing.fact is None:
            existing.fact = draft.fact
    return merged, flipped


def _local(edge: _EdgeAcc, nodes: Mapping[str, _Node], corpus: _Corpus, locality: str) -> bool:
    if locality != "strict":
        return True
    units = [
        {unit for fid in nodes[key].fragment_ids for unit in corpus.units_of.get(fid, [])}
        for key in (edge.source, edge.target)
    ]
    return bool(units[0] & units[1])


# Conform


async def conform(
    plan: PlanArtifact,
    resolved: ResolveArtifact,
    edges: EdgesArtifact,
    *,
    services: StepServices,
    vectors: VectorCache,
    at: str | None = None,
    memory: DedupMemory | None = None,
    persisted: Mapping[str, PersistedIdentity] | None = None,
) -> TargetGraph:
    """The target graph of one build (build.md step 5). ``at`` stamps statements (default: now, UTC);
    name and search vectors come from and go to ``vectors``; ``memory`` holds earlier dedup verdicts;
    ``persisted`` (``update`` builds) the persisted labels by identity key and alias (Resolve step 6),
    kept again for the keys this step gives (``_keep_persisted_labels``). ``services`` gives the gateway,
    ontology and options."""

    gateway, ontology, options = services.gateway, services.ontology, services.options
    stamp = at or datetime.now(UTC).isoformat(timespec="seconds")
    corpus = _Corpus(plan)
    ids = IdFinder(ontology)

    # 1-2. Grounding, final keys, merge of equal (alike) keys; requirement dedup
    hubs = ontology.hub_labels
    grounded = [
        _ground_node(
            node,
            edges.attributes.get(node.cid, {}),
            corpus,
            hub=node.label in hubs,
        )
        for node in resolved.canonical
    ]
    quote_fallbacks = sum(1 for node in grounded if node.primary is None)
    for node in grounded:
        _final_key(node, ontology, ids)
    labels_kept = _keep_persisted_labels(grounded, persisted or {}, ontology)
    name_vectors = await vectors.mapping(
        [node.canonical.name_embedding_text for node in grounded if node.canonical.name_embedding_text is not None]
        + [embedding_text(node.label, node.name) for node in grounded if _dedup_eligible(node, ontology)]
    )
    merged, span_merges, span_splits = _merge_equal_keys(grounded, name_vectors)
    merged, dedup = await _dedup(merged, name_vectors, gateway=gateway, ontology=ontology, ids=ids, memory=memory)
    # Split keys and dedup aliases can name a persisted node only now.
    labels_kept += _keep_persisted_labels(merged, persisted or {}, ontology)
    if labels_kept:
        log_event(logger, INFO, "graph.conform.labels_kept", nodes=labels_kept)
    nodes = {node.key: node for node in sorted(merged, key=lambda node: node.key)}
    key_of = {cid: node.key for node in nodes.values() for cid in node.cids}
    stated, fact_fallbacks = _stated_edges(edges, key_of, corpus)

    # 3-5. Implied edges, legalisation, merge, locality
    dropped: dict[str, Counter[str]] = {}
    drafts = [*stated, *_implied_edges(list(nodes.values()), ontology, ids)]
    accumulated, flipped = _legalise(drafts, nodes, ontology, dropped)
    kept: list[_EdgeAcc] = []
    for edge in accumulated.values():
        if _local(edge, nodes, corpus, options.locality):
            kept.append(edge)
        else:
            dropped.setdefault(edge.relation, Counter())["locality"] += 1
    target_edges = tuple(
        TargetEdge(
            edge_key=f"{edge.source}|{edge.relation}|{edge.target}",
            relation=edge.relation,
            source_key=edge.source,
            target_key=edge.target,
            basis=edge.basis,
            fact=edge.fact,
            fragment_ids=tuple(corpus.ordered(edge.fragment_ids)),
        )
        for edge in sorted(kept, key=lambda edge: f"{edge.source}|{edge.relation}|{edge.target}")
    )

    # 6. Statements, mentions, attributes, embeddings
    hub_index = _hub_index([node for node in nodes.values() if node.label in ontology.hub_labels], persisted or {})
    named = _named_in(hub_index, corpus)
    mentions = _mentions(named, nodes)
    texts: dict[str, tuple[str, str]] = {}
    for node in nodes.values():
        stmt = ontology.normalise_attribute(node.label, STMT_ATTRIBUTE, node.attributes.get(STMT_ATTRIBUTE))
        search = f"{embedding_text(node.label, node.name)}. {stmt}" if stmt else embedding_text(node.label, node.name)
        texts[node.key] = (search, embedding_text(node.label, node.name))
    # Stores every vector a target node names, so Persist finds them by text hash.
    await vectors.mapping(text for pair in texts.values() for text in pair)
    attributes_failed = set(edges.attributes_failed)

    target_nodes = tuple(
        TargetNode(
            identity_key=node.key,
            identity_aliases=tuple(sorted(alias for alias in node.aliases if alias != node.key)),
            label=node.label,
            name=node.name,
            attributes=_attributes(node, ontology),
            quote=ontology.normalise_attribute(node.label, QUOTE_ATTRIBUTE, node.primary.text)
            if node.primary
            else None,
            evidence=tuple(
                sorted(
                    node.evidence,
                    key=lambda e: (corpus.fragments[e.fragment_id].rank, e.line_start),
                )
            ),
            statements=_statements(
                node, corpus, stamp, named.get(node.key, {}) if node.label in hubs else None, hub_index
            ),
            mentions=mentions.get(node.key, {}),
            embedding_text=texts[node.key][0],
            name_embedding_text=texts[node.key][1],
            explicit_ids=tuple(sorted(node.explicit_ids)),
            attributes_missing=all(cid in attributes_failed for cid in node.cids),
            label_disagreement=node.label_disagreement if node.label_disagreement != node.label else None,
        )
        for node in nodes.values()
    )
    counters = TargetCounters(
        dropped_edges={relation: dict(sorted(reasons.items())) for relation, reasons in sorted(dropped.items())},
        flipped_edges=flipped,
        implied_edges=sum(1 for edge in target_edges if edge.basis == "implied"),
        exclusive_conflicts=_exclusive_conflicts(target_edges, nodes, ontology),
        quote_fallbacks=quote_fallbacks,
        fact_fallbacks=fact_fallbacks,
        span_merges=span_merges,
        span_splits=span_splits,
        dedup_candidates=dedup.candidates,
        dedup_merges=dedup.merges,
        dedup_adjudications=dedup.adjudications,
        dedup_guard_refusals=dedup.guard_refusals,
        dedup_verdicts_reused=dedup.reused,
    )
    graph = TargetGraph(nodes=target_nodes, edges=target_edges, counters=counters)

    # 7. Invariants
    check_invariants(graph, plan, ontology)
    log_event(
        logger,
        INFO,
        "graph.conform.done",
        nodes=len(target_nodes),
        edges=len(target_edges),
        dropped=sum(sum(reasons.values()) for reasons in counters.dropped_edges.values()),
        flipped=counters.flipped_edges,
        implied=counters.implied_edges,
        exclusive_conflicts=counters.exclusive_conflicts,
        quote_fallbacks=counters.quote_fallbacks,
        fact_fallbacks=counters.fact_fallbacks,
        span_merges=counters.span_merges,
        span_splits=counters.span_splits,
        dedup_candidates=counters.dedup_candidates,
        dedup_merges=counters.dedup_merges,
        dedup_adjudications=counters.dedup_adjudications,
        dedup_guard_refusals=counters.dedup_guard_refusals,
        dedup_verdicts_reused=counters.dedup_verdicts_reused,
    )
    return graph


def _attributes(node: _Node, ontology: Ontology) -> dict[str, AttributeValue]:
    specs = ontology.attribute_specs(node.label)
    values: dict[str, AttributeValue] = {}
    for name in sorted(node.attributes):
        spec = specs.get(name)
        if spec is None or not spec.store or name == QUOTE_ATTRIBUTE:
            continue
        value = ontology.normalise_attribute(node.label, name, node.attributes[name])
        if value is not None:
            values[name] = value
    return values


def _statements(
    node: _Node,
    corpus: _Corpus,
    stamp: str,
    named: Mapping[str, int] | None = None,
    hub_index: NameIndex | None = None,
) -> tuple[StatementRec, ...]:
    """One statement per evidence fragment. A hub's (``named``: the fragments naming it, by
    ``_named_in``) come only from fragments that name it: its span there, else the first line naming
    it; a hub named nowhere has none."""

    texts: dict[str, list[str]] = {}
    for span in sorted(node.spans, key=lambda s: (s.fragment.rank, s.first)):
        if named is None or span.fragment.rec.fragment_id in named:
            texts.setdefault(span.fragment.rec.fragment_id, []).append(span.text)
    for fragment in node.fallback:
        if named is None:
            texts.setdefault(fragment.rec.fragment_id, []).append(_fallback_text(node.name, fragment))
        elif fragment.rec.fragment_id in named and hub_index is not None:
            line = _named_line(hub_index, node.key, fragment)
            if line is not None:
                texts.setdefault(fragment.rec.fragment_id, []).append(line)
    entries: list[StatementRec] = []
    for fragment_id in corpus.ordered(texts):
        rec = corpus.fragments[fragment_id].rec
        document = corpus.documents.get(rec.document_id)
        entries.append(
            StatementRec(
                fragment_id=fragment_id,
                document_id=rec.document_id,
                revision_id=document.revision_id if document else "",
                version=document.version if document else 0,
                text="\n".join(part for part in texts[fragment_id] if part)[:STATEMENT_MAX_CHARS],
                at=stamp,
            )
        )
    return tuple(_cap_statements(entries))


def _fallback_text(name: str, fragment: _Fragment) -> str:
    """Lines of a fallback fragment that name the node, else the fragment's first lines (verbatim);
    not for hubs (``_statements``)."""

    needle = tuple(tokens(name))
    for index, line_tokens in enumerate(fragment.line_tokens):
        if needle and _count(needle, line_tokens):
            return fragment.text(index, index)
    return fragment.text(0, len(fragment.lines) - 1)


def _named_line(index: NameIndex, key: str, fragment: _Fragment) -> str | None:
    """The first line of ``fragment`` naming hub ``key`` as ``index`` counts it, or None (the name
    only spans a line break)."""

    for number, line in enumerate(fragment.lines):
        if index.count(hub_words(line)).get(key):
            return fragment.text(number, number)
    return None


def _cap_statements(entries: list[StatementRec]) -> list[StatementRec]:
    if len(entries) <= MAX_STATEMENTS:
        return entries
    keep: set[int] = set()
    by_document: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        by_document.setdefault(entry.document_id, []).append(index)
    for indexes in by_document.values():
        keep.update({indexes[0], indexes[-1]})
    for index in range(len(entries)):
        if len(keep) >= MAX_STATEMENTS:
            break
        keep.add(index)
    return [entries[index] for index in sorted(keep)]


def _count(needle: tuple[str, ...], haystack: Sequence[str]) -> int:
    size = len(needle)
    count = 0
    index = 0
    while index <= len(haystack) - size:
        if tuple(haystack[index : index + size]) == needle:
            count += 1
            index += size
        else:
            index += 1
    return count


def _hub_index(nodes: Iterable[_Node], persisted: Mapping[str, PersistedIdentity]) -> NameIndex:
    """The names of hub ``nodes`` by node key (``terms.hub_name_index``): its name, and the value of
    each ``surface`` identity alias of its own or of the persisted node it continues
    (``persisted_aliases``: names folded in by consolidation or a rename)."""

    return hub_name_index(
        (
            node.key,
            node.name,
            [*sorted(node.aliases), *persisted_aliases((node.key, *sorted(node.aliases)), persisted)],
        )
        for node in nodes
    )


def _named_in(index: NameIndex, corpus: _Corpus) -> dict[str, dict[str, int]]:
    """Per hub key of ``index``: groundable fragment id to whole-word occurrences of its names."""

    return named_in(
        index,
        (
            fragment.rec
            for fragment_id in corpus.ordered(corpus.fragments)
            if (fragment := corpus.groundable(fragment_id)) is not None
        ),
    )


def _mentions(named: Mapping[str, Mapping[str, int]], nodes: Mapping[str, _Node]) -> dict[str, dict[str, int]]:
    """``MENTIONED_IN`` counts per hub node key: the fragments naming it (``_named_in``) it is not
    grounded in."""

    result: dict[str, dict[str, int]] = {}
    for key, counts in named.items():
        grounded = nodes[key].fragment_ids
        kept = {fragment_id: count for fragment_id, count in counts.items() if fragment_id not in grounded}
        if kept:
            result[key] = kept
    return result


def _exclusive_conflicts(edges: Iterable[TargetEdge], nodes: Mapping[str, _Node], ontology: Ontology) -> int:
    exclusive = ontology.exclusive_pairs
    targets: Counter[tuple[str, str, str]] = Counter()
    for edge in edges:
        source_label, target_label = (
            nodes[edge.source_key].label,
            nodes[edge.target_key].label,
        )
        if (edge.relation, source_label, target_label) in exclusive:
            targets[(edge.relation, edge.source_key, target_label)] += 1
    return sum(count - 1 for count in targets.values() if count > 1)


# Invariants


def _illegal_edges(graph: TargetGraph, ontology: Ontology) -> int:
    """Edges whose ends, relation, key or symmetric order break D2."""

    label_of = {node.identity_key: node.label for node in graph.nodes}
    illegal = 0
    for edge in graph.edges:
        source, target = label_of.get(edge.source_key), label_of.get(edge.target_key)
        expected_key = f"{edge.source_key}|{edge.relation}|{edge.target_key}"
        if (
            source is None
            or target is None
            or edge.relation not in ontology.relations
            or not ontology.is_legal(edge.relation, source, target)
            or edge.edge_key != expected_key
            or (ontology.is_symmetric(edge.relation) and edge.target_key < edge.source_key)
        ):
            illegal += 1
    return illegal


def _duplicates(graph: TargetGraph) -> int:
    """Repeated node keys, aliases that collide with a key or another alias, and repeated edge keys (D3)."""

    keys = Counter(node.identity_key for node in graph.nodes)
    aliases = Counter(alias for node in graph.nodes for alias in node.identity_aliases)
    duplicate = sum(count - 1 for count in keys.values() if count > 1)
    duplicate += sum(1 for alias in aliases if alias in keys or aliases[alias] > 1)
    edge_keys = Counter(edge.edge_key for edge in graph.edges)
    duplicate += sum(count - 1 for count in edge_keys.values() if count > 1)
    return duplicate


def _ungrounded(graph: TargetGraph, fragments: Mapping[str, FragmentRec]) -> int:
    """Nodes without evidence inside a grounding fragment and stated/implied edges without one (D4)."""

    ungrounded = 0
    for node in graph.nodes:
        if not node.evidence or any(not _inside(lines, fragments) for lines in node.evidence):
            ungrounded += 1
    for edge in graph.edges:
        if edge.basis in ("stated", "implied") and (
            not edge.fragment_ids or any(fid not in fragments for fid in edge.fragment_ids)
        ):
            ungrounded += 1
    return ungrounded


def check_invariants(graph: TargetGraph, plan: PlanArtifact, ontology: Ontology) -> None:
    """D1-D4 of data-model.md on a target graph; the first violated invariant raises ``ConformanceError``."""

    fragments = {f.fragment_id: f for f in plan.fragments if f.status in GROUNDING_STATUSES}
    labels = set(ontology.labels)

    unlabelled = sum(1 for node in graph.nodes if node.label not in labels)
    if unlabelled:
        raise ConformanceError("conform_invariant_violated", invariant="D1", count=unlabelled)
    illegal = _illegal_edges(graph, ontology)
    if illegal:
        raise ConformanceError("conform_invariant_violated", invariant="D2", count=illegal)
    duplicate = _duplicates(graph)
    if duplicate:
        raise ConformanceError("conform_invariant_violated", invariant="D3", count=duplicate)
    ungrounded = _ungrounded(graph, fragments)
    if ungrounded:
        raise ConformanceError("conform_invariant_violated", invariant="D4", count=ungrounded)


def _inside(lines: EvidenceLines, fragments: Mapping[str, FragmentRec]) -> bool:
    fragment = fragments.get(lines.fragment_id)
    return fragment is not None and fragment.line_start <= lines.line_start <= lines.line_end <= fragment.line_end
