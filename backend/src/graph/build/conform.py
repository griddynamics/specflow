"""Conform: the target graph from canonical nodes, raw edges and attributes."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Final

import numpy as np
from pydantic import BaseModel, ConfigDict

from graph.build.answered import AnsweredCheck, AnsweredMemory, Question, answered_elsewhere, passages_of
from graph.build.consolidate import (
    DECISION_KIND,
    UNSURE,
    evidence_hash,
    evidence_matches,
    nearest_pairs,
    pair_cosines,
    pair_hash,
)
from graph.build.models import (
    AttributeValue,
    Basis,
    CanonicalNode,
    Confidence,
    DedupGuard,
    EdgesArtifact,
    EvidenceLines,
    FragmentRec,
    PlanArtifact,
    QuarantinedTargetEdge,
    RefusedDedupPair,
    ResolveArtifact,
    SourceQuote,
    StatementRec,
    TargetCounters,
    TargetEdge,
    TargetGraph,
    TargetNode,
)
from graph.build.normalise import (
    ConformJudge,
    ConformJudgeMemory,
    FallbackJudge,
    Normalised,
    NormEdge,
    NormNode,
    OwnerJudge,
    normalise,
    resolves_conflicts,
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
    hub_value_words,
    hub_words,
    persisted_aliases,
    tokens,
    unit_rows,
)
from graph.build.terms import NameIndex, hub_name_index, named_in
from graph.build.validator import InMemoryGraphView, ViewEdge, ViewNode, exclusive_conflicts, violations
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
from graph.core.ontology import QUOTE_ATTRIBUTE, Dropped, NodeView, Ontology, Pair, QuarantineReason
from logger.logger import DEBUG, INFO, WARNING, get_logger, log_event

logger = get_logger("graph.build.conform")

MIN_DEDUP_GROUP: Final = 2
"""Nodes a dedup group needs to hold a pair."""

STMT_ATTRIBUTE: Final = "stmt"
"""Attribute name the data model gives a meaning; the quote's is ``graph.core.ontology.QUOTE_ATTRIBUTE``."""

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
DEDUP_PROMPT: Final = "dedup_adjudicate.3"
DEDUP_CANDIDATE_COSINE: Final = 0.85
DEDUP_MERGE_COSINE: Final = 0.95
DEDUP_MERGE_TOKENS: Final = 0.7
"""Requirement dedup: candidates at ``name_embedding`` cosine >= 0.85 (each node's ``NEAREST_NAMES``
nearest); merge at cosine >= 0.95 and token Jaccard >= 0.7, otherwise the small model decides."""
NEAREST_NAMES: Final = 10
"""Nearest names by ``name_embedding`` cosine that each node pairs with as dedup candidates."""
DEDUP_RICH_K: Final = 3
DEDUP_RICH_FLOOR: Final = 0.80
"""Requirement dedup, candidate (rich) vectors (``dedup_candidate_text``): each node's ``DEDUP_RICH_K`` nearest at
cosine >= ``DEDUP_RICH_FLOOR`` are candidates too.
"""
DEDUP_QUOTE_MAX_CHARS: Final = 300
"""Longest quote in a dedup candidate text."""
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
"""A number token: digits, optional separators and a unit, but not digits that are part of an id."""
_ISO_DATE: Final = re.compile(r"(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])")
_RECORD_ID: Final = re.compile(r"(?<![\w-])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d+(?![\w-])")
"""An id of a record row, any family (``_record_rows``)."""
_CURRENCY_SIGNS: Final = {"$": "usd", "\u20ac": "eur", "\u00a3": "gbp", "\u00a5": "jpy"}
_PLAIN: Final = "plain"
"""The number kind of a unitless number (``_numbers``)."""


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
_QUOTE_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+(?=[^\sa-z])")
"""A sentence end inside a line for the persisted quote (``_quote_sentences``): ``.``, ``!`` or ``?``,
whitespace, then a character that is no lower-case letter (so "e.g. the" is one sentence)."""
_ABBREVIATIONS: Final = frozenset(re.findall(r"[\w.]+", "no nos nr fig figs eq ref sec vol vs cf approx e.g i.e dr mr"))
"""Words whose period ends no sentence (``_sentence_bounds``): "No. 7", "Fig. 3", "e.g. Redis"."""


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
    quote: str | None = None
    """The quote (or fact) the span holds; None for a line found by a name or an id."""

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


def _best_lines(quote: Counter[str], line_tokens: Sequence[Sequence[str]]) -> tuple[int, int, float] | None:
    """Best contiguous run of lines (at most ``MAX_SPAN_LINES``) for the quote tokens as ``(first,
    last, overlap)``: the largest share of the quote's tokens, then the shortest run, then the
    earliest; None when no line holds a quote token."""

    total = sum(quote.values())
    best: tuple[int, int, float] | None = None
    count = len(line_tokens)
    for first in range(count):
        if not any(token in quote for token in line_tokens[first]):
            continue
        seen: Counter[str] = Counter()
        hits = 0
        for last in range(first, min(count, first + MAX_SPAN_LINES)):
            seen.update(line_tokens[last])
            new_hits = sum((seen & quote).values())
            if new_hits > hits or last == first:
                hits = new_hits
                found = (first, last, hits / total)
                if best is None or (-found[2], last - first, first) < (-best[2], best[1] - best[0], best[0]):
                    best = found
            if hits == total:
                break
    return best


def _best_span(quote: Counter[str], fragment: _Fragment) -> _Span | None:
    """Best contiguous span of ``fragment`` for the quote tokens, or None (no token in common)."""

    found = _best_lines(quote, fragment.line_tokens)
    return _Span(fragment, found[0], found[1], found[2]) if found is not None else None


def span_overlap(quote: str, text: str) -> float:
    """Conform's snap score of ``quote`` in ``text``: the share of the quote's tokens (``resolve.tokens``) held by the
    best contiguous run of the text's lines (``SNAP_OVERLAP`` and above snaps); 0.0 when none is held.
    """

    wanted = Counter(tokens(quote))
    if not wanted:
        return 0.0
    found = _best_lines(wanted, [tokens(line) for line in text.split("\n")])
    return found[2] if found is not None else 0.0


def _snap_each(text: str | None, fragments: Sequence[_Fragment]) -> list[_Span]:
    """Accepted spans of ``text``, at most one per fragment, in fragment order."""

    quote = Counter(tokens(text or ""))
    if not quote:
        return []
    spans = [_best_span(quote, fragment) for fragment in fragments]
    return [replace(span, quote=text) for span in spans if span is not None and span.overlap >= SNAP_OVERLAP]


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
    quotes: list[str] = field(default_factory=list)
    """The texts of its source quotes, in source order (a node without a snapped quote reads the first)."""
    unsnapped_quotes: int = 0
    """Source quotes snapped nowhere (grounding statistics; the canonical node's own, before merges)."""
    needles: tuple[tuple[str, ...], ...] = ()
    """A hub's names (``_name_needles``); a quote span whose lines hold one ranks first as primary."""

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

    @property
    def quote(self) -> str:
        """The quote that names the node: its primary span's, else its first source quote, else empty."""

        if self.primary is not None and self.primary.quote:
            return self.primary.quote
        return self.quotes[0] if self.quotes else ""


@dataclass(frozen=True)
class _Grounder:
    """Grounds canonical nodes in one corpus (``_ground_node``); ``secondary`` allows id lines."""

    corpus: _Corpus
    ids: IdFinder
    hubs: frozenset[str]
    secondary: bool = True

    def unit_others(self, own: Sequence[_Fragment]) -> list[_Fragment]:
        """The groundable fragments of the units of ``own`` that are not in ``own``."""

        own_ids = {fragment.rec.fragment_id for fragment in own}
        units = {unit for fragment in own for unit in self.corpus.units_of.get(fragment.rec.fragment_id, [])}
        return [
            fragment
            for fid in self.corpus.unit_fragments(units)
            if fid not in own_ids and (fragment := self.corpus.groundable(fid)) is not None
        ]

    def snap_source(self, source: SourceQuote) -> tuple[list[_Fragment], list[_Span]]:
        """A source quote's groundable fragments and its spans: in those fragments, else the single best
        span in the other fragments of their units."""

        own = [f for f in (self.corpus.groundable(fid) for fid in source.fragment_ids) if f is not None]
        spans = _snap_each(source.quote, own)
        if not spans:
            best = _snap_best(source.quote, self.unit_others(own))
            spans = [best] if best is not None else []
        return own, spans

    def id_lines(self, explicit_ids: Iterable[str], fragments: Iterable[_Fragment]) -> list[_Span]:
        """Per fragment, the first line holding one of ``explicit_ids`` (secondary evidence)."""

        wanted = set(explicit_ids)
        if not (wanted and self.secondary):
            return []
        spans = []
        for fragment in fragments:
            line = next(
                (index for index, text in enumerate(fragment.lines) if wanted & set(self.ids.all(text))),
                None,
            )
            if line is not None:
                spans.append(_Span(fragment, line, line, 0.0))
        return spans


def _ground_node(
    node: CanonicalNode,
    attributes: Mapping[str, AttributeValue],
    grounder: _Grounder,
    quotes: Sequence[SourceQuote],
) -> _Node:
    """Ground one canonical node per source quote: each quote snaps in its own fragments, else in the other fragments of
    their units; a quote snapped nowhere is counted and, for a node that is no hub, a line of its fragments holding one
    of the node's explicit ids is secondary evidence (so are such lines in the fragments no quote came from); a fragment
    left without a span is dropped.
    """

    corpus = grounder.corpus
    attributed = [f for f in (corpus.groundable(fid) for fid in node.fragment_ids) if f is not None]
    if not attributed:
        raise ConformanceError("conform_node_without_fragments", label=node.label)
    hub = node.label in grounder.hubs
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
        quotes=[source.quote for source in quotes],
    )
    spans: dict[tuple[str, int, int], _Span] = {}
    unsnapped: list[_Fragment] = []
    covered: set[str] = set()
    for source in quotes:
        own, found = grounder.snap_source(source)
        covered.update(fragment.rec.fragment_id for fragment in own)
        if not found:
            result.unsnapped_quotes += 1
            unsnapped.extend(own)
        _keep_spans(spans, found)
    # Fragments no quote of their own snapped in (an unsnapped source's, or those no quote came from):
    # the node's other quotes, then (no hub) a line holding one of its explicit ids.
    lone = [f for f in attributed if f.rec.fragment_id not in covered]
    taken = {fragment_id for fragment_id, _, _ in spans}
    open_ = {f.rec.fragment_id: f for f in (*unsnapped, *lone) if f.rec.fragment_id not in taken}
    for source in quotes:
        _keep_spans(spans, _snap_each(source.quote, list(open_.values())))
    if hub:
        result.needles = tuple(_name_needles(node))
    quoted = [span for span in spans.values() if span.quote is not None]
    if quoted:
        result.primary = min(quoted, key=lambda span: _primary_rank(result, span))
    snapped = {fragment_id for fragment_id, _, _ in spans}
    if hub:
        _add_name_lines(result, node, attributed, snapped, spans)
    else:
        rest = [fragment for fid, fragment in open_.items() if fid not in snapped]
        _keep_spans(spans, grounder.id_lines(node.explicit_ids, rest))
        if not spans:
            result.fallback = attributed
    result.spans = sorted(spans.values(), key=lambda span: (span.fragment.rank, span.first, span.last))
    return result


def _primary_rank(node: _Node, span: _Span) -> tuple[bool, tuple[float, int, int, int]]:
    """The order of a node's quote spans for its primary span: for a hub (``needles``), a span whose
    lines name it comes first (a quote of a context line never wins over one naming the hub); then the
    span rank."""

    return bool(node.needles) and not _names_in(node.needles, span), span.rank


def _names_in(needles: Sequence[tuple[str, ...]], span: _Span) -> bool:
    """True when a line of ``span`` holds one of ``needles`` as whole folded words."""

    return any(
        _line_names(needles, line_tokens) for line_tokens in span.fragment.line_tokens[span.first : span.last + 1]
    )


def _span_key(span: _Span) -> tuple[str, int, int]:
    return span.fragment.rec.fragment_id, span.first, span.last


def _keep_spans(spans: dict[tuple[str, int, int], _Span], found: Iterable[_Span]) -> None:
    """Add ``found`` to ``spans``; of two spans on the same lines the better-ranked one stays."""

    for span in found:
        key = _span_key(span)
        held = spans.get(key)
        if held is None or span.rank < held.rank:
            spans[key] = span


def _add_name_lines(
    result: _Node,
    node: CanonicalNode,
    attributed: Sequence[_Fragment],
    snapped: Collection[str],
    spans: dict[tuple[str, int, int], _Span],
) -> None:
    """A hub's attributed fragments without a quote span: the first line naming it, else the whole
    fragment (``fallback``)."""

    needles = _name_needles(node)
    for fragment in attributed:
        if fragment.rec.fragment_id in snapped:
            continue
        line = _name_line(needles, fragment)
        if line is None:
            result.fallback.append(fragment)
        else:
            _keep_spans(spans, [_Span(fragment, line, line, 0.0)])


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
        if _line_names(needles, line_tokens):
            return index
    return None


def _line_names(needles: Sequence[tuple[str, ...]], line_tokens: Sequence[str]) -> bool:
    folded = [fold_plural(token) for token in line_tokens]
    return any(_count(needle, folded) for needle in needles)


def _final_key(node: _Node, ontology: Ontology, ids: IdFinder, persisted: Collection[str] = frozenset()) -> bool:
    """Replace a provisional key by the defining line's id or span key; the defining lines are the primary span's,
    located by its quote.
    """

    if not node.provisional:
        return True
    node.provisional = False
    if node.primary is None:
        return False
    if persisted:
        quoted = sorted((span for span in node.spans if span.quote is not None), key=lambda span: span.rank)
        node.primary = next(
            (
                span
                for span in quoted
                if (found := _span_identity(span, node.label, ontology, ids)) is not None and found[0] in persisted
            ),
            node.primary,
        )
    found = _span_identity(node.primary, node.label, ontology, ids)
    if found is None:
        return False
    node.key, label, own = found
    if label is not None and own is not None:
        node.label = label
        node.explicit_ids.add(own)
    return True


def _span_identity(
    span: _Span, label: str, ontology: Ontology, ids: IdFinder
) -> tuple[str, str | None, str | None] | None:
    """The final key a span gives a provisional node of ``label``, with the label and id an explicit
    id leading its first line fixes (else None, None); None when the span gives no key value."""

    scope = ontology.identity_rule(label).scope
    lines = span.lines
    own = ids.own(lines[0]) if lines else None
    fixed = explicit_label(ontology, own, scope) if own else None
    if own is not None and fixed is not None:
        return identity_key(scope, "id", own), fixed, own
    defining = _quote_in_lines(span.quote, lines)
    if defining:
        return identity_key(scope, "span", bounded_key_value(defining)), None, None
    return None


def _keep_persisted_labels(
    nodes: Iterable[_Node], persisted: Mapping[str, PersistedIdentity], ontology: Ontology
) -> int:
    """Resolve step 6 for the keys Conform gives (an id-less requirement's ``span`` key, a split key, a dedup alias): a
    node, not a hub, whose key or an alias names a persisted node of another extracted label of its identity scope takes
    that label, in place, and keeps the extracted one as ``label_disagreement``; its key stays.
    """

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


def _quote_in_lines(quote: str | None, lines: Sequence[str]) -> str:
    """The span key value: the normalised sentence(s) of the snapped lines that hold the quote."""

    whole = normalise_surface(" ".join(line.strip() for line in lines))
    located = normalise_surface(quote) if quote else ""
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


def _quote_sentences(span: _Span, needles: Sequence[tuple[str, ...]] = ()) -> str:
    """The persisted ``quote`` of a primary span: verbatim, the run of consecutive sentences of its text
    (``_Span.text``; ``_sentence_bounds``) that holds at least ``SNAP_OVERLAP`` of the source quote's tokens; of
    several, for a hub (``needles``) one naming it first, then the fewest sentences, the most quote tokens, the
    earliest.
    """

    text = span.text
    quote = Counter(tokens(span.quote or ""))
    total = sum(quote.values())
    bounds = _sentence_bounds(text)
    if not total or len(bounds) <= 1:
        return text
    words = [tokens(text[start:end]) for start, end in bounds]
    best: tuple[tuple[bool, int, int, int], int, int] | None = None
    for first in range(len(bounds)):
        run: list[str] = []
        for last in range(first, len(bounds)):
            run += words[last]
            hits = sum((Counter(run) & quote).values())
            if hits < SNAP_OVERLAP * total:
                continue
            rank = (bool(needles) and not _line_names(needles, run), last - first, -hits, first)
            if best is None or rank < best[0]:
                best = (rank, bounds[first][0], bounds[last][1])
    return text[best[1] : best[2]] if best is not None else text


def _sentence_bounds(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` offsets of the sentences of ``text``, blank space trimmed: its lines, each split
    at ``_QUOTE_SENTENCE_END`` unless the word before it is an ``_ABBREVIATIONS`` one."""

    bounds: list[tuple[int, int]] = []
    offset = 0
    for line in text.split("\n"):
        start = 0
        cuts = [
            (match.start(), match.end())
            for match in _QUOTE_SENTENCE_END.finditer(line)
            if line[: match.start()].rsplit(maxsplit=1)[-1].lstrip("([").rstrip(".").casefold() not in _ABBREVIATIONS
        ]
        for end, resume in [*cuts, (len(line), len(line))]:
            piece = line[start:end]
            if piece.strip():
                lead = len(piece) - len(piece.lstrip())
                bounds.append((offset + start + lead, offset + start + len(piece.rstrip())))
            start = resume
        offset += len(line) + 1
    return bounds


def _fold(text: str) -> str:
    """NFKC, case-folded, single spaces; inner and end punctuation kept (for locating a quote)."""

    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _kind(key: str) -> str:
    parts = key.split("|", 2)
    return parts[1] if len(parts) == KEY_FIELDS else ""


def _merge_equal_keys(nodes: Sequence[_Node], vectors: Mapping[str, np.ndarray]) -> tuple[list[_Node], int, int]:
    """Merge nodes with equal final keys; ``(nodes, merges, split nodes)``."""

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
    """Merge ``other`` into ``survivor``: cids, aliases, ids, missing attributes, quotes, evidence."""

    survivor.cids.extend(other.cids)
    survivor.aliases |= {alias for alias in other.aliases if alias != survivor.key}
    survivor.explicit_ids |= other.explicit_ids
    for name, value in other.attributes.items():
        survivor.attributes.setdefault(name, value)
    survivor.quotes.extend(quote for quote in other.quotes if quote not in survivor.quotes)
    _join_evidence(survivor, other, keep_primary=keep_primary)


def _join_evidence(survivor: _Node, other: _Node, *, keep_primary: bool) -> None:
    spans = {(s.fragment.rec.fragment_id, s.first, s.last): s for s in [*survivor.spans, *other.spans]}
    survivor.spans = sorted(spans.values(), key=lambda s: (s.fragment.rank, s.first, s.last))
    candidates = [span for span in (survivor.primary, other.primary) if span is not None]
    if not (keep_primary and survivor.primary is not None):
        survivor.primary = min(candidates, key=lambda span: _primary_rank(survivor, span)) if candidates else None
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


async def gather_verdicts[T](
    calls: Sequence[Coroutine[object, object, tuple[T, str | None]]], *, event: str
) -> list[T]:
    """Await ``(verdict, error code)`` calls concurrently, in order; a failed call gives the verdict it returned with
    its error code (the caller's "no verdict").
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
    return [verdict for verdict, _ in results]


class SameCommitment(BaseModel):
    """Small-model verdict of ``dedup_adjudicate.3``: do two statements make one commitment, and how
    sure is it (``low``: unsure, so no merge and nothing stored)?"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    same: bool
    confidence: Confidence


_Values = Mapping[str, frozenset[str | float]]
"""Number values by kind: a unit kind (``time``, ``count:request``, ...) or ``plain`` (unitless)."""


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
    """Cosine of the two ``name_embedding`` vectors."""
    jaccard: float
    rich_cosine: float | None = None
    """Cosine of the two candidate (rich) vectors."""
    rich_only: bool = False
    """A candidate of the rich vectors only: never merged directly."""

    @property
    def direct(self) -> bool:
        return not self.rich_only and self.cosine >= DEDUP_MERGE_COSINE and self.jaccard >= DEDUP_MERGE_TOKENS


@dataclass
class _DedupCounts:
    candidates: int = 0
    rich_only: int = 0
    merges: int = 0
    adjudications: int = 0
    guard_refusals: int = 0
    reused: int = 0
    unsure: int = 0
    expired: int = 0
    union_refused: int = 0
    collapsed: int = 0
    refused: list[RefusedDedupPair] = field(default_factory=list)
    """Pairs a pairwise guard refused, their ends by the surviving nodes' keys once clustered."""


_DEDUP_NAME: Final = "dedup"
DEDUP_PASS: Final = _DEDUP_NAME


def dedup_hash(label: str, name: str, stmt: str, quotes: Iterable[str]) -> str:
    """The evidence hash of a dedup statement (``consolidate.evidence_hash``): label, name, ``stmt`` and
    its distinct source-quote texts, sorted; a stored verdict on it expires when any of them changes."""

    return evidence_hash([label, name, stmt, sorted(set(quotes))])


@dataclass(frozen=True)
class DedupVerdicts:
    """Valid stored dedup verdicts by store key (``DedupMemory.key``) and the number that expired."""

    found: dict[str, bool]
    expired: int = 0


class DedupMemory:
    """Stored requirement dedup verdicts of one project (module docstring, step 2), each with its
    evidence hash (``pair_hash`` of the two keys with their ``dedup_hash``)."""

    def __init__(self, store: ExtractionStore, project_id: str, model: str) -> None:
        self._store = store
        self._project_id = project_id
        self.fingerprint = f"{DEDUP_PROMPT}|{model}"

    def key(self, first: str, second: str) -> str:
        low, high = sorted((first, second))
        return f"{self._project_id}|{DEDUP_PASS}|{low}|{high}"

    async def verdicts(self, pairs: Sequence[tuple[str, str]], hashes: Mapping[str, str]) -> DedupVerdicts:
        """Stored verdicts by store key (``key``) of the pairs that have a valid one (``hashes``: each
        key's ``dedup_hash``); an entry whose hash differs expired, one without a hash is a miss."""

        evidence = {self.key(a, b): pair_hash(a, hashes[a], b, hashes[b]) for a, b in pairs}
        found = await get_many(self._store, DECISION_KIND, sorted(evidence), self.fingerprint)
        result: dict[str, bool] = {}
        expired = 0
        for key, payload in found.items():
            same = payload.get("same")
            match = evidence_matches(payload, evidence[key])
            if match is False:
                expired += 1
            elif match and isinstance(same, bool):
                result[key] = same
        return DedupVerdicts(result, expired)

    async def write(self, verdicts: Mapping[tuple[str, str], SameCommitment], hashes: Mapping[str, str]) -> None:
        """Store fresh verdicts by key pair, each with its confidence and evidence hash."""

        await put_many(
            self._store,
            DECISION_KIND,
            self.fingerprint,
            {
                self.key(a, b): {
                    "same": verdict.same,
                    "confidence": verdict.confidence,
                    "evidence": pair_hash(a, hashes[a], b, hashes[b]),
                }
                for (a, b), verdict in sorted(verdicts.items())
            },
        )


def _dedup_quote(node: _Node) -> str:
    """The node's quote (its primary span's, else its first source quote), else its snapped quote
    lines, else empty."""

    quote = node.quote
    if quote.strip():
        return quote
    return node.primary.text if node.primary else ""


def _dedup_text(node: _Node) -> str:
    return f"{node.name}\n{_dedup_quote(node)}"


def dedup_candidate_text(label: str, name: str, stmt: str, quote: str) -> str:
    """The candidate (rich) embedding text of a dedup statement, ``<label>: <name>."""

    parts = [" ".join(name.split()).rstrip(" .")]
    seen = [tuple(tokens(name))]
    for part in (stmt, quote):
        folded = tuple(tokens(part))
        if not folded or folded in seen:
            continue
        seen.append(folded)
        parts.append(" ".join(part[:DEDUP_QUOTE_MAX_CHARS].split()).rstrip(" ."))
    return f"{label}: " + ". ".join(parts)


def _candidate_text(node: _Node, ontology: Ontology) -> str:
    return dedup_candidate_text(node.label, node.name, _stmt(node, ontology), _dedup_quote(node))


def _stmt(node: _Node, ontology: Ontology) -> str:
    """The node's normalised ``stmt`` attribute, else empty."""

    stmt = ontology.normalise_attribute(node.label, STMT_ATTRIBUTE, node.attributes.get(STMT_ATTRIBUTE))
    return str(stmt) if stmt else ""


def _signature(node: _Node, ids: IdFinder) -> _Signature:
    text = _dedup_text(node)
    words = set(tokens(text))
    modal = frozenset(name for name, cue in (("strong", _STRONG_MODALS), ("weak", _WEAK_MODALS)) if words & cue)
    quote, records = _record_rows(_dedup_quote(node))
    return _Signature(
        numbers=_own_numbers(node.name, quote),
        negated=_negated(node.name),
        modal=modal,
        references=frozenset(ids.all(text)) | records,
    )


def _record_rows(quote: str) -> tuple[str, frozenset[str]]:
    """``quote`` without the ISO dates of its record rows, and those rows' ids."""

    lines: list[str] = []
    records: set[str] = set()
    for line in quote.split("\n"):
        record = line.lstrip().startswith("|") and _ISO_DATE.search(line) and _RECORD_ID.search(line)
        if record:
            records.update(normalise_id(found.group(0)) for found in _RECORD_ID.finditer(line))
        lines.append(_ISO_DATE.sub(" ", line) if record else line)
    return "\n".join(lines), frozenset(records)


def _own_numbers(name: str, quote: str) -> _Values:
    """The statement's own numbers by kind: of each kind its name states, the name's values; of a kind only its quote
    states, the quote's.
    """

    named: dict[str, set[str | float]] = {}
    for kind, value in _numbers(name):
        named.setdefault(kind, set()).add(value)
    quoted: dict[str, set[str | float]] = {}
    for kind, value in _numbers(quote):
        if kind not in named:
            quoted.setdefault(kind, set()).add(value)
    return {kind: frozenset(values) for kind, values in (named | quoted).items()}


def _numbers(text: str) -> list[tuple[str, str | float]]:
    """``(kind, value)`` of every number in ``text``, read line by line: a unit number's kind and
    its value in the kind's base unit; a unitless number's kind ``plain`` and its digits."""

    found: list[tuple[str, str | float]] = []
    for line in unicodedata.normalize("NFKC", text).splitlines():
        for match in _NUMBER.finditer(line):
            digits = _NUMBER_SEPARATOR.sub("", match.group(2))
            sign, word = match.group(1), (match.group(3) or "").casefold()
            unit = (f"currency:{_CURRENCY_SIGNS[sign]}", 1.0) if sign else _UNITS.get(word)
            value = _float(digits)
            if unit is None or value is None:
                found.append((_PLAIN, digits))
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


def _refusal(first: _Signature, second: _Signature) -> DedupGuard | None:
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


@dataclass
class _Apart:
    """The pairwise dedup guards over the nodes in ``ordered`` (by position): ``_refusal`` of their
    signatures (read once, from name and quote), then ``same_sentence``."""

    ordered: Sequence[_Node]
    ids: IdFinder
    names: Mapping[str, np.ndarray]
    signatures: dict[int, _Signature] = field(default_factory=dict)

    def signature(self, index: int) -> _Signature:
        if index not in self.signatures:
            self.signatures[index] = _signature(self.ordered[index], self.ids)
        return self.signatures[index]

    def guard(self, first: int, second: int, name_cosine: float | None = None) -> DedupGuard | None:
        """The guard that refuses the pair, or None."""

        reason = _refusal(self.signature(first), self.signature(second))
        if reason is not None:
            return reason
        a, b = self.ordered[first], self.ordered[second]
        if not _same_sentence(a, b):
            return None
        if name_cosine is None:
            name_cosine = _cosine(
                self.names[embedding_text(a.label, a.name)], self.names[embedding_text(b.label, b.name)]
            )
        jaccard = token_jaccard(frozenset(tokens(a.name)), frozenset(tokens(b.name)))
        near_identical = name_cosine >= DEDUP_MERGE_COSINE and jaccard >= DEDUP_MERGE_TOKENS
        return None if near_identical else "same_sentence"


def _same_sentence(first: _Node, second: _Node) -> bool:
    """True when both nodes rest on one source sentence: their primary spans overlap in one fragment, or
    their quotes are equal (case, punctuation and spacing aside)."""

    a, b = first.primary, second.primary
    if (
        a is not None
        and b is not None
        and a.fragment.rec.fragment_id == b.fragment.rec.fragment_id
        and a.first <= b.last
        and b.first <= a.last
    ):
        return True
    quote = tuple(tokens(first.quote))
    return bool(quote) and quote == tuple(tokens(second.quote))


def _dedup_candidates(
    ordered: Sequence[_Node],
    groups: Mapping[tuple[str, str], list[int]],
    vectors: _DedupVectors,
    apart: _Apart,
    counts: _DedupCounts,
) -> tuple[list[_DedupPair], list[_DedupPair]]:
    """The candidate pairs of the dedup groups: ``(direct pairs, pairs to ask the model)``; a pair a guard
    (``_Apart``) refuses is counted and listed (``counts.refused``)."""

    direct: list[_DedupPair] = []
    asked: list[_DedupPair] = []
    for group_key in sorted(groups):
        indexes = groups[group_key]
        if len(indexes) < MIN_DEDUP_GROUP:
            continue
        for found in _group_candidates(ordered, indexes, vectors):
            first, second = found.first, found.second
            counts.candidates += 1
            counts.rich_only += int(found.rich_only)
            reason = apart.guard(first, second, found.cosine)
            if reason is not None:
                counts.guard_refusals += 1
                counts.refused.append(
                    RefusedDedupPair(
                        first_key=ordered[first].key,
                        second_key=ordered[second].key,
                        label=ordered[first].label,
                        guard=reason,
                        name_cosine=round(found.cosine, 6),
                        rich_cosine=None if found.rich_cosine is None else round(found.rich_cosine, 6),
                    )
                )
                log_event(
                    logger,
                    DEBUG,
                    "graph.conform.dedup_refused",
                    reason=reason,
                    cosine=round(found.cosine, 3),
                    rich_only=found.rich_only,
                )
                continue
            (direct if found.direct else asked).append(found)
    return direct, asked


@dataclass(frozen=True)
class _DedupVectors:
    """The dedup vectors: ``name_embedding`` vectors by their text (``embedding_text``) and candidate
    (rich) vectors by ``dedup_candidate_text``, with each node's candidate text by its position."""

    names: Mapping[str, np.ndarray]
    rich: Mapping[str, np.ndarray]
    rich_text: Mapping[int, str]


def _group_candidates(ordered: Sequence[_Node], indexes: Sequence[int], vectors: _DedupVectors) -> list[_DedupPair]:
    """The candidate pairs of one dedup group, in position order: each node's ``NEAREST_NAMES`` nearest names at cosine
    >= ``DEDUP_CANDIDATE_COSINE`` and its ``DEDUP_RICH_K`` nearest candidate vectors at cosine >= ``DEDUP_RICH_FLOOR``;
    a pair only the second gives is ``rich_only``.
    """

    names = unit_rows([vectors.names[embedding_text(ordered[i].label, ordered[i].name)] for i in indexes])
    rich = unit_rows([vectors.rich[vectors.rich_text[i]] for i in indexes])
    near_names = sorted(nearest_pairs(names, NEAREST_NAMES, DEDUP_CANDIDATE_COSINE))
    by_name = {
        pair
        for pair, cosine in zip(near_names, pair_cosines(names, near_names), strict=True)
        if cosine >= DEDUP_CANDIDATE_COSINE
    }
    near_rich = sorted(nearest_pairs(rich, DEDUP_RICH_K, DEDUP_RICH_FLOOR))
    by_rich = {
        pair
        for pair, cosine in zip(near_rich, pair_cosines(rich, near_rich), strict=True)
        if cosine >= DEDUP_RICH_FLOOR
    }
    pairs = sorted(by_name | by_rich)
    found: list[_DedupPair] = []
    for (a, b), name_cosine, rich_cosine in zip(
        pairs, pair_cosines(names, pairs), pair_cosines(rich, pairs), strict=True
    ):
        first, second = indexes[a], indexes[b]
        found.append(
            _DedupPair(
                first,
                second,
                name_cosine,
                token_jaccard(frozenset(tokens(ordered[first].name)), frozenset(tokens(ordered[second].name))),
                rich_cosine,
                rich_only=(a, b) not in by_name,
            )
        )
    return found


async def _dedup_verdicts(
    asked: Sequence[_DedupPair],
    ordered: Sequence[_Node],
    *,
    gateway: Gateway,
    ontology: Ontology,
    memory: DedupMemory | None,
    counts: _DedupCounts,
) -> dict[int, bool]:
    """The verdict on each pair in ``asked`` (by index): valid stored ones reused, the others asked of
    the model. A failed call or an unsure (``low``) verdict is no merge and is not stored."""

    keys = [(ordered[p.first].key, ordered[p.second].key) for p in asked]
    hashes = {
        node.key: _dedup_evidence(node, ontology) for p in asked for node in (ordered[p.first], ordered[p.second])
    }
    stored = await memory.verdicts(keys, hashes) if memory is not None and keys else DedupVerdicts({})
    known = {
        index: stored.found[memory.key(*keys[index])]
        for index in range(len(asked))
        if memory is not None and memory.key(*keys[index]) in stored.found
    }
    pending = [index for index in range(len(asked)) if index not in known]

    async def ask(index: int) -> tuple[SameCommitment | None, str | None]:
        pair = asked[index]
        return await _adjudicate_dedup(gateway, ontology, ordered[pair.first], ordered[pair.second])

    counts.adjudications = len(pending)
    counts.reused = len(known)
    counts.expired = stored.expired
    answers = await gather_verdicts([ask(index) for index in pending], event="graph.conform.dedup_adjudication_failed")
    verdicts = dict(known)
    fresh: dict[tuple[str, str], SameCommitment] = {}
    for index, answer in zip(pending, answers, strict=True):
        unsure = answer is not None and answer.confidence == UNSURE
        counts.unsure += int(unsure)
        verdicts[index] = answer is not None and answer.same and not unsure
        if answer is not None and not unsure:
            fresh[keys[index]] = answer
    if memory is not None:
        await memory.write(fresh, hashes)
    return verdicts


def _dedup_evidence(node: _Node, ontology: Ontology) -> str:
    """``dedup_hash`` of a node: label, name, normalised ``stmt`` and source-quote texts."""

    return dedup_hash(node.label, node.name, _stmt(node, ontology), node.quotes)


def _dedup_clusters(
    ordered: Sequence[_Node],
    confirmed: Sequence[_DedupPair],
    apart: _Apart,
    counts: _DedupCounts,
) -> list[_Node]:
    """Merge the confirmed pairs (clusters at most ``MAX_CLUSTER``, one modality) into their survivors; a union two
    members of which a pairwise guard (``_Apart``) refuses is not made (``union_refused``); the refused pairs' ends
    become their survivors' keys (``_surviving_refusals``).
    """

    parent = list(range(len(ordered)))
    members: dict[int, list[int]] = {index: [index] for index in range(len(ordered))}
    modal = {index: apart.signatures.get(index, _NO_SIGNATURE).modal for index in parent}

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
        if any(apart.guard(x, y) is not None for x in members[a] for y in members[b]):
            counts.union_refused += 1
            continue
        root, child = (a, b) if a < b else (b, a)
        parent[child] = root
        members[root].extend(members.pop(child))
        modal[root] = modal[root] | modal[child]
        counts.merges += 1

    result: list[_Node] = []
    owner: dict[str, str] = {}
    for root in sorted(members):
        survivor, *others = (ordered[index] for index in sorted(members[root]))
        for other in others:
            owner[other.key] = survivor.key
            survivor.aliases.add(other.key)
            _absorb(survivor, other, keep_primary=True)
        result.append(survivor)
    counts.refused = _surviving_refusals(counts.refused, owner)
    counts.collapsed = sum(1 for pair in counts.refused if pair.collapsed)
    return result


def _surviving_refusals(refused: Iterable[RefusedDedupPair], owner: Mapping[str, str]) -> list[RefusedDedupPair]:
    """The refused pairs with their ends' survivors' keys (``owner``: merged key to survivor key), lower key first; a
    pair whose ends ended in one node anyway keeps its own keys (lower first) and is marked ``collapsed``.
    """

    kept: dict[tuple[str, str, str], RefusedDedupPair] = {}
    for pair in refused:
        first, second = sorted(owner.get(key, key) for key in (pair.first_key, pair.second_key))
        update: dict[str, object] = {"first_key": first, "second_key": second}
        if first == second:
            first, second = sorted((pair.first_key, pair.second_key))
            update = {"first_key": first, "second_key": second, "collapsed": True}
        known = kept.get((first, second, pair.guard))
        if known is None or pair.name_cosine > known.name_cosine:
            kept[(first, second, pair.guard)] = pair.model_copy(update=update)
    return [kept[key] for key in sorted(kept)]


def _listed_refusals(refused: Iterable[RefusedDedupPair], nodes: Mapping[str, _Node]) -> tuple[RefusedDedupPair, ...]:
    """The refused pairs whose ends are (or, collapsed, are aliases of) nodes normalisation kept; a pair
    with an end it dropped is lost and leaves the list."""

    owner = {alias: key for key, node in nodes.items() for alias in node.aliases}
    return tuple(
        pair
        for pair in refused
        if all(key in nodes or owner.get(key) in nodes for key in (pair.first_key, pair.second_key))
    )


async def _dedup(
    nodes: Sequence[_Node],
    vectors: Mapping[str, np.ndarray],
    *,
    cache: VectorCache,
    gateway: Gateway,
    ontology: Ontology,
    ids: IdFinder,
    memory: DedupMemory | None = None,
) -> tuple[list[_Node], _DedupCounts]:
    """Merge near-identical restatements among id-less requirements; ``vectors``: the ``name_embedding`` vectors by
    text; the candidate (rich) vectors come from ``cache``; ``memory`` holds the verdicts of earlier builds (None: none
    are kept).
    """

    counts = _DedupCounts()
    ordered = sorted(nodes, key=lambda node: node.key)
    groups: dict[tuple[str, str], list[int]] = {}
    for index, node in enumerate(ordered):
        if _dedup_eligible(node, ontology):
            scope = ontology.identity_rule(node.label).scope
            groups.setdefault((scope, node.label), []).append(index)
    rich_text = {
        index: _candidate_text(ordered[index], ontology)
        for indexes in groups.values()
        if len(indexes) >= MIN_DEDUP_GROUP
        for index in indexes
    }
    rich = await cache.mapping(rich_text.values())

    apart = _Apart(ordered, ids, vectors)
    direct, asked = _dedup_candidates(ordered, groups, _DedupVectors(vectors, rich, rich_text), apart, counts)
    verdicts = await _dedup_verdicts(asked, ordered, gateway=gateway, ontology=ontology, memory=memory, counts=counts)
    confirmed = direct + [p for index, p in enumerate(asked) if verdicts[index]]
    confirmed.sort(key=lambda p: (not p.direct, -p.cosine, -p.jaccard, p.first, p.second))
    return _dedup_clusters(ordered, confirmed, apart, counts), counts


async def _adjudicate_dedup(
    gateway: Gateway, ontology: Ontology, first: _Node, second: _Node
) -> tuple[SameCommitment | None, str | None]:
    """The small model's verdict on one pair; a failed call is none (with its error code)."""

    messages = dedup_messages(
        ontology, gateway, first.label, [(node.name, _dedup_quote(node)) for node in (first, second)]
    )
    try:
        verdict = await gateway.structured("small", None, messages, SameCommitment, prompt=DEDUP_PROMPT)
    except LLMFatalError:
        raise
    except LLMError as error:
        return None, error.code
    return verdict, None


def dedup_messages(
    ontology: Ontology, gateway: Gateway, label: str, statements: Sequence[tuple[str, str]]
) -> list[dict[str, str]]:
    """Messages of one ``dedup_adjudicate.3`` call over two ``(name, quote)`` statements of ``label``."""

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
                "strength). Give your confidence: high when the statements leave no doubt, medium "
                "when the answer is likely but rests on reading between the lines, low when you "
                "suspect an answer but the statements cannot confirm it."
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
    fallback: bool = False
    """A ``derive`` draft whose target the whole-word fallback found (``_contained_target``)."""
    raw: int | None = None
    """A stated draft's index in ``EdgesArtifact.raw_edges`` (``_foreign_edges``); None for a derived one."""


@dataclass
class _EdgeAcc:
    relation: str
    source: str
    target: str
    basis: Basis
    fact: str | None
    fragment_ids: set[str]
    fallback: bool = False
    """Every draft of the edge is a derive-fallback draft (``TargetGraph.derive_fallback_edges``)."""


@dataclass
class _Quarantine:
    """Edges Conform cannot keep, one entry per reason, ends and relation: repeated drafts join their evidence fragments
    and the strongest basis wins, as kept edges merge.
    """

    edges: dict[tuple[QuarantineReason, str, str, str], tuple[Basis, set[str]]] = field(default_factory=dict)

    def add(
        self,
        reason: QuarantineReason,
        relation: str,
        ends: tuple[str, str],
        basis: Basis,
        fragment_ids: Iterable[str],
    ) -> None:
        source, target = ends
        key = (reason, source, relation, target)
        known = self.edges.get(key)
        if known is None:
            self.edges[key] = (basis, set(fragment_ids))
            return
        fragments = known[1] | set(fragment_ids)
        self.edges[key] = (basis if BASIS_RANK[basis] < BASIS_RANK[known[0]] else known[0], fragments)

    def entries(self, nodes: Mapping[str, _Node], corpus: _Corpus) -> tuple[QuarantinedTargetEdge, ...]:
        """The entries ordered by reason, then source key, relation and target key."""

        return tuple(
            QuarantinedTargetEdge(
                reason=reason,
                relation=relation,
                source_label=nodes[source].label,
                target_label=nodes[target].label,
                basis=basis,
                fragment_ids=tuple(corpus.ordered(fragments)),
                source_key=source,
                target_key=target,
            )
            for (reason, source, relation, target), (basis, fragments) in sorted(self.edges.items())
        )


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
                raw=index,
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


_HubForms = Sequence[tuple[_Node, tuple[tuple[str, ...], ...]]]
"""Hub nodes with the word forms of their name and ``surface`` keys, in ``hub_surface`` form (no leading
article, plurals folded)."""


def _hub_forms(nodes: Sequence[_Node], ontology: Ontology) -> _HubForms:
    hubs = ontology.hub_labels
    index: list[tuple[_Node, tuple[tuple[str, ...], ...]]] = []
    for node in nodes:
        if node.label not in hubs:
            continue
        forms = {tuple(hub_value_words(hub_surface(node.name)))}
        for key in (node.key, *node.aliases):
            parts = key.split("|", 2)
            if len(parts) == KEY_FIELDS and parts[1] == "surface":
                forms.add(tuple(hub_value_words(parts[2])))
        forms.discard(())
        index.append((node, tuple(sorted(forms))))
    return index


def _contained_target(
    value: str, node: _Node, targets: frozenset[str], index: _DeriveIndex
) -> tuple[_Node | None, bool]:
    """The one hub of a ``targets`` label named in ``node``'s fragments (``_DeriveIndex.grounded``) whose name starts
    with the words of reference ``value``, else the one whose name holds them as whole words, both in ``hub_surface``
    form (a leading article dropped); None when none does or several of one tier do.
    """

    words = tuple(hub_value_words(hub_surface(value)))
    if not words:
        return None, False
    size = len(words)
    starts: dict[str, _Node] = {}
    holds: dict[str, _Node] = {}
    for candidate, forms in index.hubs:
        if candidate is node or candidate.label not in targets:
            continue
        if any(form[:size] == words for form in forms):
            starts[candidate.key] = candidate
        elif any(form[at : at + size] == words for form in forms for at in range(1, len(form) - size + 1)):
            holds[candidate.key] = candidate
    passed = False
    for tier in (starts, holds):
        found = [candidate for candidate in tier.values() if index.grounded(candidate, node)]
        passed = passed or len(found) < len(tier)
        if found:
            return (found[0], False) if len(found) == 1 else (None, False)
    return None, passed


@dataclass(frozen=True)
class _HubNames:
    """The hub names of one build (``_hub_index``), counted once over the corpus: per hub key, the
    fragments whose text names it (``_named_in``), and per fragment, the hub keys its heading path
    names."""

    index: NameIndex
    named: dict[str, dict[str, int]]
    headings: dict[str, frozenset[str]]


@dataclass(frozen=True)
class _DeriveIndex:
    """What ``_derived_drafts`` resolves references against: the nodes by surface form and by explicit
    id (``_surface_index``), the hub word forms (``_hub_forms``) and the hub names (``_HubNames``)."""

    by_surface: Mapping[str, Sequence[_Node]]
    by_id: Mapping[str, _Node]
    hubs: _HubForms
    names: _HubNames

    def grounded(self, target: _Node, node: _Node) -> bool:
        """True when hub ``target`` is named in the requirement ``node``'s evidence fragments or their
        heading path (a heading that is a part's own name counts)."""

        fragments = node.fragment_ids
        named = self.names.named.get(target.key, {})
        return not fragments.isdisjoint(named) or any(
            target.key in self.names.headings.get(fragment_id, frozenset()) for fragment_id in fragments
        )


def _derived_drafts(node: _Node, ontology: Ontology, index: _DeriveIndex) -> tuple[list[_EdgeDraft], int, int]:
    """The edges the ontology's derive rules give ``node`` (the attribute value names the target: by name or id, else by
    the whole-word fallback ``_contained_target``, which is skipped when the value is the exact name of a hub of any
    label, and takes only hubs named in ``node``'s evidence fragments or their heading path), how many references the
    fallback resolved and how many it left unresolved after passing over a hub not so named.
    """

    by_surface, by_id = index.by_surface, index.by_id
    drafts: list[_EdgeDraft] = []
    fallbacks = 0
    refused = 0
    hub_labels = ontology.hub_labels
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
        named_hub = any(found.label in hub_labels for name in names for found in by_surface.get(name, []))
        contained, passed = (
            (None, False) if matches or named_hub else _contained_target(value, node, rule.target_labels, index)
        )
        if contained is not None:
            matches = [contained]
            fallbacks += 1
        elif passed:
            refused += 1
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
                    fallback=contained is not None,
                )
            )
    return drafts, fallbacks, refused


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
    texts = [node.primary.text if node.primary else node.quote]
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


def _implied_edges(
    nodes: Sequence[_Node], ontology: Ontology, ids: IdFinder, names: _HubNames
) -> tuple[list[_EdgeDraft], list[_EdgeDraft], int, int]:
    """The ``derive`` drafts (legalised with the stated edges), the ``id_references`` drafts (normalisation stage 8 adds
    them, across owners only), the references the derive fallback resolved and those it left unresolved after passing
    over a hub not named in the source's evidence fragments or their heading path (``names``).
    """

    by_surface, by_id = _surface_index(nodes, ontology)
    index = _DeriveIndex(by_surface, by_id, _hub_forms(nodes, ontology), names)
    derived: list[_EdgeDraft] = []
    references: list[_EdgeDraft] = []
    fallbacks = 0
    refused = 0
    for node in nodes:
        drafts, resolved, ungrounded = _derived_drafts(node, ontology, index)
        derived.extend(drafts)
        fallbacks += resolved
        refused += ungrounded
        references.extend(_id_reference_drafts(node, ontology, ids, by_id))
    return derived, references, fallbacks, refused


def _text(value: AttributeValue) -> str:
    return value if isinstance(value, str) else ""


@dataclass
class _Legal:
    """Legalisation's outcome (``_legalise``): the legal edges merged by edge key, the drafts flipped, the ``derive``
    drafts suppressed by a stated edge, and per stated draft (by raw edge index) what became of it: the key of the edge
    it joined, ``quarantined:<reason>`` or ``dropped`` (a self-loop).
    """

    edges: dict[str, _EdgeAcc] = field(default_factory=dict)
    flipped: int = 0
    suppressed: int = 0
    fates: dict[int, str] = field(default_factory=dict)


def _owning(
    relation: str, source: str, target: str, views: Mapping[str, NodeView], ontology: Ontology
) -> tuple[str, ...] | None:
    """The conflict group of the legal ``relation`` edge ``source`` to ``target``, as normalisation's stage 1 forms
    them: its source and relation on an ``owner`` pair (every owner pair of the source, for one relation), or its source
    and pair on an ``exclusive`` one; None on any other pair.
    """

    pair = ontology.pair_rule(relation, views[source].label, views[target].label)
    if pair is not None and pair.owner:
        return (source, relation)
    if pair is not None and pair.exclusive:
        return (source, relation, pair.source, pair.target)
    return None


def _legalise(
    stated: Iterable[_EdgeDraft],
    derived: Iterable[_EdgeDraft],
    *,
    views: Mapping[str, NodeView],
    ontology: Ontology,
    counters: dict[str, Counter[str]],
    quarantine: _Quarantine,
    local: Callable[[_EdgeAcc], bool],
) -> _Legal:
    """Legal edges merged by edge key (``_Legal``), in two phases: the ``stated`` drafts, then the ``derived`` ones."""

    legal = _Legal()
    owned: set[tuple[str, ...]] = set()
    for draft in stated:
        fate = _legal_one(draft, views=views, ontology=ontology, counters=counters, quarantine=quarantine, legal=legal)
        if draft.raw is not None:
            legal.fates[draft.raw] = fate
        edge = legal.edges.get(fate)
        group = (
            _owning(edge.relation, edge.source, edge.target, views, ontology)
            if edge is not None and local(edge)
            else None
        )
        if group is not None:
            owned.add(group)
    for draft in derived:
        _legal_one(
            draft, views=views, ontology=ontology, counters=counters, quarantine=quarantine, legal=legal, owned=owned
        )
    return legal


def _legal_one(
    draft: _EdgeDraft,
    *,
    views: Mapping[str, NodeView],
    ontology: Ontology,
    counters: dict[str, Counter[str]],
    quarantine: _Quarantine,
    legal: _Legal,
    owned: Collection[tuple[str, ...]] = (),
) -> str:
    """Legalise one draft into ``legal`` (``_legalise``); its fate: the edge key it joined,
    ``quarantined:<reason>``, ``dropped`` (a self-loop) or ``suppressed`` (``owned``: the conflict
    groups, ``_owning``, a kept stated edge already holds)."""

    outcome = ontology.legalise(draft.relation, views[draft.source], views[draft.target], draft.evidence_lines)
    if isinstance(outcome, Dropped):
        counters.setdefault(draft.relation, Counter())[outcome.reason] += 1
        ends = (draft.source, draft.target)
        if outcome.reason != "unknown_relation" and ontology.is_symmetric(draft.relation):
            ends = (min(ends), max(ends))
        quarantine.add(outcome.reason, draft.relation, ends, draft.basis, draft.fragment_ids)
        return f"quarantined:{outcome.reason}"
    relation = outcome.relation
    source_key, target_key = draft.source, draft.target
    if outcome.flipped:
        source_key, target_key = target_key, source_key
    if source_key == target_key:
        legal.flipped += int(outcome.flipped)
        counters.setdefault(draft.relation, Counter())["self_loop"] += 1
        return "dropped"
    if ontology.is_symmetric(relation) and target_key < source_key:
        source_key, target_key = target_key, source_key
    edge_key = f"{source_key}|{relation}|{target_key}"
    if owned and edge_key not in legal.edges and _owning(relation, source_key, target_key, views, ontology) in owned:
        legal.suppressed += 1
        return "suppressed"
    legal.flipped += int(outcome.flipped)
    existing = legal.edges.get(edge_key)
    if existing is None:
        legal.edges[edge_key] = _EdgeAcc(
            relation,
            source_key,
            target_key,
            draft.basis,
            draft.fact,
            set(draft.fragment_ids),
            draft.fallback,
        )
        return edge_key
    existing.fallback = existing.fallback and draft.fallback
    existing.fragment_ids |= set(draft.fragment_ids)
    if BASIS_RANK[draft.basis] < BASIS_RANK[existing.basis]:
        existing.basis = draft.basis
        existing.fact = draft.fact or existing.fact
    elif existing.fact is None:
        existing.fact = draft.fact
    return edge_key


def _local(edge: _EdgeAcc | NormEdge, nodes: Mapping[str, _Node], corpus: _Corpus, locality: str) -> bool:
    if locality != "strict":
        return True
    units = [
        {unit for fid in nodes[key].fragment_ids for unit in corpus.units_of.get(fid, [])}
        for key in (edge.source, edge.target)
    ]
    return bool(units[0] & units[1])


def _view(node: _Node, ontology: Ontology) -> NodeView:
    """The node's label and normalised attribute values, a null value read as its declared ``default``
    (normalisation and the choice between defaults read these; defaults are also stored)."""

    values: dict[str, object] = {}
    for name, spec in ontology.attribute_specs(node.label).items():
        value = ontology.normalise_attribute(node.label, name, node.attributes.get(name))
        if value is None:
            value = spec.default
        if value is not None:
            values[name] = value
    return NodeView(node.label, values)


def _judge_statements(node: _Node, ontology: Ontology) -> tuple[str, ...]:
    """What a conform judge payload shows of a node: its snapped spans (document order), else its
    ``stmt``."""

    texts = [
        span.text[:STATEMENT_MAX_CHARS]
        for span in sorted(node.spans, key=lambda span: (span.fragment.rank, span.first))
    ]
    texts = [text for text in dict.fromkeys(texts) if text]
    if not texts:
        stmt = ontology.normalise_attribute(node.label, STMT_ATTRIBUTE, node.attributes.get(STMT_ATTRIBUTE))
        texts = [stmt] if stmt else []
    return tuple(texts)


def _norm_edge(draft: _EdgeAcc | _EdgeDraft) -> NormEdge:
    return NormEdge(draft.relation, draft.source, draft.target, draft.basis, draft.fact, set(draft.fragment_ids))


@dataclass(frozen=True, slots=True)
class ConformJudging:
    """What Conform's judges reuse and who judges owner conflicts."""

    dedup_memory: DedupMemory | None = None
    judge_memory: ConformJudgeMemory | None = None
    judge: OwnerJudge | None = None
    answered_memory: AnsweredMemory | None = None
    """Earlier answered-elsewhere verdicts (``graph.build.answered``)."""


@dataclass
class _Locality:
    """Conform's locality (``_local``) over the kept edges and the edges normalisation derives: an edge
    it refuses is counted in ``dropped`` (an edge key once) and quarantined ``locality``."""

    nodes: Mapping[str, _Node]
    corpus: _Corpus
    locality: str
    dropped: dict[str, Counter[str]]
    quarantine: _Quarantine
    distant: set[str] = field(default_factory=set)

    def local(self, edge: _EdgeAcc) -> bool:
        """Whether locality keeps ``edge`` (nothing is counted or quarantined)."""

        return _local(edge, self.nodes, self.corpus, self.locality)

    def keep(self, accumulated: Iterable[_EdgeAcc]) -> list[_EdgeAcc]:
        kept: list[_EdgeAcc] = []
        for edge in accumulated:
            if _local(edge, self.nodes, self.corpus, self.locality):
                kept.append(edge)
            else:
                self.dropped.setdefault(edge.relation, Counter())["locality"] += 1
                self.quarantine.add(
                    "locality", edge.relation, (edge.source, edge.target), edge.basis, edge.fragment_ids
                )
                self.distant.add(f"{edge.source}|{edge.relation}|{edge.target}")
        return kept

    def admit(self, edge: NormEdge) -> bool:
        """Locality for the edges normalisation derives (an edge key is counted once)."""

        if _local(edge, self.nodes, self.corpus, self.locality):
            return True
        if edge.key not in self.distant:
            self.dropped.setdefault(edge.relation, Counter())["locality"] += 1
            self.distant.add(edge.key)
        self.quarantine.add("locality", edge.relation, (edge.source, edge.target), edge.basis, edge.fragment_ids)
        return False


def _add_normalised(normalised: Normalised, dropped: dict[str, Counter[str]], quarantine: _Quarantine) -> None:
    """Add normalisation's dropped-edge counts and quarantined edges to Conform's."""

    for relation, reasons in normalised.counted.items():
        dropped.setdefault(relation, Counter()).update(reasons)
    for entry in normalised.quarantine:
        quarantine.add(entry.reason, entry.relation, (entry.source, entry.target), entry.basis, entry.fragment_ids)


def _target_edges(normalised: Normalised, corpus: _Corpus) -> tuple[TargetEdge, ...]:
    return tuple(
        TargetEdge(
            edge_key=f"{edge.source}|{edge.relation}|{edge.target}",
            relation=edge.relation,
            source_key=edge.source,
            target_key=edge.target,
            basis=edge.basis,
            fact=edge.fact,
            fragment_ids=tuple(corpus.ordered(edge.fragment_ids)),
        )
        for edge in normalised.edges
    )


def _embedding_texts(nodes: Mapping[str, _Node], ontology: Ontology) -> dict[str, tuple[str, str]]:
    """``(search text, name text)`` by node key; the search text adds the node's statement when it has one."""

    texts: dict[str, tuple[str, str]] = {}
    for node in nodes.values():
        stmt = ontology.normalise_attribute(node.label, STMT_ATTRIBUTE, node.attributes.get(STMT_ATTRIBUTE))
        search = f"{embedding_text(node.label, node.name)}. {stmt}" if stmt else embedding_text(node.label, node.name)
        texts[node.key] = (search, embedding_text(node.label, node.name))
    return texts


async def _answered(
    nodes: dict[str, _Node],
    stated: list[_EdgeDraft],
    *,
    corpus: _Corpus,
    services: StepServices,
    vectors: VectorCache,
    memory: AnsweredMemory | None,
) -> tuple[dict[str, _Node], list[_EdgeDraft], AnsweredCheck | None]:
    """The answered-elsewhere check (``graph.build.answered``) over the nodes of the ontology's
    ``conform.answered_elsewhere`` label, and the nodes and stated drafts without those it drops
    (``_without_answered``); the check is None when the ontology has no such flag."""

    conform_spec = services.ontology.conform_spec()
    spec = conform_spec.answered_elsewhere if conform_spec is not None else None
    if spec is None:
        return nodes, stated, None
    texts = _embedding_texts({key: node for key, node in nodes.items() if node.label == spec.label}, services.ontology)
    questions = [
        Question(
            key=key,
            name=nodes[key].name,
            quote=_dedup_quote(nodes[key]),
            text=search,
            fragments=frozenset(nodes[key].fragment_ids),
        )
        for key, (search, _) in texts.items()
    ]
    pool = [fragment.rec for fid in corpus.ordered(corpus.fragments) if (fragment := corpus.groundable(fid))]
    check = await answered_elsewhere(
        questions,
        passages_of(pool) if questions else [],
        spec=spec,
        gateway=services.gateway,
        ontology=services.ontology,
        vectors=vectors,
        memory=memory,
    )
    return (*_without_answered(nodes, stated, check), check)


def _without_answered(
    nodes: dict[str, _Node], stated: list[_EdgeDraft], answered: AnsweredCheck
) -> tuple[dict[str, _Node], list[_EdgeDraft]]:
    """``nodes`` without those ``answered`` drops, and the ``stated`` drafts between the rest."""

    if not answered.dropped:
        return nodes, stated
    kept = {key: node for key, node in nodes.items() if key not in answered.keys}
    return kept, [draft for draft in stated if draft.source in kept and draft.target in kept]


CATALOG_PATHS: Final = ("mentioned", "heading", "near", "referenced")
"""The entry paths of a foreign catalog entry (``CatalogParts``), in report order."""


def _foreign_edges(
    edges: EdgesArtifact,
    *,
    legal: _Legal,
    distant: Collection[str],
    normalised: Normalised,
    views: Mapping[str, NodeView],
    ontology: Ontology,
) -> dict[str, dict[str, int]] | None:
    """Outcomes of the raw edges with an end from another unit's catalog part, per entry path of that end (an edge with
    two foreign ends counts once under each of their paths) and outcome (``_foreign_outcome``); None when the artifact
    records no catalog parts (built before entry paths were recorded, or one call per unit).
    """

    if not edges.catalog_parts:
        return None
    final = _Final(normalised, views, ontology)
    counts: dict[str, Counter[str]] = {}
    for index, raw in enumerate(edges.raw_edges):
        parts = edges.catalog_parts.get(raw.unit_id)
        if parts is None:
            continue
        paths = [path for path in CATALOG_PATHS if {raw.source_cid, raw.target_cid} & set(getattr(parts, path))]
        if not paths:
            continue
        outcome = _foreign_outcome(legal.fates.get(index), legal, distant, final)
        for path in paths:
            counts.setdefault(path, Counter())[outcome] += 1
    return {path: dict(sorted(counts[path].items())) for path in CATALOG_PATHS if path in counts}


class _Final:
    """The target edges after normalisation, indexed for ``_foreign_outcome``: their keys, by source
    with their pairs, their unordered ends; and normalisation's first quarantine reason per relation
    and unordered ends."""

    def __init__(self, normalised: Normalised, views: Mapping[str, NodeView], ontology: Ontology) -> None:
        self.views = views
        self.ontology = ontology
        self.keys = frozenset(edge.key for edge in normalised.edges)
        self.ends = frozenset(frozenset((edge.source, edge.target)) for edge in normalised.edges)
        self.by_source: dict[str, list[tuple[NormEdge, Pair | None]]] = {}
        for edge in normalised.edges:
            pair = ontology.pair_rule(edge.relation, views[edge.source].label, views[edge.target].label)
            self.by_source.setdefault(edge.source, []).append((edge, pair))
        self.quarantined: dict[tuple[str, frozenset[str]], str] = {}
        for entry in sorted(normalised.quarantine, key=lambda item: item.reason):
            self.quarantined.setdefault((entry.relation, frozenset((entry.source, entry.target))), entry.reason)

    def lost_conflict(self, edge: _EdgeAcc) -> bool:
        """True when ``edge`` is on an owner or exclusive pair and its source kept another target on
        such a pair (of the same relation, for an exclusive pair)."""

        pair = self.ontology.pair_rule(edge.relation, self.views[edge.source].label, self.views[edge.target].label)
        if pair is None or not (pair.owner or pair.exclusive):
            return False
        return any(
            other.target != edge.target
            and rival is not None
            and (
                (pair.owner and rival.owner) or (pair.exclusive and rival.exclusive and other.relation == edge.relation)
            )
            for other, rival in self.by_source.get(edge.source, ())
        )


def _foreign_outcome(fate: str | None, legal: _Legal, distant: Collection[str], final: _Final) -> str:
    """One raw edge's outcome (``FOREIGN_EDGE_OUTCOMES``) from its legalisation ``fate``: quarantined there or by
    locality (``quarantined:<reason>``); ``kept`` when its edge is a target edge; ``owner_conflict`` when it lost an
    owner or exclusive conflict (``_Final.lost_conflict``); ``quarantined:<reason>`` when normalisation quarantined an
    edge of its relation between its ends; ``kept`` when another relation between its ends is a target edge (a
    condition's ``otherwise``); else ``dropped`` (a self-loop, a node answered elsewhere, redundant or unsupported).
    """

    edge = legal.edges.get(fate) if fate is not None else None
    if fate is None or edge is None:
        return fate if fate is not None and fate.startswith("quarantined:") else "dropped"
    if fate in distant or fate in final.keys:
        return "quarantined:locality" if fate in distant else "kept"
    if final.lost_conflict(edge):
        return "owner_conflict"
    ends = frozenset((edge.source, edge.target))
    reason = final.quarantined.get((edge.relation, ends))
    if reason is not None:
        return f"quarantined:{reason}"
    return "kept" if ends in final.ends else "dropped"


# Conform


async def conform(
    plan: PlanArtifact,
    resolved: ResolveArtifact,
    edges: EdgesArtifact,
    *,
    services: StepServices,
    vectors: VectorCache,
    at: str | None = None,
    persisted: Mapping[str, PersistedIdentity] | None = None,
    judging: ConformJudging | None = None,
) -> TargetGraph:
    """The target graph of one build."""

    gateway, ontology, options = services.gateway, services.ontology, services.options
    judging = judging or ConformJudging()
    stamp = at or datetime.now(UTC).isoformat(timespec="seconds")
    corpus = _Corpus(plan)
    ids = IdFinder(ontology)

    # 1-2. Grounding, final keys, merge of equal (alike) keys; requirement dedup
    hubs = ontology.hub_labels
    grounder = _Grounder(corpus, ids, frozenset(hubs))
    grounded = [
        _ground_node(node, edges.attributes.get(node.cid, {}), grounder, node.quotes) for node in resolved.canonical
    ]
    quote_fallbacks = sum(1 for node in grounded if node.primary is None)
    keyed = [_final_key(node, ontology, ids, frozenset(persisted or {})) for node in grounded]
    grounding = _grounding_stats(grounded, keyed, corpus, sources=True)
    fresh = _fresh_members(grounded, corpus, plan)
    labels_kept = _keep_persisted_labels(grounded, persisted or {}, ontology)
    name_vectors = await vectors.mapping(
        [node.canonical.name_embedding_text for node in grounded if node.canonical.name_embedding_text is not None]
        + [embedding_text(node.label, node.name) for node in grounded if _dedup_eligible(node, ontology)]
    )
    merged, span_merges, span_splits = _merge_equal_keys(grounded, name_vectors)
    merged, dedup = await _dedup(
        merged,
        name_vectors,
        cache=vectors,
        gateway=gateway,
        ontology=ontology,
        ids=ids,
        memory=judging.dedup_memory,
    )
    dirty_wins = _prefer_fresh_values(merged, fresh, edges.attributes, ontology, persisted or {})
    # Split keys and dedup aliases can name a persisted node only now.
    labels_kept += _keep_persisted_labels(merged, persisted or {}, ontology)
    if labels_kept:
        log_event(logger, INFO, "graph.conform.labels_kept", nodes=labels_kept)
    nodes = {node.key: node for node in sorted(merged, key=lambda node: node.key)}
    key_of = {cid: node.key for node in nodes.values() for cid in node.cids}
    stated, fact_fallbacks = _stated_edges(edges, key_of, corpus)
    # 2b. Answered elsewhere: a dropped node leaves with its edges.
    nodes, stated, answered = await _answered(
        nodes, stated, corpus=corpus, services=services, vectors=vectors, memory=judging.answered_memory
    )

    # 3-5. Implied edges, legalisation, merge, locality
    dropped: dict[str, Counter[str]] = {}
    quarantine = _Quarantine()
    views = {key: _view(node, ontology) for key, node in nodes.items()}
    # Hub names are counted once, over the nodes before normalisation drops any (stage 7).
    names = _hub_names(nodes.values(), hubs, persisted or {}, corpus)
    derived, references, derive_fallbacks, derive_refused = _implied_edges(list(nodes.values()), ontology, ids, names)
    locality = _Locality(nodes, corpus, options.locality, dropped, quarantine)
    legal = _legalise(
        stated,
        derived,
        views=views,
        ontology=ontology,
        counters=dropped,
        quarantine=quarantine,
        local=locality.local,
    )

    # 6. Normalisation
    judge = judging.judge
    if judge is None:
        judge = ConformJudge(gateway, ontology, judging.judge_memory) if ontology.conform_spec() else FallbackJudge()
    normalised = await normalise(
        {
            key: NormNode(key, node.label, node.name, views[key].attributes, _judge_statements(node, ontology))
            for key, node in nodes.items()
        },
        [
            _norm_edge(edge)
            for edge in sorted(
                locality.keep(legal.edges.values()), key=lambda edge: f"{edge.source}|{edge.relation}|{edge.target}"
            )
        ],
        ontology,
        judge=judge,
        texts={
            fragment_id: fragment.rec.text
            for fragment_id in corpus.ordered(corpus.fragments)
            if (fragment := corpus.groundable(fragment_id)) is not None
        },
        derived=[_norm_edge(draft) for draft in references],
        admit=locality.admit,
    )
    _add_normalised(normalised, dropped, quarantine)
    every_node = nodes
    nodes = {key: node for key, node in nodes.items() if key not in normalised.dropped_nodes}
    target_edges = _target_edges(normalised, corpus)

    # 7. Statements, mentions, attributes, embeddings
    mentions = _mentions(names.named, nodes)
    texts = _embedding_texts(nodes, ontology)
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
            quote=ontology.normalise_attribute(
                node.label, QUOTE_ATTRIBUTE, _quote_sentences(node.primary, node.needles)
            )
            if node.primary
            else None,
            evidence=tuple(
                sorted(
                    node.evidence,
                    key=lambda e: (corpus.fragments[e.fragment_id].rank, e.line_start),
                )
            ),
            statements=_statements(
                node, corpus, stamp, names.named.get(node.key, {}) if node.label in hubs else None, names.index
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
        flipped_edges=legal.flipped,
        implied_edges=sum(1 for edge in target_edges if edge.basis == "implied"),
        derive_fallback_matches=derive_fallbacks,
        derive_fallback_refused=derive_refused,
        exclusive_conflicts=normalised.counters.exclusive_conflicts
        if normalised.resolved
        else _exclusive_conflicts(target_edges, nodes, ontology),
        quote_fallbacks=quote_fallbacks,
        fact_fallbacks=fact_fallbacks,
        span_merges=span_merges,
        span_splits=span_splits,
        dedup_candidates=dedup.candidates,
        dedup_candidates_rich_only=dedup.rich_only,
        dedup_merges=dedup.merges,
        dedup_adjudications=dedup.adjudications,
        dedup_guard_refusals=dedup.guard_refusals,
        dedup_verdicts_reused=dedup.reused,
        dedup_unsure=dedup.unsure,
        dedup_verdicts_expired=dedup.expired,
        dedup_union_refused=dedup.union_refused,
        dedup_refused_collapsed=dedup.collapsed,
        owner_conflicts=normalised.counters.owner_conflicts,
        owner_conflicts_judged=normalised.counters.owner_conflicts_judged,
        redundant_edges=normalised.counters.redundant_edges,
        cycles_broken=normalised.counters.cycles_broken,
        unsupported_nodes=normalised.counters.unsupported_nodes,
        derived_edges=normalised.counters.derived_edges,
        conform_judge_calls=normalised.counters.judge_calls,
        conform_judge_reused=normalised.counters.judge_reused,
        conform_judge_failed=normalised.counters.judge_failed,
        conform_judge_below_floor=normalised.counters.judge_below_floor,
        conform_judge_over_cap=normalised.counters.judge_over_cap,
        conform_judge_invalid_quotes=normalised.counters.judge_invalid_quotes,
        in_scope_disagreements=_exclude_disagreements(every_node.values(), edges.attributes, ontology),
        in_scope_dirty_wins=dirty_wins,
        unsnapped_quote_nodes=grounding.unsnapped_quote_nodes,
        unsnapped_source_quotes=grounding.unsnapped_source_quotes or 0,
        provisional_keys_left=grounding.provisional_keys_left,
        attributed_fragments_dropped=grounding.attributed_fragments_dropped,
        nodes_missing_source_documents=grounding.nodes_missing_source_documents,
        derive_suppressed_by_edge=legal.suppressed,
        foreign_edges=_foreign_edges(
            edges, legal=legal, distant=locality.distant, normalised=normalised, views=views, ontology=ontology
        ),
        oq_answer_judged=answered.judged if answered is not None else None,
        oq_partly_kept=answered.partly if answered is not None else None,
        oq_answered_dropped=len(answered.dropped) if answered is not None else None,
    )
    fallback_keys = {key for key, edge in legal.edges.items() if edge.fallback}
    graph = TargetGraph(
        nodes=target_nodes,
        edges=target_edges,
        counters=counters,
        quarantine=quarantine.entries(every_node, corpus),
        dedup_refused=_listed_refusals(dedup.refused, nodes),
        derive_fallback_edges=tuple(edge.edge_key for edge in target_edges if edge.edge_key in fallback_keys),
        answered_elsewhere=answered.dropped if answered is not None else (),
    )

    # 8. Invariants
    check_invariants(graph, plan, ontology)
    log_event(
        logger,
        INFO,
        "graph.conform.done",
        nodes=len(target_nodes),
        edges=len(target_edges),
        dropped=sum(sum(reasons.values()) for reasons in counters.dropped_edges.values()),
        quarantined=len(graph.quarantine),
        flipped=counters.flipped_edges,
        implied=counters.implied_edges,
        derive_fallback_refused=counters.derive_fallback_refused,
        derive_suppressed_by_edge=counters.derive_suppressed_by_edge,
        foreign_edges=sum(sum(outcomes.values()) for outcomes in (counters.foreign_edges or {}).values()),
        oq_answered_dropped=counters.oq_answered_dropped,
        exclusive_conflicts=counters.exclusive_conflicts,
        quote_fallbacks=counters.quote_fallbacks,
        fact_fallbacks=counters.fact_fallbacks,
        span_merges=counters.span_merges,
        span_splits=counters.span_splits,
        dedup_candidates=counters.dedup_candidates,
        dedup_candidates_rich_only=counters.dedup_candidates_rich_only,
        dedup_merges=counters.dedup_merges,
        dedup_adjudications=counters.dedup_adjudications,
        dedup_guard_refusals=counters.dedup_guard_refusals,
        dedup_refused=len(graph.dedup_refused),
        dedup_verdicts_reused=counters.dedup_verdicts_reused,
        dedup_unsure=counters.dedup_unsure,
        dedup_verdicts_expired=counters.dedup_verdicts_expired,
        dedup_union_refused=counters.dedup_union_refused,
        dedup_refused_collapsed=counters.dedup_refused_collapsed,
        derive_fallback_edges=len(graph.derive_fallback_edges),
        owner_conflicts=counters.owner_conflicts,
        owner_conflicts_judged=counters.owner_conflicts_judged,
        redundant_edges=counters.redundant_edges,
        cycles_broken=counters.cycles_broken,
        unsupported_nodes=counters.unsupported_nodes,
        conform_judge_calls=counters.conform_judge_calls,
        in_scope_disagreements=counters.in_scope_disagreements,
        in_scope_dirty_wins=counters.in_scope_dirty_wins,
        unsnapped_source_quotes=counters.unsnapped_source_quotes,
        provisional_keys_left=counters.provisional_keys_left,
        attributed_fragments_dropped=counters.attributed_fragments_dropped,
        nodes_missing_source_documents=counters.nodes_missing_source_documents,
    )
    return graph


# Grounding statistics


@dataclass(frozen=True)
class GroundingStats:
    """How well one build's canonical nodes ground, each node grounded on its own, before Conform merges any
    (``grounding_stats``).
    """

    unsnapped_quote_nodes: dict[str, int] = field(default_factory=dict)
    unsnapped_source_quotes: int | None = None
    provisional_keys_left: int = 0
    attributed_fragments_dropped: int = 0
    nodes_missing_source_documents: int = 0


def grounding_stats(
    plan: PlanArtifact, resolved: ResolveArtifact, edges: EdgesArtifact, ontology: Ontology
) -> GroundingStats:
    """The grounding statistics of one build from its stored plan, resolve and extract-edges artifacts, as Conform
    counts them.
    """

    legacy = bool(resolved.canonical) and not any("quotes" in node.model_fields_set for node in resolved.canonical)
    corpus = _Corpus(plan)
    ids = IdFinder(ontology)
    grounder = _Grounder(corpus, ids, frozenset(ontology.hub_labels), secondary=not legacy)
    grounded = [
        _ground_node(
            node,
            edges.attributes.get(node.cid, {}),
            grounder,
            _legacy_quotes(node, edges) if legacy else node.quotes,
        )
        for node in resolved.canonical
    ]
    keyed = [_final_key(node, ontology, ids) for node in grounded]
    return _grounding_stats(grounded, keyed, corpus, sources=not legacy)


def _legacy_quotes(node: CanonicalNode, edges: EdgesArtifact) -> tuple[SourceQuote, ...]:
    """The one source quote of an artifact written before per-source quotes: the ``quote`` attribute over all the node's
    fragments.
    """

    quote = edges.attributes.get(node.cid, {}).get(QUOTE_ATTRIBUTE)
    return (SourceQuote(quote=quote, fragment_ids=node.fragment_ids),) if isinstance(quote, str) and quote else ()


def _grounding_stats(
    grounded: Sequence[_Node], keyed: Sequence[bool], corpus: _Corpus, *, sources: bool
) -> GroundingStats:
    """``GroundingStats`` of freshly grounded nodes (one per canonical node) and whether each got its
    final key; ``sources``: whether the source quotes count (else None)."""

    unsnapped: Counter[str] = Counter()
    dropped = 0
    missing = 0
    for node in grounded:
        if node.primary is None:
            unsnapped[node.canonical.label] += 1
        attributed = {fid for fid in node.canonical.fragment_ids if corpus.groundable(fid) is not None}
        evidence = node.fragment_ids
        dropped += len(attributed - evidence)
        documents = {corpus.fragments[fid].rec.document_id for fid in attributed}
        grounded_in = {corpus.fragments[fid].rec.document_id for fid in evidence}
        missing += bool(documents - grounded_in)
    return GroundingStats(
        unsnapped_quote_nodes=dict(sorted(unsnapped.items())),
        unsnapped_source_quotes=sum(node.unsnapped_quotes for node in grounded) if sources else None,
        provisional_keys_left=sum(
            1 for node, final in zip(grounded, keyed, strict=True) if node.canonical.provisional and not final
        ),
        attributed_fragments_dropped=dropped,
        nodes_missing_source_documents=missing,
    )


# Spine exclude values of merged nodes (T12, limited)


def _fresh_members(grounded: Iterable[_Node], corpus: _Corpus, plan: PlanArtifact) -> frozenset[str]:
    """``update`` builds: the cids of canonical nodes grounded in a document this build reads new or
    revised; empty in ``first`` and ``rebuild`` builds."""

    if plan.mode != "update":
        return frozenset()
    dirty = {*plan.change_set.new, *plan.change_set.revised}
    return frozenset(
        node.cids[0]
        for node in grounded
        if any(corpus.fragments[fid].rec.document_id in dirty for fid in node.fragment_ids)
    )


def _prefer_fresh_values(
    nodes: Iterable[_Node],
    fresh: frozenset[str],
    attributes: Mapping[str, Mapping[str, AttributeValue]],
    ontology: Ontology,
    persisted: Mapping[str, PersistedIdentity],
) -> int:
    """T12 (limited): a merged node whose members disagree on an attribute the spine ``exclude`` condition reads takes
    the value of the first member (in merge order) grounded in a new or revised document of this build, when it has one
    and some member is not so grounded.
    """

    watched = sorted(ontology.spine_exclude())
    changed = 0
    for node in nodes:
        if len(node.cids) < MIN_DEDUP_GROUP or not watched:
            continue
        members = [cid for cid in node.cids if cid in fresh]
        if len(members) == len(node.cids):
            continue
        stored = next((persisted[key] for key in (node.key, *sorted(node.aliases)) if key in persisted), None)
        took = False
        for name in watched:
            values = {
                cid: ontology.normalise_attribute(node.label, name, attributes.get(cid, {}).get(name))
                for cid in node.cids
            }
            if len(set(values.values()) - {None}) <= 1:
                continue
            winner = next((cid for cid in members if values[cid] is not None), None)
            fresh_value = winner is not None
            if winner is None and stored is not None:
                kept = ontology.normalise_attribute(node.label, name, dict(stored.attributes).get(name))
                winner = next((cid for cid in node.cids if kept is not None and values[cid] == kept), None)
            if winner is None:
                continue
            current = ontology.normalise_attribute(node.label, name, node.attributes.get(name))
            if current != values[winner]:
                node.attributes[name] = attributes[winner][name]
                took = took or fresh_value
        changed += took
    return changed


def _exclude_disagreements(
    nodes: Iterable[_Node], attributes: Mapping[str, Mapping[str, AttributeValue]], ontology: Ontology
) -> int:
    """Merged nodes (several canonical nodes) whose members gave different non-null normalised values
    for an attribute the spine ``exclude`` condition reads (``in_scope_disagreements``; the merged node
    keeps the first value)."""

    watched = sorted(ontology.spine_exclude())
    count = 0
    for node in nodes:
        if len(node.cids) < MIN_DEDUP_GROUP:
            continue
        for name in watched:
            values = {
                ontology.normalise_attribute(node.label, name, attributes.get(cid, {}).get(name)) for cid in node.cids
            }
            if len(values - {None}) > 1:
                count += 1
                break
    return count


def _attributes(node: _Node, ontology: Ontology) -> dict[str, AttributeValue]:
    """The stored attribute values: normalised, a null value replaced by the attribute's ``default``."""

    specs = ontology.attribute_specs(node.label)
    values: dict[str, AttributeValue] = {}
    for name in sorted({*node.attributes, *(name for name, spec in specs.items() if spec.default is not None)}):
        spec = specs.get(name)
        if spec is None or not spec.store or name == QUOTE_ATTRIBUTE:
            continue
        value = ontology.normalise_attribute(node.label, name, node.attributes.get(name))
        if value is None:
            value = spec.default
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


def _hub_names(
    nodes: Iterable[_Node], hubs: Collection[str], persisted: Mapping[str, PersistedIdentity], corpus: _Corpus
) -> _HubNames:
    """The names of the hub nodes among ``nodes`` (``_hub_index``), the fragments naming each
    (``_named_in``) and the hub keys each groundable fragment's heading path names (each heading on
    its own)."""

    index = _hub_index([node for node in nodes if node.label in hubs], persisted)
    by_heading: dict[str, frozenset[str]] = {}
    headings: dict[str, frozenset[str]] = {}
    for fragment_id in corpus.ordered(corpus.fragments):
        fragment = corpus.groundable(fragment_id)
        if fragment is None or not index:
            continue
        keys: set[str] = set()
        for heading in fragment.rec.heading_path:
            if heading not in by_heading:
                by_heading[heading] = frozenset(index.count(hub_words(heading)))
            keys |= by_heading[heading]
        if keys:
            headings[fragment_id] = frozenset(keys)
    return _HubNames(index, _named_in(index, corpus), headings)


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
    """``MENTIONED_IN`` counts per hub node key of ``nodes``: the fragments naming it (``_named_in``) it
    is not grounded in."""

    result: dict[str, dict[str, int]] = {}
    for key, counts in named.items():
        if key not in nodes:
            continue
        grounded = nodes[key].fragment_ids
        kept = {fragment_id: count for fragment_id, count in counts.items() if fragment_id not in grounded}
        if kept:
            result[key] = kept
    return result


def _exclusive_conflicts(edges: Iterable[TargetEdge], nodes: Mapping[str, _Node], ontology: Ontology) -> int:
    return exclusive_conflicts(
        ((edge.relation, edge.source_key, edge.target_key) for edge in edges),
        {key: node.label for key, node in nodes.items()},
        ontology,
    )


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
    """Repeated node keys, aliases that collide with a key or another alias, and repeated edge keys."""

    keys = Counter(node.identity_key for node in graph.nodes)
    aliases = Counter(alias for node in graph.nodes for alias in node.identity_aliases)
    duplicate = sum(count - 1 for count in keys.values() if count > 1)
    duplicate += sum(1 for alias in aliases if alias in keys or aliases[alias] > 1)
    edge_keys = Counter(edge.edge_key for edge in graph.edges)
    duplicate += sum(count - 1 for count in edge_keys.values() if count > 1)
    return duplicate


def _ungrounded(graph: TargetGraph, fragments: Mapping[str, FragmentRec]) -> int:
    """Nodes without evidence inside a grounding fragment and stated/implied edges without one."""

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


_D2_CHECKS: Final = frozenset({"illegal_pair", "where", "depth", "across", "stated_only"})
_D6_CHECKS: Final = frozenset({"owner", "exclusive"})
_D7_CHECKS: Final = frozenset({"acyclic"})


def _validator_counts(graph: TargetGraph, ontology: Ontology) -> Counter[str]:
    """Shared-validator violations of the target graph per invariant: D2 (a pair's conditions, depth, ``stated_only``),
    D6 (owner, exclusive) and D7 (cycles).
    """

    unique: dict[str, TargetNode] = {}
    for node in graph.nodes:  # a repeated key breaks D3, checked separately
        unique.setdefault(node.identity_key, node)
    view = InMemoryGraphView(
        (ViewNode(key, node.label, node.attributes) for key, node in unique.items()),
        (ViewEdge(edge.relation, edge.source_key, edge.target_key, edge.basis) for edge in graph.edges),
        ontology,
    )
    counts: Counter[str] = Counter()
    resolved = resolves_conflicts(ontology)
    for violation in violations(view, ontology):
        if violation.check in _D2_CHECKS:
            counts["D2"] += 1
        elif violation.check in _D6_CHECKS and resolved:
            counts["D6"] += 1
        elif violation.check in _D7_CHECKS:
            counts["D7"] += 1
    return counts


def check_invariants(graph: TargetGraph, plan: PlanArtifact, ontology: Ontology) -> None:
    """D1-D4, D6 and D7 of on a target graph (D2's conditions, D6 and D7 with the shared validator,
    ``_validator_counts``); the first violated invariant raises ``ConformanceError``.
    """

    fragments = {f.fragment_id: f for f in plan.fragments if f.status in GROUNDING_STATUSES}
    labels = set(ontology.labels)

    unlabelled = sum(1 for node in graph.nodes if node.label not in labels)
    if unlabelled:
        raise ConformanceError("conform_invariant_violated", invariant="D1", count=unlabelled)
    found = _validator_counts(graph, ontology)
    illegal = _illegal_edges(graph, ontology) or found["D2"]
    if illegal:
        raise ConformanceError("conform_invariant_violated", invariant="D2", count=illegal)
    duplicate = _duplicates(graph)
    if duplicate:
        raise ConformanceError("conform_invariant_violated", invariant="D3", count=duplicate)
    ungrounded = _ungrounded(graph, fragments)
    if ungrounded:
        raise ConformanceError("conform_invariant_violated", invariant="D4", count=ungrounded)
    for invariant in ("D6", "D7"):
        if found[invariant]:
            raise ConformanceError("conform_invariant_violated", invariant=invariant, count=found[invariant])


def _inside(lines: EvidenceLines, fragments: Mapping[str, FragmentRec]) -> bool:
    fragment = fragments.get(lines.fragment_id)
    return fragment is not None and fragment.line_start <= lines.line_start <= lines.line_end <= fragment.line_end
