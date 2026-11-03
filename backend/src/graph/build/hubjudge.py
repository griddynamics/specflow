"""The hub judge of Resolve step 4: what the ``HUBS_PROMPT`` judge is shown of a hub item, which items reach it
together, and how its answers join hub clusters.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
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
    UNSURE,
    Apply,
    Ask,
    Decision,
    DecisionMemory,
    Pair,
    PassHooks,
    PassItems,
    PassSpec,
    Proposal,
    RawGroup,
    StoredConfidence,
    blocks,
    consolidate,
    evidence_hash,
    nearest_pairs,
    nn_chain,
    pair,
    pair_cosines,
    settled_items,
    short_id,
)
from graph.build.hubforms import hub_surface, hub_words
from graph.build.models import (
    Confidence,
    ConsolidationCounters,
    FragmentRec,
    HubMerge,
    HubMergeSource,
    HubName,
    PairCandidate,
    PairOutcome,
    PairSource,
    PlanArtifact,
    RawNode,
)
from graph.build.vectors import Vector, VectorCache
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm import Gateway
from graph.core.llm.gateway import prompt_name
from graph.core.logfields import count_fields
from graph.core.ontology import Ontology
from logger.logger import INFO, get_logger, log_event

__all__ = [
    "EVIDENCE_LINES",
    "MIN_ITEMS",
    "PAIR_BATCH_ITEMS",
    "PAIR_CALL_CAP",
    "PAIR_FLOOR",
    "PAIR_K",
    "TWINS_PROMPT",
    "TWIN_BATCH_PAIRS",
    "TWIN_MEMORY",
    "Clusters",
    "ConsolidationParams",
    "GroupFacts",
    "HeadingId",
    "HeadingTree",
    "HubContext",
    "HubGroup",
    "HubGroups",
    "HubItem",
    "HubJoiner",
    "HubJudge",
    "HubPassResult",
    "JudgeItem",
    "TwinAnswer",
    "TwinAnswers",
    "candidate_text",
    "consolidate_hubs",
    "embedding_text",
    "hub_block_messages",
    "hub_item_hash",
    "label_part",
    "majority_label",
    "modifier_of",
    "pair_batches",
    "twin_messages",
    "twin_pairs",
    "twin_system",
    "unit_rows",
]

logger = get_logger("graph.build.hubjudge")

EVIDENCE_LINES: Final = 2
"""Default source lines per hub item shown to the judge (``ConsolidationParams.evidence_lines``)."""
QUOTE_MAX_CHARS: Final = 200
HEADING_MAX_CHARS: Final = 120
REQUIREMENT_MAX_CHARS: Final = 100
REQUIREMENTS_SHOWN: Final = 3
HEADING_SEPARATOR: Final = " > "
TWINS_PROMPT: Final = "hub_twins.3"
"""Prompt version of the twin question (``twin_messages``); part of its decision fingerprint."""
TWIN_MEMORY: Final = "hubs.twin"
"""Decision-memory pass of the twin question (keys ``<project>|hubs.twin|<key a>|<key b>``)."""
TWIN_BATCH_PAIRS: Final = 8
"""Most twin pairs one ``TWINS_PROMPT`` call shows the judge."""
TWIN_ID: Final = "p"
PAIR_K: Final = 5
"""Nearest items per item (candidate vectors) a vector pair may come from."""
PAIR_FLOOR: Final = 0.52
"""Lowest candidate-vector cosine of a vector pair."""
PAIR_BATCH_ITEMS: Final = 16
"""Most items one pair-pass batch shows the judge."""
PAIR_CALL_CAP: Final = 60
"""Default most pair-pass batches (judge calls) per build (``ConsolidationParams.pair_call_cap``)."""
MIN_ITEMS: Final = 2
"""Items a consolidation needs to have anything to join."""
COSINE_DIGITS: Final = 4
"""Digits a recorded pair cosine keeps."""

_ROLE_SUBJECT: Final = {"anchor": "part of the system", "group": "grouping"}
_LINK_WORDS: Final = frozenset(
    {
        "a",
        "about",
        "across",
        "after",
        "against",
        "and",
        "as",
        "at",
        "before",
        "behind",
        "between",
        "by",
        "for",
        "from",
        "in",
        "inside",
        "into",
        "of",
        "on",
        "or",
        "outside",
        "over",
        "per",
        "the",
        "through",
        "to",
        "under",
        "via",
        "with",
        "within",
        "without",
    }
)
"""Words after which a longer hub name still names the shorter one (``X for Y``, ``X over Y``, ``X and Y``):
generic English prepositions, conjunctions and articles (``modifier_of``)."""
_LEAD_MARKUP: Final = re.compile(r"^(?:[-*+#>|]+\s*|\d+[.)]\s+)*")
_PROPOSED_CONFIDENCE: Final[Mapping[str, Confidence]] = {"high": "high", "medium": "medium"}
_STORED_CONFIDENCE: Final[Mapping[str, StoredConfidence]] = {"high": "high", "medium": "medium"}


_COMPOUND_RULE: Final = (
    "A heading or a name that joins two things (X and Y, X & Y, X/Y) names two things: the nodes named "
    "for X and for Y are different things even when one heading names both, and two compound names "
    "that share one part but differ in the other (X and Y, X and Z) are different things. A compound "
    "node never joins its parts: never group the node for X with the node for Y because a compound "
    "names both. Group a compound with the node of one of its parts only when the lines show that "
    "the compound names that one thing (its second part is a piece or a detail of it). "
    'GOOD: under the heading "Search and Export", "Search" and "Export" stay apart; "Billing and '
    'invoices" and "Billing and refunds" stay apart; "Notifications" and "Notifications and inbox" '
    "are grouped when the lines show the inbox is where the notifications are read. "
    'BAD: grouping "Search" with "Export" because one heading names both; grouping "Billing and '
    'invoices" with "Billing and refunds" because both start with "Billing".'
)
"""The compound-name rule of the ``HUBS_PROMPT`` judge (``hub_system``): generic English, no ontology names."""
_TWIN_EXAMPLES: Final = (
    'GOOD: the release table row "Exports | New | Yes" and the chapter "Exports" with its export '
    'requirements are one thing; the glossary row "Ingest pipeline | subsystem that parses uploads" and '
    'the diagram box "Ingest pipeline" are one thing, labelled by what the glossary says it is. BAD: '
    "answering same false because one line is a table row and the other a chapter, or because one "
    'label is a part and the other a grouping of what it offers. Same false is right for "Payments" '
    "named as the payment provider's hosted service in one line and as the system's own payment pages "
    'in the other (two owners), and for "Search" when one line defines it as the index service and the '
    "other as the filter step inside it (a piece of the other)."
)
"""GOOD/BAD examples of the ``TWINS_PROMPT`` judge (``twin_system``): generic English, no ontology names."""


@dataclass(frozen=True)
class ConsolidationParams:
    """Settings of the hub pass (internal; the A/B replays vary them): evidence lines per item, block
    size and overlap, whether the pair pass (candidate pairs and pair batches) runs, and its call cap."""

    evidence_lines: int = EVIDENCE_LINES
    block_size: int = BLOCK_SIZE
    block_overlap: int = BLOCK_OVERLAP
    pair_pass: bool = True
    pair_call_cap: int = PAIR_CALL_CAP
    twin_pass: bool = True
    """Whether cross-label twins (``twin_pairs``) are asked as explicit pairs after the pair pass."""

    def __post_init__(self) -> None:
        if not 0 <= self.block_overlap < self.block_size or self.block_size < MIN_ITEMS:
            message = f"blocks need 0 <= overlap < size and size >= {MIN_ITEMS}"
            raise ValueError(message)
        if self.evidence_lines < 0 or self.pair_call_cap < 0:
            message = "evidence lines and the pair call cap cannot be negative"
            raise ValueError(message)


# Shared helpers (Resolve re-exports them)


def embedding_text(label: str, name: str) -> str:
    """Text of a ``name_embedding``."""

    return f"{label}: {name}"


def majority_label(labels: Sequence[str]) -> str:
    """Most frequent label, ties to the first seen."""

    counts = Counter(labels)
    first = {label: index for index, label in reversed(list(enumerate(labels)))}
    return min(counts, key=lambda label: (-counts[label], first[label]))


def unit_rows(vectors: Sequence[Vector]) -> np.ndarray:
    """``vectors`` as a new matrix of float32 rows of length one (a zero row stays zero)."""

    matrix = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0
    matrix /= norms[:, None]
    return matrix


def hub_item_hash(label: str, surfaces: Iterable[str], shown: JudgeItem) -> str:
    """The evidence hash of a hub item (``consolidate.evidence_hash``): its label, its distinct surfaces
    (``hub_surface`` forms, so spacing and case variants of a name hash alike, sorted) and its judge line (``shown``:
    what the judge is shown of it).
    """

    fields = {name: value for name, value in shown.fields().items() if name != "name"}
    return evidence_hash([label, sorted(set(surfaces)), fields])


def label_part(labels: Collection[str], apart: Collection[str]) -> str:
    """The label part of a hub item with ``labels``: its first label whose type has
    ``merge_across_labels: false`` (in ``apart``), else ``""`` (the part of all other labels)."""

    return min((label for label in labels if label in apart), default="")


# What the judge is shown


@dataclass(frozen=True)
class JudgeItem:
    """One hub item as the judge sees it (module docstring, step 1); every text already safe."""

    label: str
    name: str
    quote: str = ""
    heading: str = ""
    requirements: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def fields(self) -> dict[str, object]:
        """The item as the judge sees it, without an id."""

        return {
            "label": self.label,
            "name": self.name,
            "quote": self.quote,
            "heading": self.heading,
            "requirements": list(self.requirements),
            "evidence": list(self.evidence),
        }

    def line(self, position: int) -> str:
        """The item's JSON line in a block, with the short id of its ``position``."""

        return json.dumps({"id": short_id(position), **self.fields()}, ensure_ascii=False)


def candidate_text(item: JudgeItem) -> str:
    """The text of an item's candidate vector: ``"<name>. <heading>. <quote>"`` (empty parts left out)."""

    return ". ".join(part for part in (item.name, item.heading, item.quote) if part)


def _holds(needle: Sequence[str], words: Sequence[str]) -> bool:
    size = len(needle)
    return any(tuple(words[index : index + size]) == tuple(needle) for index in range(len(words) - size + 1))


def _names(needles: Sequence[Sequence[str]], text: str) -> bool:
    words = hub_words(text)
    return any(_holds(needle, words) for needle in needles)


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _in_quote(line: str, quote: str) -> bool:
    """Whether the quote holds ``line`` (list and heading markup, case and spacing ignored)."""

    core = _LEAD_MARKUP.sub("", _flat(line)).strip()
    return bool(core) and core in _flat(quote)


@dataclass(frozen=True)
class HubContext:
    """The plan and raw statement nodes a hub item's judge line is drawn from (module docstring, step 1)."""

    fragments: Mapping[str, FragmentRec]
    rank: Mapping[str, int]
    lines: Mapping[str, Sequence[str]]
    statements: Mapping[str, Sequence[str]]
    """Names of the raw statement nodes (labels outside the hub labels) by fragment, artifact order."""
    evidence_lines: int = EVIDENCE_LINES

    @classmethod
    def of(
        cls, plan: PlanArtifact, raw_nodes: Iterable[RawNode], hub_labels: Collection[str], evidence_lines: int
    ) -> HubContext:
        statements: dict[str, list[str]] = {}
        for raw in raw_nodes:
            if raw.label not in hub_labels:
                for fragment_id in raw.fragment_ids:
                    statements.setdefault(fragment_id, []).append(raw.name)
        return cls(
            fragments={fragment.fragment_id: fragment for fragment in plan.fragments},
            rank={fragment.fragment_id: index for index, fragment in enumerate(plan.fragments)},
            lines={fragment.fragment_id: fragment.text.split("\n") for fragment in plan.fragments},
            statements=statements,
            evidence_lines=evidence_lines,
        )

    def _place(self, fragment_id: str) -> tuple[int, str]:
        return self.rank.get(fragment_id, len(self.rank)), fragment_id

    def item(self, label: str, name: str, surfaces: Iterable[str], raws: Sequence[RawNode]) -> JudgeItem:
        """The judge line of a hub item named ``surfaces`` with raw nodes ``raws``."""

        needles = [needle for needle in dict.fromkeys(tuple(hub_words(surface)) for surface in surfaces) if needle]
        fragment_ids = sorted({fragment_id for raw in raws for fragment_id in raw.fragment_ids}, key=self._place)
        quoted = [raw for raw in raws if raw.quote]
        first = min(
            quoted,
            key=lambda raw: (min(map(self._place, raw.fragment_ids), default=(len(self.rank), "")), raw.quote or ""),
            default=None,
        )
        quote = prompt_name((first.quote or "") if first is not None else "", QUOTE_MAX_CHARS)
        main = next(
            (
                fragment_id
                for fragment_id in fragment_ids
                if any(_names(needles, line) for line in self.lines.get(fragment_id, ()))
            ),
            fragment_ids[0] if fragment_ids else None,
        )
        fragment = self.fragments.get(main) if main is not None else None
        heading = HEADING_SEPARATOR.join(fragment.heading_path) if fragment is not None else ""
        return JudgeItem(
            label=label,
            name=prompt_name(name),
            quote=quote,
            heading=prompt_name(heading, HEADING_MAX_CHARS),
            requirements=self._requirements(needles, main),
            evidence=self._evidence(needles, fragment_ids, quote),
        )

    def _requirements(self, needles: Sequence[Sequence[str]], main: str | None) -> tuple[str, ...]:
        # name order, not artifact order: an update reuses stored raw nodes in another order
        names = sorted(self.statements.get(main, ())) if main is not None else ()
        ordered = [name for name in names if _names(needles, name)] + [
            name for name in names if not _names(needles, name)
        ]
        shown = [text for text in dict.fromkeys(prompt_name(name, REQUIREMENT_MAX_CHARS) for name in ordered) if text]
        return tuple(shown[:REQUIREMENTS_SHOWN])

    def _evidence(self, needles: Sequence[Sequence[str]], fragment_ids: Sequence[str], quote: str) -> tuple[str, ...]:
        found: list[str] = []
        if self.evidence_lines <= 0:
            return ()
        for fragment_id in fragment_ids:
            for line in self.lines.get(fragment_id, ()):
                if not _names(needles, line) or (quote and _in_quote(line, quote)):
                    continue
                text = prompt_name(line, LINE_MAX_CHARS)
                if text and text not in found:
                    found.append(text)
                    if len(found) == self.evidence_lines:
                        return tuple(found)
        return tuple(found)


HeadingId = tuple[str, int]
"""A heading line of the plan: its document and its source line."""
_HEADING: Final = re.compile(r"^(#{1,6})\s+(\S.*)$")
_FENCE: Final = re.compile(r"^\s*(?:```|~~~)")


@dataclass(frozen=True)
class HeadingTree:
    """The ATX heading lines (``#`` to ``######``, outside code fences) of a plan's documents: the headings each one
    lies under (``ancestors``: in its document, every earlier heading of a lower level not closed by a heading of its
    own level or lower) and, per fragment, its heading lines by their text, with and without the ``#`` markup
    (``lines``, spacing flattened).
    """

    ancestors: Mapping[HeadingId, frozenset[HeadingId]]
    lines: Mapping[str, Mapping[str, HeadingId]]

    @classmethod
    def of(cls, plan: PlanArtifact) -> HeadingTree:
        levels: dict[str, dict[int, int]] = {}
        lines: dict[str, dict[str, HeadingId]] = {}
        for fragment in plan.fragments:
            if fragment.status == "retired":
                continue
            found = lines.setdefault(fragment.fragment_id, {})
            fenced = False
            for offset, line in enumerate(fragment.text.split("\n")):
                if _FENCE.match(line):
                    fenced = not fenced
                    continue
                match = None if fenced else _HEADING.match(line.strip())
                if match is None:
                    continue
                heading = (fragment.document_id, fragment.line_start + offset)
                levels.setdefault(fragment.document_id, {})[heading[1]] = len(match.group(1))
                for text in (_flat(line), _flat(match.group(2))):
                    found.setdefault(text, heading)
        ancestors: dict[HeadingId, frozenset[HeadingId]] = {}
        for document, by_line in levels.items():
            still_open: list[tuple[int, int]] = []  # (line, level) of the headings still open
            for number, level in sorted(by_line.items()):
                while still_open and still_open[-1][1] >= level:
                    still_open.pop()
                ancestors[(document, number)] = frozenset((document, above) for above, _ in still_open)
                still_open.append((number, level))
        return cls(ancestors=ancestors, lines=lines)

    def headings(self, raws: Iterable[RawNode]) -> frozenset[HeadingId]:
        """The heading lines ``raws`` were extracted from: a raw node whose quote is a heading line of
        one of its fragments, with or without its ``#`` markup (spacing ignored)."""

        found: set[HeadingId] = set()
        for raw in raws:
            quote = _flat(raw.quote or "")
            if not quote:
                continue
            for fragment_id in raw.fragment_ids:
                heading = self.lines.get(fragment_id, {}).get(quote)
                if heading is not None:
                    found.add(heading)
        return frozenset(found)

    def under(self, headings: Iterable[HeadingId]) -> frozenset[HeadingId]:
        """Every heading one of ``headings`` lies under."""

        return frozenset(above for heading in headings for above in self.ancestors.get(heading, ()))


def _grouping_and_part(ontology: Ontology) -> str:
    """`` (a <group label> and its <anchor label> ...)``: the first group-role and anchor-role hub labels
    in declaration order that merge across labels; empty when the ontology has no such pair."""

    roles = {node_type.label: node_type.role for node_type in ontology.node_types.values()}
    labels = [
        label
        for label in ontology.declared_labels
        if label in ontology.hub_labels and ontology.merge_across_labels(label)
    ]
    group = next((label for label in labels if roles.get(label) == "group"), None)
    anchor = next((label for label in labels if roles.get(label) == "anchor"), None)
    if group is None or anchor is None:
        return ""
    return f" (a {group} and its {anchor} are different things)"


def _hub_definitions(ontology: Ontology, labels: Collection[str] | None = None) -> str:
    """Each hub type's label, role subject and description (the ontology's wording only); only those of
    ``labels`` when given."""

    return "\n\n".join(
        f"{node_type.label} (a {_ROLE_SUBJECT.get(node_type.role or '', 'thing')}):\n{node_type.description.strip()}"
        for node_type in ontology.node_types.values()
        if node_type.label in ontology.hub_labels and (labels is None or node_type.label in labels)
    )


def hub_system(ontology: Ontology) -> str:
    """The system message of the ``HUBS_PROMPT`` judge (the ontology's wording only)."""

    hub_types = [node_type for node_type in ontology.node_types.values() if node_type.label in ontology.hub_labels]
    definitions = _hub_definitions(ontology)
    labels = ", ".join(node_type.label for node_type in hub_types)
    apart = [node_type.label for node_type in hub_types if not ontology.merge_across_labels(node_type.label)]
    own_label_only = (
        f" A node labelled {' or '.join(apart)} is never the same thing as a node of another label: "
        "group it only with nodes of its own label."
        if apart
        else ""
    )
    example = _grouping_and_part(ontology)
    return (
        f"Hub nodes were extracted from {ontology.framing}, document by document, so one part of "
        "the system or one grouping may occur several times, under different names or labels.\n"
        f"Hub types:\n{definitions}\n\n"
        "Each line of the block is one node as JSON: its id, label and name; quote: the source text "
        "it was extracted from; heading: the heading path of the section that names it; "
        "requirements: up to three requirements stated in that section; evidence: source lines that "
        "name it. Find the nodes that are the same part of the system or the same grouping: a short "
        "name can be an alias of a longer one (a table row or service name for the full name), and "
        "one thing can be extracted under different labels. Answer groups: each group lists the ids "
        "of the nodes that are one thing, the label the merged node should have (one of: "
        f"{labels}) and your confidence: high when the lines show they are one thing, medium when "
        "they very likely are, low when you suspect they are one thing but the lines cannot confirm "
        "it (a low group is not merged). Never group different things, such as a part and one of "
        "its sub-parts or two features with similar names. A grouping and the part of the system "
        f"that holds it are different things even when they share a name{example}.{own_label_only} "
        f"{_COMPOUND_RULE} "
        "Leave out nodes without a duplicate. "
        "Use only ids of the block."
    )


def hub_block_messages(ontology: Ontology, gateway: Gateway, items: Sequence[JudgeItem]) -> list[dict[str, str]]:
    """Messages of one ``HUBS_PROMPT`` call over a block (or pair batch) of items, shown with the
    short ids of their positions."""

    lines = [item.line(position) for position, item in enumerate(items)]
    return [
        {"role": "system", "content": hub_system(ontology)},
        {"role": "user", "content": gateway.untrusted("hubs", "\n".join(lines))},
    ]


class HubGroup(BaseModel):
    """One group of a ``HUBS_PROMPT`` answer: block ids of one hub, its label and the judge's
    confidence (``low``: unsure, so not merged and not stored; ``graph.build.consolidate``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ids: list[str]
    label: str
    confidence: Confidence


class HubGroups(BaseModel):
    """Answer of the ``HUBS_PROMPT`` judge: groups of hubs that are one node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    groups: list[HubGroup]


# The twin question


def twin_system(ontology: Ontology) -> str:
    """The system message of the ``TWINS_PROMPT`` judge (the ontology's wording only): one name under
    two labels, the same thing or not, and which label."""

    merging = [
        label
        for label in ontology.declared_labels
        if label in ontology.hub_labels and ontology.merge_across_labels(label)
    ]
    labels = ", ".join(merging)
    return (
        f"Hub nodes were extracted from {ontology.framing}, document by document.\n"
        f"Hub types:\n{_hub_definitions(ontology, merging)}\n\n"
        "Each line of the block is one pair of hub nodes as JSON: its id and the nodes a and b, each "
        "with its label and name; quote: the source text it was extracted from; heading: the heading "
        "path of the section that names it; requirements: up to three requirements stated in that "
        "section; evidence: source lines that name it. The two nodes of a pair have one name under two "
        "different labels. One name in two labels is one thing by default: the text names it in "
        "several places (a release or scope table row, a diagram box, a glossary row, its own chapter), "
        "and each place gave it another label. The type descriptions above decide the label, never "
        "whether a and b are one thing: a node given a label that does not fit it is still that thing. "
        f"For each pair answer same true, with the label the merged node should have (one of: {labels}): "
        "the label whose description fits best what the lines of both say it is (what it does, what it "
        "holds, where it runs). Answer same false, with an empty label, only when the lines show two "
        "different things: they state a different scope (one is a piece or a single step of the "
        "other), a different owner (another party's product or service and the system's own part) or a "
        "different meaning (two definitions that cannot describe one thing). Lines from different "
        "sections, a short table row against a whole chapter, a row or heading that names it with other "
        "things, or one node described as a part of the system and the other as what it offers, show no "
        "two things; when the lines show no two things, answer same true. "
        f"{_TWIN_EXAMPLES} Give your confidence: high when the lines show the answer, medium when it is "
        "very likely, low when the lines cannot confirm it (a low answer changes nothing). Answer every "
        "pair id once and use only ids of the block."
    )


def twin_messages(
    ontology: Ontology, gateway: Gateway, pairs: Sequence[tuple[JudgeItem, JudgeItem]]
) -> list[dict[str, str]]:
    """Messages of one ``TWINS_PROMPT`` call over ``pairs`` (ids ``p1``, ``p2``, ... by position)."""

    lines = [
        json.dumps({"id": twin_id(position), "a": first.fields(), "b": second.fields()}, ensure_ascii=False)
        for position, (first, second) in enumerate(pairs)
    ]
    return [
        {"role": "system", "content": twin_system(ontology)},
        {"role": "user", "content": gateway.untrusted("hubs", "\n".join(lines))},
    ]


def twin_id(position: int) -> str:
    """The id of the twin pair at ``position`` (0-based) of a call: ``p1``, ``p2``, ..."""

    return f"{TWIN_ID}{position + 1}"


class TwinAnswer(BaseModel):
    """The ``TWINS_PROMPT`` judge on one pair: one thing or not, the merged node's label, confidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    same: bool
    label: str
    confidence: Confidence


class TwinAnswers(BaseModel):
    """Answer of the ``TWINS_PROMPT`` judge: one entry per pair of the block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answers: list[TwinAnswer]


# Clusters and the joiner


def modifier_of(short: str, long: str) -> bool:
    """Whether hub surface ``long`` is ``short`` followed by more words, the first of them not a ``_LINK_WORDS`` word:
    ``short`` is then a modifier of what ``long`` names ("user" of "user provisioning", "atlassian" of "atlassian
    cloud"), not that thing ("todo item with question json", "dcr for mcp client" and "keycloak and identity provider"
    still name it).
    """

    head, words = short.split(), long.split()
    return bool(head) and len(words) > len(head) and words[: len(head)] == head and words[len(head)] not in _LINK_WORDS


@dataclass(frozen=True)
class GroupFacts:
    """What the hub pass needs of one exact group (Resolve step 2): its explicit ids, the label its id family fixes
    (None when none does), its label and the labels of its raw nodes; ``headings``: the heading lines its raw nodes were
    extracted from (``HeadingTree.headings``), ``under``: the headings those lie under (``HeadingTree.under``).
    """

    explicit_ids: frozenset[str]
    id_label: str | None
    label: str
    raw_labels: tuple[str, ...]
    headings: frozenset[HeadingId] = frozenset()
    under: frozenset[HeadingId] = frozenset()


class Clusters:
    """Union-find over group indexes with the cluster-level vetoes and each cluster's members."""

    def __init__(self, facts: Sequence[GroupFacts], max_size: int) -> None:
        self._facts = facts
        self._max = max_size
        self._parent = list(range(len(facts)))
        self._ids = [set(fact.explicit_ids) for fact in facts]
        self._headings = [set(fact.headings) for fact in facts]
        self._under = [set(fact.under) for fact in facts]
        self._members: dict[int, list[int]] = {index: [index] for index in range(len(facts))}

    def find(self, index: int) -> int:
        while self._parent[index] != index:
            self._parent[index] = self._parent[self._parent[index]]
            index = self._parent[index]
        return index

    def nested(self, first: int, second: int) -> bool:
        """Whether one cluster holds a group named from a heading the other's heading lines lie under
        (a section and its subsection: never one node)."""

        a, b = self.find(first), self.find(second)
        return bool(self._headings[a] & self._under[b] or self._headings[b] & self._under[a])

    def union(self, first: int, second: int) -> bool | None:
        """Merge; None when already one cluster, False when a veto refuses: two different explicit
        ids, more than ``max_size`` groups, or a section and its subsection (``nested``)."""

        a, b = self.find(first), self.find(second)
        if a == b:
            return None
        if self._ids[a] and self._ids[b] and self._ids[a] != self._ids[b]:
            return False
        if len(self._members[a]) + len(self._members[b]) > self._max:
            return False
        if self.nested(a, b):
            return False
        root, child = (a, b) if a < b else (b, a)
        self._parent[child] = root
        self._members[root] = self._members[root] + self._members.pop(child)
        self._ids[root] |= self._ids[child]
        self._headings[root] |= self._headings[child]
        self._under[root] |= self._under[child]
        return True

    def members(self, index: int) -> list[int]:
        """The group indexes of ``index``'s cluster, its root's first, then in merge order."""

        return self._members[self.find(index)]

    def labels(self, index: int) -> set[str]:
        """The labels of the groups of ``index``'s cluster."""

        return {self._facts[member].label for member in self.members(index)}

    def raw_labels(self, index: int) -> list[str]:
        return [label for member in self.members(index) for label in self._facts[member].raw_labels]

    def label(self, index: int, chosen: str | None) -> str:
        """The cluster's label: its explicit id family, else ``chosen``, else the majority of its raw labels."""

        fixed = next((label for member in self.members(index) if (label := self._facts[member].id_label)), None)
        return fixed or chosen or majority_label(self.raw_labels(index))


@dataclass(frozen=True)
class HubItem:
    """A hub cluster at the start of the pass: its root group, decision key, label, name, the hub
    surfaces of its names, its judge line and its evidence hash (``hub_item_hash``)."""

    root: int
    key: str
    label: str
    name: str
    surfaces: frozenset[str]
    shown: JudgeItem
    hash: str


@dataclass
class HubJoiner:
    """The apply side of the hub pass: joins the label parts of a proposal under the vetoes, merges
    the clusters and records each merged cluster's label in ``chosen`` (by cluster root)."""

    clusters: Clusters
    chosen: dict[int, str]
    items: Sequence[HubItem]
    hubs: frozenset[str]
    apart: frozenset[str]
    source: HubMergeSource = "block"
    """The source recorded for a model proposal's merges (a stored proposal's is ``stored``)."""
    applied: dict[str, int] = field(
        default_factory=lambda: {"vetoes": 0, "label_splits": 0, "merges": 0, "cross_label_merges": 0}
    )
    merges: list[HubMerge] = field(default_factory=list)
    """Every merge of the pass in application order: a member's cluster joined to its anchor's."""
    persisted: Mapping[str, str] = field(default_factory=dict)
    """``update`` builds: the identity key of the persisted node each item continues, by item key
    (``persisted_apart``); empty in ``first`` and ``rebuild``."""
    part_of: list[str] = field(init=False)
    """The label part of each item at the start of the pass (``label_part``)."""
    _item_of: dict[int, int] = field(init=False)

    def __post_init__(self) -> None:
        self.part_of = [label_part(self.clusters.labels(item.root), self.apart) for item in self.items]
        self._item_of = {item.root: index for index, item in enumerate(self.items)}

    def persisted_apart(self, first: int, second: int) -> bool:
        """Whether two items continue two different persisted nodes and one's name is the other's
        used as a modifier (``modifier_of``): a whole and its part or aspect, never one node in an
        ``update`` (module docstring, step 5)."""

        node_a = self.persisted.get(self.items[first].key)
        node_b = self.persisted.get(self.items[second].key)
        if node_a is None or node_b is None or node_a == node_b:
            return False
        a, b = self.items[first].surfaces, self.items[second].surfaces
        return any(modifier_of(x, y) or modifier_of(y, x) for x in a for y in b)

    def _clusters_apart(self, first: int, second: int) -> bool:
        """``persisted_apart`` for any item of cluster ``first`` with any item of cluster ``second``."""

        if not self.persisted:
            return False
        ones = [self._item_of[member] for member in self.clusters.members(first) if member in self._item_of]
        others = [self._item_of[member] for member in self.clusters.members(second) if member in self._item_of]
        return any(self.persisted_apart(one, other) for one in ones for other in others)

    def label_apart(self, first: int, second: int) -> bool:
        return self.part_of[first] != self.part_of[second]

    def never(self, first: int, second: int) -> bool:
        """Whether no answer can join two items: different label parts, a section and its
        subsection (``Clusters.nested``), or two persisted hubs of a whole and its part
        (``persisted_apart``)."""

        return (
            self.label_apart(first, second)
            or self.clusters.nested(self.items[first].root, self.items[second].root)
            or self.persisted_apart(first, second)
        )

    def joined(self, first: int, second: int) -> bool:
        """Whether two items are one cluster now."""

        return self.clusters.find(self.items[first].root) == self.clusters.find(self.items[second].root)

    def apply(self, proposal: Proposal, cannot: frozenset[Pair]) -> list[tuple[int, ...]]:
        parts: dict[str, list[int]] = {}
        for member in dict.fromkeys(proposal.members):
            parts.setdefault(self.part_of[member], []).append(member)
        self.applied["label_splits"] += int(len(parts) > 1)
        joined: list[tuple[int, ...]] = []
        for part in sorted(parts.values(), key=lambda part: min(self.items[item].key for item in part)):
            if len(part) >= MIN_ITEMS:
                joined.extend(self._join(part, proposal, cannot))
        return joined

    def _join(self, part: Sequence[int], proposal: Proposal, cannot: frozenset[Pair]) -> list[tuple[int, ...]]:
        ordered = sorted(part, key=lambda item: self.items[item].key)
        anchor = ordered[0]
        accepted = [anchor]
        for member in ordered[1:]:
            if any(pair(member, other) in cannot for other in accepted) or not self._merge(anchor, member, proposal):
                self.applied["vetoes"] += 1
                continue
            accepted.append(member)
        if len(accepted) < MIN_ITEMS:
            return []
        self._choose(self.items[anchor].root, proposal.label)
        return [tuple(accepted)]

    def _merge(self, anchor: int, member: int, proposal: Proposal) -> bool:
        """Join ``member``'s cluster into ``anchor``'s (recorded in ``merges``); False when a cluster
        veto refuses."""

        clusters, chosen = self.clusters, self.chosen
        first = clusters.find(self.items[anchor].root)
        second = clusters.find(self.items[member].root)
        differ = first != second and clusters.label(first, chosen.get(first)) != clusters.label(
            second, chosen.get(second)
        )
        if first != second and self._clusters_apart(first, second):
            return False
        outcome = clusters.union(first, second)
        if outcome is False:
            return False
        if outcome:
            self.applied["merges"] += 1
            self.applied["cross_label_merges"] += int(differ)
            self.merges.append(
                HubMerge(
                    anchor_key=self.items[anchor].key,
                    member_key=self.items[member].key,
                    source="stored" if proposal.stored else self.source,
                    confidence=proposal.confidence,
                )
            )
        return True

    def _choose(self, index: int, label: str | None) -> None:
        """The merged cluster's label: its label kept apart, else the proposed hub label, else the majority."""

        root = self.clusters.find(index)
        kept = sorted(self.clusters.labels(root) & self.apart)  # one label at most: the parts keep such labels apart
        if kept:
            self.chosen[root] = kept[0]
        elif label in self.hubs and label not in self.apart:
            self.chosen[root] = label
        else:
            self.chosen[root] = majority_label(self.clusters.raw_labels(root))


# The pass


@dataclass(frozen=True)
class HubJudge:
    """The services and settings of one hub pass; ``catalog``: the build's hub catalog (alias pairs)."""

    gateway: Gateway
    ontology: Ontology
    vectors: VectorCache
    memory: DecisionMemory | None
    params: ConsolidationParams
    catalog: Sequence[HubName] = ()


@dataclass(frozen=True)
class HubPassResult:
    """The pass's counters, its merges (application order) and its candidate pairs with their outcomes."""

    counters: ConsolidationCounters
    merges: tuple[HubMerge, ...] = ()
    pair_candidates: tuple[PairCandidate, ...] = ()


@dataclass
class _Candidate:
    """A candidate pair of items (``first < second``) of one part, how it was found and its outcome."""

    first: int
    second: int
    part: str
    name_cos: float = 0.0
    rich_cos: float = 0.0
    alias: bool = False
    vector: bool = False
    outcome: PairOutcome | None = None

    @property
    def pair(self) -> Pair:
        return self.first, self.second

    @property
    def source(self) -> PairSource:
        if self.alias and self.vector:
            return "both"
        return "alias" if self.alias else "vector"


_PAIR_COUNTERS: Final = (
    "pair_candidates_vector",
    "pair_candidates_alias",
    "pair_batches",
    "pair_calls",
    "pair_over_cap",
    "pair_merges",
    "alias_merges",
)
_UNDECIDED: Final[frozenset[PairOutcome]] = frozenset({"over_cap", "unsure"})
"""Candidate outcomes a later build may still judge although both items are settled (queued)."""
_SUMMED: Final = (
    "calls",
    "failed_calls",
    "groups_proposed",
    "unknown_ids",
    "unsure_groups",
    "decisions_read",
    "decisions_expired",
    "decision_overrides",
    "decisions_written",
    "kept_apart",
)


async def _unit(vectors: VectorCache, texts: Sequence[str]) -> np.ndarray:
    found = await vectors.mapping(texts)
    return unit_rows([found[text] for text in texts])


def _asker(judge: HubJudge, items: Sequence[HubItem]) -> Ask:
    async def ask(block: Sequence[int]) -> list[RawGroup]:
        messages = hub_block_messages(judge.ontology, judge.gateway, [items[item].shown for item in block])
        answer = await judge.gateway.structured("judge", None, messages, HubGroups, prompt=HUBS_PROMPT)
        return [(group.ids, group.label, group.confidence) for group in answer.groups]

    return ask


def _part_blocks(
    items: Sequence[HubItem], part_of: Sequence[str], unit: np.ndarray, params: ConsolidationParams
) -> list[list[int]]:
    """Each label part sorted by decision key, chained over its name vectors and cut into blocks."""

    parts: dict[str, list[int]] = {}
    for index, part in enumerate(part_of):
        parts.setdefault(part, []).append(index)
    block_list: list[list[int]] = []
    for part in sorted(parts):
        by_key = sorted(parts[part], key=lambda index: items[index].key)
        order = [by_key[row] for row in nn_chain(unit[by_key])]
        block_list.extend(blocks(order, params.block_size, params.block_overlap))
    return block_list


def _alias_pairs(items: Sequence[HubItem], part_of: Sequence[str], catalog: Iterable[HubName]) -> set[Pair]:
    """Item pairs of one part whose hub surfaces meet the name or an alias of one catalog entry."""

    by_surface: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        for surface in item.surfaces:
            by_surface.setdefault(surface, []).append(index)
    found: set[Pair] = set()
    for entry in catalog:
        names = {hub_surface(name) for name in (entry.name, *entry.aliases)} - {""}
        matched = sorted({index for name in names for index in by_surface.get(name, ())})
        found.update(
            pair(first, second)
            for first, second in itertools.combinations(matched, 2)
            if part_of[first] == part_of[second]
        )
    return found


def _vector_pairs(rich: np.ndarray, part_of: Sequence[str]) -> set[Pair]:
    """Per part, each item with its ``PAIR_K`` nearest items at exact candidate cosine ``PAIR_FLOOR`` or more."""

    parts: dict[str, list[int]] = {}
    for index, part in enumerate(part_of):
        parts.setdefault(part, []).append(index)
    found: set[Pair] = set()
    for members in parts.values():
        if len(members) < MIN_ITEMS:
            continue
        close = [pair(members[a], members[b]) for a, b in sorted(nearest_pairs(rich[members], PAIR_K, PAIR_FLOOR))]
        found.update(
            close_pair
            for close_pair, cosine in zip(close, pair_cosines(rich, close), strict=True)
            if cosine >= PAIR_FLOOR
        )
    return found


async def _candidates(
    items: Sequence[HubItem], part_of: Sequence[str], name_unit: np.ndarray, judge: HubJudge
) -> list[_Candidate]:
    """The candidate pairs of the pass (module docstring, step 3), in pair order."""

    rich = await _unit(judge.vectors, [candidate_text(item.shown) for item in items])
    alias = _alias_pairs(items, part_of, judge.catalog)
    vector = _vector_pairs(rich, part_of)
    found = sorted(alias | vector)
    names = pair_cosines(name_unit, found)
    riches = pair_cosines(rich, found)
    return [
        _Candidate(
            first,
            second,
            part_of[first],
            name_cos=name_cos,
            rich_cos=rich_cos,
            alias=(first, second) in alias,
            vector=(first, second) in vector,
        )
        for (first, second), name_cos, rich_cos in zip(found, names, riches, strict=True)
    ]


def pair_batches(
    queue: Iterable[Pair], part_of: Sequence[str], cap: int, size: int = PAIR_BATCH_ITEMS
) -> tuple[list[list[int]], list[Pair], list[Pair]]:
    """``queue`` packed first-fit into batches of at most ``size`` items of one part, at most ``cap``
    batches: the batches (items in order of arrival), the pairs placed and the pairs left over."""

    batches: list[list[int]] = []
    placed: list[Pair] = []
    over: list[Pair] = []
    for first, second in queue:
        target = next(
            (
                batch
                for batch in batches
                if part_of[batch[0]] == part_of[first] and len(set(batch) | {first, second}) <= size
            ),
            None,
        )
        if target is None and len(batches) < cap:
            target = []
            batches.append(target)
        if target is None:
            over.append((first, second))
            continue
        target.extend(item for item in (first, second) if item not in target)
        placed.append((first, second))
    return batches, placed, over


@dataclass
class _AnswerLog:
    """What a pass's answers said: pairs proposed, pairs in an unsure group, blocks answered and failed."""

    proposed: set[Pair] = field(default_factory=set)
    unsure: set[Pair] = field(default_factory=set)
    answered: list[frozenset[int]] = field(default_factory=list)
    failed: list[frozenset[int]] = field(default_factory=list)

    def ask(self, ask: Ask) -> Ask:
        async def logged(block: Sequence[int]) -> list[RawGroup]:
            try:
                answer = await ask(block)
            except LLMError:
                self.failed.append(frozenset(block))
                raise
            self.answered.append(frozenset(block))
            by_id = {short_id(position): item for position, item in enumerate(block)}
            for ids, _, confidence in answer:
                if confidence == UNSURE:
                    members = sorted(
                        {by_id[key] for key in (str(raw).strip().casefold() for raw in ids) if key in by_id}
                    )
                    self.unsure.update(itertools.combinations(members, 2))
            return answer

        return logged

    def apply(self, apply: Apply) -> Apply:
        def logged(proposal: Proposal, cannot: frozenset[Pair]) -> list[tuple[int, ...]]:
            self.proposed.update(pair(a, b) for a, b in itertools.combinations(proposal.members, 2))
            return apply(proposal, cannot)

        return logged

    def undecided(self, found: Pair) -> bool:
        """Whether no answer decided the pair: an unsure group held it, or every block sent with both
        items failed."""

        first, second = found

        def holds(block: frozenset[int]) -> bool:
            return first in block and second in block

        return found in self.unsure or (any(map(holds, self.failed)) and not any(map(holds, self.answered)))

    def outcome(self, candidate: _Candidate, joiner: HubJoiner) -> PairOutcome:
        if joiner.joined(candidate.first, candidate.second):
            return "same"
        if self.undecided(candidate.pair):
            return "unsure"
        return "vetoed" if candidate.pair in self.proposed else "different"


def _blocks_of(block_list: Sequence[Sequence[int]]) -> dict[int, set[int]]:
    found: dict[int, set[int]] = {}
    for number, block in enumerate(block_list):
        for item in block:
            found.setdefault(item, set()).add(number)
    return found


def _settle(
    candidates: Iterable[_Candidate], block_list: Sequence[Sequence[int]], joiner: HubJoiner, log: _AnswerLog
) -> None:
    """Mark the candidates the block pass settled: already one cluster, or both items in one block
    (``block``); a pair whose blocks failed or answered it unsure is ``unsure`` (judged next build)."""

    blocks_of = _blocks_of(block_list)
    for candidate in candidates:
        first, second = candidate.pair
        if joiner.joined(first, second):
            candidate.outcome = "block"
        elif blocks_of.get(first, set()) & blocks_of.get(second, set()):
            candidate.outcome = "unsure" if log.undecided(candidate.pair) else "block"


async def _apply_partners(
    items: Sequence[HubItem], block_list: Sequence[Sequence[int]], joiner: HubJoiner, memory: DecisionMemory
) -> tuple[int, int]:
    """Apply the valid stored ``same`` decisions of node-entry partners that no block holds together (a pair-pass merge
    of items that were no candidate pair, a block merge whose items the chain now puts apart); returns the pair
    decisions read and expired (node entries are counted by the blocks).
    """

    hashes = {item.key: item.hash for item in items}
    index_of = {item.key: index for index, item in enumerate(items)}
    nodes = await memory.nodes(HUBS_PASS, HUBS_PROMPT, hashes)
    blocks_of = _blocks_of(block_list)
    found: set[Pair] = set()
    for key, node in nodes.found.items():
        for partner in node.partners:
            other = index_of.get(partner)
            if other is not None and other != index_of[key]:
                found.add(pair(index_of[key], other))
    keyed: dict[tuple[str, str], Pair] = {}
    for a, b in sorted(found):
        if blocks_of.get(a, set()) & blocks_of.get(b, set()) or joiner.joined(a, b) or joiner.label_apart(a, b):
            continue
        low, high = sorted((items[a].key, items[b].key))
        keyed[(low, high)] = (a, b)
    if not keyed:
        return 0, 0
    stored = await memory.pairs(HUBS_PASS, HUBS_PROMPT, keyed, hashes)
    for keys, decision in sorted(stored.found.items()):
        if decision.same:
            confidence = _PROPOSED_CONFIDENCE.get(decision.confidence or "")
            joiner.apply(Proposal(keyed[keys], decision.label, stored=True, confidence=confidence), frozenset())
    return len(stored.found), stored.expired


async def _apply_stored(
    candidates: Sequence[_Candidate], items: Sequence[HubItem], joiner: HubJoiner, memory: DecisionMemory | None
) -> tuple[int, int]:
    """Valid stored decisions of the open candidates: ``different`` drops one, ``same`` is applied as a
    stored proposal; returns the decisions read and expired."""

    if memory is None or not candidates:
        return 0, 0
    hashes = {item.key: item.hash for item in items}
    keyed = {_keys(c, items): c for c in candidates}
    stored = await memory.pairs(HUBS_PASS, HUBS_PROMPT, keyed, hashes)
    for keys, decision in sorted(stored.found.items()):
        candidate = keyed[keys]
        candidate.outcome = _stored_outcome(candidate, decision, joiner)
    return len(stored.found), stored.expired


def _keys(candidate: _Candidate, items: Sequence[HubItem]) -> tuple[str, str]:
    """The candidate's identity keys, sorted."""

    return _pair_keys(items, candidate.pair)


def _pair_keys(items: Sequence[HubItem], found: Pair) -> tuple[str, str]:
    """The identity keys of an item pair, sorted."""

    first, second = sorted((items[found[0]].key, items[found[1]].key))
    return first, second


def _stored_outcome(candidate: _Candidate, decision: Decision, joiner: HubJoiner) -> PairOutcome:
    if not decision.same:
        return "different"
    proposal = Proposal(
        candidate.pair, decision.label, stored=True, confidence=_PROPOSED_CONFIDENCE.get(decision.confidence or "")
    )
    return "same" if joiner.apply(proposal, frozenset()) else "vetoed"


async def _pair_pass(
    candidates: Sequence[_Candidate],
    block_list: Sequence[Sequence[int]],
    items: Sequence[HubItem],
    *,
    joiner: HubJoiner,
    judge: HubJudge,
    name_unit: np.ndarray,
    block_log: _AnswerLog,
    settled: frozenset[int] = frozenset(),
) -> dict[str, int]:
    """The pair pass (module docstring, step 4): sets every candidate's outcome; returns its counts.
    ``settled``: the items settled before the block pass (``consolidate`` step 6)."""

    merged_before = len(joiner.merges)
    for candidate in candidates:
        if joiner.never(candidate.first, candidate.second):
            candidate.outcome = "vetoed"
    _settle([c for c in candidates if c.outcome is None], block_list, joiner, block_log)
    read, expired = await _apply_stored([c for c in candidates if c.outcome is None], items, joiner, judge.memory)
    if judge.memory is not None:
        partners_read, partners_expired = await _apply_partners(items, block_list, joiner, judge.memory)
        read, expired = read + partners_read, expired + partners_expired
    waiting = [c for c in candidates if c.outcome is None and not joiner.joined(c.first, c.second)]
    kept_apart = await _keep_settled_apart(waiting, items, settled, judge.memory)
    waiting = [c for c in waiting if c.outcome is None]
    waiting.sort(key=lambda c: (not c.alias, -c.rich_cos, items[c.first].key, items[c.second].key))
    by_pair = {c.pair: c for c in waiting}
    batch_list, placed, over = pair_batches((c.pair for c in waiting), joiner.part_of, judge.params.pair_call_cap)
    for found in over:
        by_pair[found].outcome = "over_cap"
    counts = dict.fromkeys(_SUMMED, 0) | {
        "decisions_read": read,
        "decisions_expired": expired,
        "kept_apart": kept_apart,
        "pair_batches": 0,
    }
    if batch_list:
        log = _AnswerLog()
        joiner.source = "pair"
        tally = await consolidate(
            spec=PassSpec(name=HUBS_PASS, prompt=HUBS_PROMPT, node_entries=False),
            items=PassItems(
                keys=[item.key for item in items], hashes=[item.hash for item in items], unit=name_unit, pairs=placed
            ),
            block_list=batch_list,
            hooks=PassHooks(ask=log.ask(_asker(judge, items)), apply=log.apply(joiner.apply), undecidable=joiner.never),
            memory=judge.memory,
            tally=ConsolidationCounters(),
            settled=settled,
        )
        for found in placed:
            by_pair[found].outcome = log.outcome(by_pair[found], joiner)
        # its expiries were counted already (node entries by the blocks, candidate pairs above)
        counts = {
            name: counts[name] + (getattr(tally, name) if name != "decisions_expired" else 0) for name in _SUMMED
        } | {"pair_batches": tally.blocks}
    for candidate in candidates:
        if candidate.outcome is None:  # joined by another stored decision of the pass
            candidate.outcome = "same" if joiner.joined(candidate.first, candidate.second) else "different"
    if judge.memory is not None:
        undecided = [_keys(c, items) for c in candidates if c.outcome in _UNDECIDED]
        await judge.memory.queue(HUBS_PASS, HUBS_PROMPT, undecided, {item.key: item.hash for item in items})
    pair_merges = joiner.merges[merged_before:]
    alias_keys = {_keys(c, items) for c in candidates if c.alias}
    return counts | {
        "pair_candidates_vector": sum(1 for c in candidates if c.vector),
        "pair_candidates_alias": sum(1 for c in candidates if c.alias),
        "pair_calls": counts["calls"],
        "pair_over_cap": len(over),
        "pair_merges": len(pair_merges),
        "alias_merges": sum(
            1 for merge in pair_merges if tuple(sorted((merge.anchor_key, merge.member_key))) in alias_keys
        ),
    }


async def _keep_settled_apart(
    waiting: Sequence[_Candidate], items: Sequence[HubItem], settled: frozenset[int], memory: DecisionMemory | None
) -> int:
    """Mark ``kept_apart`` the open candidates of two items settled before the build (an update) that
    no earlier build left undecided (``DecisionMemory.queued``): no stored decision covers them, so
    they are new pairs of unchanged hubs (``consolidate`` step 6); returns how many."""

    both = [c for c in waiting if c.first in settled and c.second in settled]
    if not both or memory is None:
        return 0
    hashes = {item.key: item.hash for item in items}
    queued = await memory.queued(HUBS_PASS, HUBS_PROMPT, [_keys(c, items) for c in both], hashes)
    kept = [c for c in both if _keys(c, items) not in queued]
    for candidate in kept:
        candidate.outcome = "kept_apart"
    return len(kept)


async def _settled_before(items: Sequence[HubItem], memory: DecisionMemory | None) -> frozenset[int]:
    """The items settled before the block pass (valid node entries, not unsure): none in a first build."""

    if memory is None or not items:
        return frozenset()
    nodes = await memory.nodes(HUBS_PASS, HUBS_PROMPT, {item.key: item.hash for item in items})
    return frozenset(settled_items([item.key for item in items], nodes.found))


def twin_pairs(items: Sequence[HubItem], part_of: Sequence[str]) -> list[Pair]:
    """Cross-label twins: item pairs of one label part, of different labels, that share a hub surface
    (one name, two labels), in pair order."""

    by_surface: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(items):
        for surface in item.surfaces:
            by_surface.setdefault((part_of[index], surface), []).append(index)
    found = {
        pair(first, second)
        for members in by_surface.values()
        for first, second in itertools.combinations(sorted(set(members)), 2)
        if items[first].label != items[second].label
    }
    return sorted(found)


_TWIN_COUNTS: Final = (
    "calls",
    "failed_calls",
    "unsure_groups",
    "decisions_read",
    "decisions_expired",
    "decisions_written",
)


@dataclass
class _TwinRound:
    """One twin pass: its pairs' outcomes, the decisions to store and its counts."""

    outcomes: dict[Pair, PairOutcome] = field(default_factory=dict)
    decisions: dict[tuple[str, str], Decision] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_TWIN_COUNTS, 0))

    def apply(self, found: Pair, proposal: Proposal, joiner: HubJoiner) -> None:
        self.outcomes[found] = "same" if joiner.apply(proposal, frozenset()) else "vetoed"


async def _ask_twins(judge: HubJudge, items: Sequence[HubItem], batch: Sequence[Pair]) -> list[TwinAnswer] | None:
    """One ``TWINS_PROMPT`` call over ``batch``; None when it failed (``LLMFatalError`` propagates)."""

    shown = [(items[first].shown, items[second].shown) for first, second in batch]
    messages = twin_messages(judge.ontology, judge.gateway, shown)
    try:
        answer = await judge.gateway.structured("judge", None, messages, TwinAnswers, prompt=TWINS_PROMPT)
    except LLMFatalError:
        raise
    except LLMError:
        return None
    return answer.answers


async def _ask_all_twins(
    judge: HubJudge, items: Sequence[HubItem], batches: Sequence[Sequence[Pair]]
) -> list[list[TwinAnswer] | None]:
    """Every twin call concurrently, in order; ``LLMFatalError`` cancels the others and propagates."""

    tasks = [asyncio.ensure_future(_ask_twins(judge, items, batch)) for batch in batches]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _read_twins(
    batch: Sequence[Pair], answers: Sequence[TwinAnswer] | None, items: Sequence[HubItem], joiner: HubJoiner
) -> _TwinRound:
    """One call's answers applied: a ``same`` answer is proposed (under the vetoes, the label chosen as
    for any proposal); a ``low`` answer, a missing one or a failed call is unsure (nothing stored)."""

    out = _TwinRound()
    by_id = {twin_id(position): found for position, found in enumerate(batch)}
    answered: dict[Pair, TwinAnswer] = {}
    for given in answers or ():
        found = by_id.get(given.id.strip().casefold())
        if found is not None and found not in answered:
            answered[found] = given
    for found in batch:
        answer = answered.get(found)
        if answer is None or answer.confidence == UNSURE:
            out.outcomes[found] = "unsure"
            out.counts["unsure_groups"] += int(answer is not None)
            continue
        confidence = _PROPOSED_CONFIDENCE.get(answer.confidence)
        label = answer.label or None
        out.decisions[_pair_keys(items, found)] = Decision(
            answer.same, label if answer.same else None, _STORED_CONFIDENCE.get(answer.confidence)
        )
        if answer.same:
            out.apply(found, Proposal(found, label, confidence=confidence), joiner)
        else:
            out.outcomes[found] = "different"
    return out


async def _twin_pass(items: Sequence[HubItem], joiner: HubJoiner, judge: HubJudge) -> _TwinRound:
    """Cross-label twins not yet one node (``twin_pairs``), each asked as an explicit pair: valid stored twin decisions
    (pass ``TWIN_MEMORY``) first, the rest in calls of at most ``TWIN_BATCH_PAIRS`` pairs outside the pair-pass cap;
    their merges are recorded with source ``pair`` (``stored`` when stored).
    """

    out = _TwinRound()
    twins = [
        found for found in twin_pairs(items, joiner.part_of) if not (joiner.joined(*found) or joiner.never(*found))
    ]
    if not twins:
        return out
    joiner.source = "pair"
    hashes = {item.key: item.hash for item in items}
    if judge.memory is not None:
        stored = await judge.memory.pairs(
            TWIN_MEMORY, TWINS_PROMPT, {_pair_keys(items, found): found for found in twins}, hashes
        )
        out.counts["decisions_read"], out.counts["decisions_expired"] = len(stored.found), stored.expired
        for found in twins:
            decision = stored.found.get(_pair_keys(items, found))
            if decision is None:
                continue
            if decision.same:
                confidence = _PROPOSED_CONFIDENCE.get(decision.confidence or "")
                out.apply(found, Proposal(found, decision.label, stored=True, confidence=confidence), joiner)
            else:
                out.outcomes[found] = "different"
    asked = [found for found in twins if found not in out.outcomes]
    batches = [asked[start : start + TWIN_BATCH_PAIRS] for start in range(0, len(asked), TWIN_BATCH_PAIRS)]
    for batch, answers in zip(batches, await _ask_all_twins(judge, items, batches), strict=True):
        round_ = _read_twins(batch, answers, items, joiner)
        out.outcomes |= round_.outcomes
        out.decisions |= round_.decisions
        out.counts["unsure_groups"] += round_.counts["unsure_groups"]
        out.counts["failed_calls"] += int(answers is None)
    out.counts["calls"] = len(batches)
    if judge.memory is not None and out.decisions:
        out.counts["decisions_written"] = await judge.memory.write(TWIN_MEMORY, TWINS_PROMPT, out.decisions, {}, hashes)
    log_event(
        logger,
        INFO,
        "graph.hubjudge.twins",
        twins=len(twins),
        merged=sum(1 for outcome in out.outcomes.values() if outcome == "same"),
        **count_fields(out.counts),
    )
    return out


def _record(candidate: _Candidate, items: Sequence[HubItem]) -> PairCandidate:
    key_a, key_b = _keys(candidate, items)
    return PairCandidate(
        key_a=key_a,
        key_b=key_b,
        part=candidate.part,
        name_cos=round(candidate.name_cos, COSINE_DIGITS),
        rich_cos=round(candidate.rich_cos, COSINE_DIGITS),
        source=candidate.source,
        outcome=candidate.outcome or "different",
    )


async def consolidate_hubs(items: Sequence[HubItem], joiner: HubJoiner, judge: HubJudge) -> HubPassResult:
    """Resolve step 4 over ``items`` (module docstring): blocks per label part (with the candidate
    pairs when the pair pass runs), then the pair pass; ``joiner`` applies the proposals."""

    pair_counts = dict.fromkeys(_PAIR_COUNTERS, 0)
    tally = ConsolidationCounters(items=len(items))
    if len(items) < MIN_ITEMS:
        return HubPassResult(tally.model_copy(update=pair_counts | {"unsure_groups": 0, "decisions_expired": 0}))
    name_unit = await _unit(judge.vectors, [embedding_text(item.label, item.name) for item in items])
    block_list = _part_blocks(items, joiner.part_of, name_unit, judge.params)
    candidates = await _candidates(items, joiner.part_of, name_unit, judge) if judge.params.pair_pass else []
    settled = await _settled_before(items, judge.memory)
    block_log = _AnswerLog()
    tally = await consolidate(
        spec=PassSpec(name=HUBS_PASS, prompt=HUBS_PROMPT),
        items=PassItems(
            keys=[item.key for item in items],
            hashes=[item.hash for item in items],
            unit=name_unit,
            pairs=[candidate.pair for candidate in candidates],
        ),
        block_list=block_list,
        hooks=PassHooks(ask=block_log.ask(_asker(judge, items)), apply=joiner.apply, undecidable=joiner.never),
        memory=judge.memory,
        tally=tally,
    )
    if candidates or judge.memory is not None:
        counts = await _pair_pass(
            candidates,
            block_list,
            items,
            joiner=joiner,
            judge=judge,
            name_unit=name_unit,
            block_log=block_log,
            settled=settled,
        )
        pair_counts = {name: counts[name] for name in _PAIR_COUNTERS}
        tally = tally.model_copy(update={name: getattr(tally, name) + counts[name] for name in _SUMMED})
    if judge.params.twin_pass:
        twins = await _twin_pass(items, joiner, judge)
        tally = tally.model_copy(update={name: getattr(tally, name) + twins.counts[name] for name in _TWIN_COUNTS})
        for candidate in candidates:
            candidate.outcome = twins.outcomes.get(candidate.pair, candidate.outcome)
    records = tuple(_record(candidate, items) for candidate in candidates)
    log_event(logger, INFO, "graph.hubjudge.pairs", candidates=len(records), **count_fields(pair_counts))
    return HubPassResult(tally.model_copy(update=joiner.applied | pair_counts), tuple(joiner.merges), records)
