"""References between units for the edge catalog."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import IntEnum
from pathlib import PurePosixPath
from typing import Final, Literal

from graph.build.models import CanonicalNode, FragmentRec, Unit
from graph.build.prompts import reference_relations, requirement_labels
from graph.build.resolve import hub_surface, hub_value_words, hub_words
from graph.build.terms import NameIndex
from graph.core.identity import STOP_WORDS, explicit_id_pattern, normalise_id
from graph.core.ontology import Ontology

__all__ = [
    "REFERENCED_CAP",
    "Reference",
    "ReferenceFinder",
    "UnitReferences",
    "requirement_labels",
    "section_hub_labels",
]

REFERENCED_CAP: Final = 30
"""Referenced entries per unit at most."""
RANGE_MAX: Final = 20
"""Widest range a plural reference expands (``Sections 7.1 to 7.3``: 3 sections); a wider one gives its ends."""
SENTENCE_BREAK: Final = re.compile(r"(?<=[.!?;])\s+|\n+")
"""Where a fragment's text is cut into sentences: after ``.``, ``!``, ``?`` or ``;`` and a space, or at
a line break (so ``4.2`` stays whole)."""
LITERAL: Final = re.compile(r"`[^`\n]*`|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d")
"""Inline code and double-quoted spans: illustrations and literal values, never references."""
_NUMBER: Final = r"\d+(?:\.\d+)*(?!\d)"
_CHAPTER: Final = r"\d+(?!\.?\d)"
_RANGE_JOIN: Final = r"(?:\s+(?:to|through)\s+|\s*[-\u2013\u2014]\s*)"
_JOIN: Final = rf"(?:\s*,\s*(?:and\s+|or\s+)?|\s+(?:and|or)\s+|{_RANGE_JOIN})"
SECTION_NUMBER: Final = re.compile(rf"(?:\bsection\s+|(?<!§)§(?!§)\s*)(?P<number>{_NUMBER})", re.IGNORECASE)
"""Rule 2, singular: ``section 4.2``, ``§4.2`` or ``§ 4.2``."""
SECTION_NUMBERS: Final = re.compile(
    rf"(?:\bsections\s+|§§\s*)(?P<numbers>{_NUMBER}(?:{_JOIN}(?:sections?\s+|§\s*)?{_NUMBER})*)", re.IGNORECASE
)
"""Rule 2, plural: ``Sections 7.1 and 7.2``, ``Sections 7.1 to 7.3``, ``§§ 7.1-7.3`` (hyphen, en or em dash)."""
CHAPTER_NUMBER: Final = re.compile(rf"\bchapter\s+(?P<number>{_CHAPTER})", re.IGNORECASE)
"""Rule 5, singular: ``Chapter 11``."""
CHAPTER_NUMBERS: Final = re.compile(
    rf"\bchapters\s+(?P<numbers>{_CHAPTER}(?:{_JOIN}(?:chapters?\s+)?{_CHAPTER})*)", re.IGNORECASE
)
"""Rule 5, plural: ``Chapters 7, 9 and 11``, ``Chapters 7 to 9``."""
_LISTED: Final = re.compile(
    rf"(?P<join>{_RANGE_JOIN})?(?:(?:sections?|chapters?)\s+|§\s*)?(?P<number>{_NUMBER})", re.IGNORECASE
)
TITLED: Final = re.compile(
    r"\b(?:see|described\s+in|defined\s+in)\s+(?:the\s+)?(?P<title>[^.;:()]{1,80}?)\s+"
    r"(?P<kind>section|document|specification)\b",
    re.IGNORECASE,
)
"""Rule 3: "see the <Title> section" and its kinds; the title is matched in hub form."""
SECTION_KIND: Final = "section"
HEADING_NUMBER: Final = re.compile(r"^\s*(?:section\s+|§\s*)?(?P<number>\d+(?:\.\d+)*)\.?(?![\d.])\s*", re.IGNORECASE)
"""A heading path element's leading section number (``4.2 Payments``, ``4.2. Payments``, ``§4.2``)."""
TEXT_HEADING: Final = re.compile(
    r"^ {0,3}(?:#{1,6}\s+(?P<markdown>.+?)\s*#*|(?:\*\*)?(?P<numbered>\d+(?:\.\d+)+\.?\s+[A-Z][^|*\n]{0,78}?)"
    r"(?<![.:;,])(?:\*\*)?)\s*$"
)
"""A heading line inside a fragment's text: a markdown heading, or a numbered heading line (``4.2.1 Retries``:
two levels at least, a capitalised title, no closing punctuation)."""
_FENCE: Final = re.compile(r"^ {0,3}(?:```|~~~)")
_DOCUMENT_NOUN: Final = r"(?i:specifications?|specs?|documents?|guides?|handbooks?|manuals?|standards?)"
_FILE: Final = r"[\w./-]*\w\.(?i:pdf|docx?|md|markdown|txt|rtf|odt|pptx?|xlsx?|html?)"
DOCUMENT_AFTER: Final = re.compile(
    rf"^\s+(?i:of|in)\s+(?:(?i:the|this)\s+)?[`'\"*]*(?:(?P<file_name>{_FILE})|(?P<words>(?:[\w-]+\s+){{0,5}}?){_DOCUMENT_NOUN})\b"
)
"""A document named right after a section number: "of the checkout spec", "in `spec.pdf`"."""
DOCUMENT_BEFORE: Final = re.compile(
    rf"(?:(?P<file_name>{_FILE})|(?<![\w-])(?P<words>(?:[A-Z][\w-]*\s+){{1,5}}){_DOCUMENT_NOUN})[`'\"*]*\s*,?\s*$"
)
"""A document named right before a section number: "Checkout specification, section 4.3"."""
DEFINITION_WORDS: Final = frozenset({"glossary", "definition", "terminology", "abbreviation", "acronym"})
"""Heading words (hub form) of a definition section, whose references rank last."""
ILLUSTRATION_WORDS: Final = frozenset({"example"})
"""Heading words (hub form) of an illustration, whose fragments have no references."""
_QUOTES: Final = "\"'`*_\u201c\u201d\u2018\u2019"
_STOP_WORDS: Final = frozenset({"the", "a", "an", "this", "that", "of", "in", "and", "or"})

ReferenceRule = Literal["section_number", "section_title", "explicit_id", "chapter_number"]

_Section = tuple[str, tuple[str, ...]]
"""A section: document id and heading path prefix through the matching element."""
_Sections = dict[_Section, list[str]]


class Rank(IntEnum):
    """The order a unit's entries take before the cap (module docstring)."""

    SECTION = 0
    EXPLICIT_ID = 1
    DEFINITION = 2
    CHAPTER = 3
    SECTION_HUB = 4


@dataclass(frozen=True, slots=True)
class Reference:
    """One reference found in a fragment: its rule, the text matched, the fragments of the section it
    resolves to (rules 2, 3 and 5, plan order) or the node it names (rule 4, ``cids``), and the
    sentence it was found in (stripped)."""

    rule: ReferenceRule
    text: str
    fragment_ids: tuple[str, ...] = ()
    cids: tuple[str, ...] = ()
    sentence: str = ""


@dataclass(frozen=True, slots=True)
class UnitReferences:
    """A unit's referenced entries: ``cids`` (at most ``REFERENCED_CAP``, ranked; the catalog's ``referenced`` part),
    the cids each of its fragments references (uncapped, text order; ``EdgeCatalog.window_catalog``), the entries left
    out by the cap, the references found, by fragment id, and per entry of ``cids`` the first sentence that refers to it
    (fragment order, then text order; the catalog line's ``referenced_in``).
    """

    cids: tuple[str, ...] = ()
    by_fragment: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    capped: int = 0
    found: Mapping[str, tuple[Reference, ...]] = field(default_factory=dict)
    sentences: Mapping[str, str] = field(default_factory=dict)


def section_hub_labels(ontology: Ontology) -> frozenset[str]:
    """The hub labels a section without a requirement offers (module docstring): the hub labels that a relation of the
    reference line (``prompts.reference_relations``: the link and dependency relations) pairs, outside an owner pair,
    with a requirement label or with the same hub label; none when the ontology has no requirement label (references add
    nothing then).
    """

    requirements = requirement_labels(ontology)
    if not requirements:
        return frozenset()
    links, dependencies, _ = reference_relations(ontology)
    hubs = ontology.hub_labels
    found: set[str] = set()
    for name in (*links, *dependencies):
        for pair in ontology.pairs(name):
            if pair.owner:
                continue
            for end, other in ((pair.source, pair.target), (pair.target, pair.source)):
                if end in hubs and (other in requirements or other == end):
                    found.add(end)
    return frozenset(found)


def _form(text: str) -> tuple[str, ...]:
    """A title or heading in hub form: the words of its hub surface, quotes and markup left out."""

    return tuple(hub_value_words(hub_surface(text.strip().strip(_QUOTES).strip())))


def _masked(text: str) -> str:
    """``text`` with its ``LITERAL`` spans blanked, offsets kept."""

    return LITERAL.sub(lambda match: " " * len(match[0]), text)


def _sentences(text: str) -> Iterator[tuple[int, int]]:
    """The ``(start, end)`` offsets of ``text``'s sentences (``SENTENCE_BREAK``)."""

    start = 0
    for match in SENTENCE_BREAK.finditer(text):
        yield start, match.start()
        start = match.end()
    yield start, len(text)


def _numbers(text: str) -> list[str]:
    """The numbers of a plural reference's list, ranges expanded (module docstring, rule 2)."""

    numbers: list[str] = []
    for match in _LISTED.finditer(text):
        number = match["number"]
        if match["join"] and numbers:
            numbers.extend(_range(numbers[-1], number))
        else:
            numbers.append(number)
    return list(dict.fromkeys(numbers))


def _range(start: str, end: str) -> list[str]:
    """The numbers after ``start`` through ``end`` when they differ only in the last part, ascending and at
    most ``RANGE_MAX`` apart; else ``end`` alone."""

    first, last = start.split("."), end.split(".")
    if len(first) != len(last) or first[:-1] != last[:-1]:
        return [end]
    low, high = int(first[-1]), int(last[-1])
    if not low < high <= low + RANGE_MAX:
        return [end]
    return [".".join([*first[:-1], str(value)]) for value in range(low + 1, high + 1)]


def _turns(sequences: Iterable[Sequence[str]]) -> list[str]:
    """The items of ``sequences`` taken in turns (the first of each, then the second, ...), each once."""

    pending = [sequence for sequence in sequences if sequence]
    taken: dict[str, None] = {}
    for index in range(max((len(sequence) for sequence in pending), default=0)):
        for sequence in pending:
            if index < len(sequence):
                taken.setdefault(sequence[index])
    return list(taken)


def _heading_words(fragment: FragmentRec) -> set[str]:
    return {word for element in fragment.heading_path for word in hub_words(element)}


class ReferenceFinder:
    """References of a build's units (module docstring), indexes built once."""

    def __init__(
        self,
        *,
        units: Sequence[Unit],
        fragments_by_id: Mapping[str, FragmentRec],
        titles: Mapping[str, str],
        canonical: Sequence[CanonicalNode],
        labels: Collection[str],
        id_families: Collection[str] = (),
        hub_labels: Collection[str] = (),
        cap: int = REFERENCED_CAP,
    ) -> None:
        self._fragments = fragments_by_id
        self._cap = max(cap, 0)
        self._unit_of = {fid: unit.unit_id for unit in units for fid in unit.fragment_ids}
        self._order = {fid: index for index, fid in enumerate(fid for unit in units for fid in unit.fragment_ids)}
        self._titles = NameIndex((title, document_id) for document_id, title in sorted(titles.items()) if title.strip())
        self._title_forms: dict[tuple[str, ...], set[str]] = {}
        self._title_words: dict[str, frozenset[str]] = {}
        for document_id, title in titles.items():
            if _form(title):
                self._title_forms.setdefault(_form(title), set()).add(document_id)
                self._title_words[document_id] = frozenset(_form(title))
        self._files: dict[str, set[str]] = {}
        for document_id in titles:
            self._files.setdefault(PurePosixPath(document_id).name.casefold(), set()).add(document_id)
        self._by_number: dict[str, _Sections] = {}
        self._by_title: dict[tuple[str, ...], _Sections] = {}
        self._by_chapter: dict[str, _Sections] = {}
        merged_numbers: dict[str, _Sections] = {}
        merged_titles: dict[tuple[str, ...], _Sections] = {}
        for fragment_id in sorted(self._order, key=self._order.__getitem__):
            fragment = fragments_by_id.get(fragment_id)
            if fragment is not None:
                self._index_headings(fragment)
                self._index_text_headings(fragment, merged_numbers, merged_titles)
        _add_missing(self._by_number, merged_numbers)
        _add_missing(self._by_title, merged_titles)
        ordered = sorted(canonical, key=lambda node: node.cid)
        self._requirements: dict[str, list[str]] = {}
        self._hubs: dict[str, list[str]] = {}
        self._grounded_units: dict[str, set[str]] = {}
        self._by_id: dict[str, list[str]] = {}
        self._words: dict[str, frozenset[str]] = {}
        for node in ordered:
            units_of = {self._unit_of[fid] for fid in node.fragment_ids if fid in self._unit_of}
            self._grounded_units[node.cid] = units_of
            grounded = self._requirements if node.label in labels else self._hubs if node.label in hub_labels else None
            if grounded is not None:
                self._words[node.cid] = _content_words(" ".join([node.name, *(quote.quote for quote in node.quotes)]))
                for fragment_id in dict.fromkeys(node.fragment_ids):
                    grounded.setdefault(fragment_id, []).append(node.cid)
            for explicit in node.explicit_ids:
                self._by_id.setdefault(normalise_id(explicit), []).append(node.cid)
        self._ids = explicit_id_pattern(sorted(id_families)) if self._by_id else None
        counts = Counter(word for words in self._words.values() for word in words)
        self._weight = {word: math.log((1 + len(self._words)) / (1 + count)) for word, count in counts.items()}

    def _index_headings(self, fragment: FragmentRec) -> None:
        path = fragment.heading_path
        chapter_seen = False
        for depth, element in enumerate(path):
            section = (fragment.document_id, path[: depth + 1])
            numbered = HEADING_NUMBER.match(element)
            if numbered:
                number = numbered["number"]
                self._by_number.setdefault(number, {}).setdefault(section, []).append(fragment.fragment_id)
                if not chapter_seen and "." not in number:
                    self._by_chapter.setdefault(number, {}).setdefault(section, []).append(fragment.fragment_id)
                chapter_seen = True
            form = _form(element[numbered.end() :] if numbered else element)
            if form:
                self._by_title.setdefault(form, {}).setdefault(section, []).append(fragment.fragment_id)

    def _index_text_headings(
        self,
        fragment: FragmentRec,
        numbers: dict[str, _Sections],
        titles: dict[tuple[str, ...], _Sections],
    ) -> None:
        """Heading lines inside the fragment's text (outside code fences) that its heading path does not
        hold: merged subsections (module docstring, rule 2)."""

        path_numbers, path_forms = set(), set()
        for element in fragment.heading_path:
            numbered = HEADING_NUMBER.match(element)
            if numbered:
                path_numbers.add(numbered["number"])
            path_forms.add(_form(element[numbered.end() :] if numbered else element))
        fenced = False
        for line in fragment.text.split("\n"):
            if _FENCE.match(line):
                fenced = not fenced
                continue
            heading = None if fenced else TEXT_HEADING.match(line)
            if heading is None:
                continue
            element = (heading["markdown"] or heading["numbered"]).strip("*").strip()
            numbered = HEADING_NUMBER.match(element)
            form = _form(element[numbered.end() :] if numbered else element)
            if (numbered and numbered["number"] in path_numbers) or form in path_forms:
                continue
            section = (fragment.document_id, (*fragment.heading_path, element))
            if numbered:
                numbers.setdefault(numbered["number"], {}).setdefault(section, []).append(fragment.fragment_id)
            if form:
                titles.setdefault(form, {}).setdefault(section, []).append(fragment.fragment_id)

    def for_unit(self, unit: Unit) -> UnitReferences:
        """The unit's references (module docstring): entries ranked, fragments and references taking
        turns within a rank, each once; capped."""

        found: dict[str, tuple[Reference, ...]] = {}
        by_fragment: dict[str, tuple[str, ...]] = {}
        ranked: dict[Rank, list[list[str]]] = {}
        sentences: dict[str, str] = {}
        for fragment_id in unit.fragment_ids:
            fragment = self._fragments.get(fragment_id)
            if fragment is None:
                continue
            references = tuple(self.in_fragment(fragment, unit.unit_id))
            if references:
                found[fragment_id] = references
            definition = bool(_heading_words(fragment) & DEFINITION_WORDS)
            entries_of = [
                self._ranked_entries(reference, unit.unit_id, definition=definition) for reference in references
            ]
            for reference, (_, entries) in zip(references, entries_of, strict=True):
                for cid in entries:
                    sentences.setdefault(cid, reference.sentence)
            cids = list(dict.fromkeys(cid for _, entries in entries_of for cid in entries))
            if cids:
                by_fragment[fragment_id] = tuple(cids)
            for rank in sorted({rank for rank, _ in entries_of}):
                ranked.setdefault(rank, []).append(_turns(entries for of, entries in entries_of if of == rank))
        entries = list(dict.fromkeys(cid for rank in sorted(ranked) for cid in _turns(ranked[rank])))
        kept = tuple(entries[: self._cap])
        return UnitReferences(
            cids=kept,
            by_fragment=by_fragment,
            capped=max(len(entries) - self._cap, 0),
            found=found,
            sentences={cid: sentences[cid] for cid in kept},
        )

    def _ranked_entries(self, reference: Reference, unit_id: str, *, definition: bool) -> tuple[Rank, list[str]]:
        """The rank and the cids a reference adds to unit ``unit_id``: its nodes, or the requirements of
        its fragments, else the hubs of its fragments (``Rank.SECTION_HUB``); none grounded in that unit."""

        rank = _rank(reference, definition=definition)
        if reference.cids:
            return rank, self._outside(reference.cids, unit_id)
        requirements = self._outside(self._grounded_in(self._requirements, reference.fragment_ids), unit_id)
        if requirements:
            return rank, self._by_overlap(requirements, reference.sentence)
        hubs = self._outside(self._grounded_in(self._hubs, reference.fragment_ids), unit_id)
        return Rank.SECTION_HUB, self._by_overlap(hubs, reference.sentence)

    def _by_overlap(self, cids: Iterable[str], sentence: str) -> list[str]:
        """``cids`` (each once) by their overlap with the referring ``sentence``, best first: the summed weight of the
        content words (``_content_words``) a node's name and quotes share with the sentence, each word weighted by its
        rarity among the indexed nodes; ties keep plan order.
        """

        unique = list(dict.fromkeys(cids))
        words = _content_words(sentence)
        score = {
            cid: sum(self._weight.get(word, 0.0) for word in words & self._words.get(cid, frozenset()))
            for cid in unique
        }
        return sorted(unique, key=lambda cid: -score[cid])

    @staticmethod
    def _grounded_in(index: Mapping[str, Sequence[str]], fragment_ids: Iterable[str]) -> list[str]:
        return [cid for fragment_id in fragment_ids for cid in index.get(fragment_id, ())]

    def _outside(self, cids: Iterable[str], unit_id: str) -> list[str]:
        return [cid for cid in cids if unit_id not in self._grounded_units.get(cid, ())]

    def in_fragment(self, fragment: FragmentRec, unit_id: str) -> list[Reference]:
        """The references in one fragment of ``unit_id``, in text order; unresolved and ambiguous ones
        left out, none in an illustration."""

        if _heading_words(fragment) & ILLUSTRATION_WORDS:
            return []
        masked = _masked(fragment.text)
        found: list[Reference] = []
        for start, end in _sentences(fragment.text):
            sentence = fragment.text[start:end]
            if sentence.strip():
                in_sentence = self._in_sentence(sentence, masked[start:end], fragment, unit_id)
                found.extend(
                    replace(reference, sentence=sentence.strip())
                    for _, reference in sorted(in_sentence, key=lambda item: item[0])
                )
        return found

    def _in_sentence(
        self, sentence: str, masked: str, fragment: FragmentRec, unit_id: str
    ) -> list[tuple[int, Reference]]:
        """The references of one sentence with their offsets (rules 2 to 5, scoped by rules 1 and 3 and
        the documents named next to a number); ``masked``: the sentence with its literals blanked."""

        document_id = fragment.document_id
        scope = {found for found in self._titles.count(hub_words(sentence)) if found != document_id}
        titled = list(TITLED.finditer(sentence))
        for match in titled:
            if match["kind"].lower() != SECTION_KIND:
                scope.update(self._title_forms.get(_form(match["title"]), set()) - {document_id})
        found = self._numbered(sentence, masked, scope, fragment.document_id, unit_id, chapters=False)
        found += self._numbered(sentence, masked, scope, fragment.document_id, unit_id, chapters=True)
        for match in titled:
            if match["kind"].lower() == SECTION_KIND:
                sections = self._by_title.get(_form(match["title"]), {})
                hits = self._resolve(sections, scope, document_id, unit_id, same_document=True)
                if hits:
                    found.append((match.start(), Reference("section_title", match[0].strip(), fragment_ids=hits)))
        found.extend(self._ids_in(masked, unit_id))
        return found

    def _numbered(
        self, sentence: str, masked: str, scope: set[str], document_id: str, unit_id: str, *, chapters: bool
    ) -> list[tuple[int, Reference]]:
        """Rule 2, or rule 5 with ``chapters``: each number of each match in ``masked``, scoped by
        ``scope`` and by a document named next to it (module docstring), with its offset."""

        singular, plural = (CHAPTER_NUMBER, CHAPTER_NUMBERS) if chapters else (SECTION_NUMBER, SECTION_NUMBERS)
        index = self._by_chapter if chapters else self._by_number
        rule: ReferenceRule = "chapter_number" if chapters else "section_number"
        matches = [(match, [match["number"]]) for match in singular.finditer(masked)]
        matches += [(match, _numbers(match["numbers"])) for match in plural.finditer(masked)]
        found: list[tuple[int, Reference]] = []
        for match, numbers in matches:
            named = self._named_document(sentence[: match.start()], sentence[match.end() :])
            if named is not None and not named:
                continue
            where = scope | (named or set())
            for number in numbers:
                hits = self._resolve(index.get(number, {}), where, document_id, unit_id, same_document=not chapters)
                if hits:
                    found.append((match.start(), Reference(rule, match[0].strip(), fragment_ids=hits)))
        return found

    def _named_document(self, before: str, after: str) -> set[str] | None:
        """The documents a name right before or after a number names (module docstring): None without a
        name, an empty set for a document outside the corpus."""

        match = DOCUMENT_AFTER.match(after) or DOCUMENT_BEFORE.search(before)
        if match is None:
            return None
        if match["file_name"]:
            name = PurePosixPath(match["file_name"]).name.casefold()
            return set(self._files.get(name, ()))
        words = {word for word in hub_words(match["words"]) if word not in _STOP_WORDS}
        if not words:
            return None
        return {document for document, title in self._title_words.items() if words <= title}

    def _resolve(
        self,
        sections: Mapping[_Section, Sequence[str]],
        scope: Collection[str],
        document_id: str,
        unit_id: str,
        *,
        same_document: bool,
    ) -> tuple[str, ...]:
        """The fragments of the one matching section outside ``unit_id`` in the first tier with any:
        the scoped documents, the other documents, the same document (when ``same_document``); () when
        that tier holds two or more sections, or none has any."""

        outside = {
            section: [fid for fid in fragment_ids if self._unit_of.get(fid) not in (None, unit_id)]
            for section, fragment_ids in sections.items()
            if same_document or section[0] != document_id
        }
        outside = {section: fragment_ids for section, fragment_ids in outside.items() if fragment_ids}
        if not outside:
            return ()

        def tier(section: _Section) -> int:
            return 0 if section[0] in scope else 2 if section[0] == document_id else 1

        first = min(tier(section) for section in outside)
        matches = [section for section in outside if tier(section) == first]
        if len(matches) > 1:
            return ()
        return tuple(sorted(dict.fromkeys(outside[matches[0]]), key=self._order.__getitem__))

    def _ids_in(self, sentence: str, unit_id: str) -> list[tuple[int, Reference]]:
        """Rule 4: the explicit ids in ``sentence`` of nodes grounded in another unit and not in
        ``unit_id``, with their offsets."""

        if self._ids is None:
            return []
        found: list[tuple[int, Reference]] = []
        for match in self._ids.finditer(sentence):
            cids = tuple(
                cid
                for cid in self._by_id.get(normalise_id(match[0]), ())
                if self._grounded_units.get(cid) and unit_id not in self._grounded_units[cid]
            )
            if cids:
                found.append((match.start(), Reference("explicit_id", match[0], cids=cids)))
        return found


def _content_words(text: str) -> frozenset[str]:
    """The words of ``text`` in hub form (``hub_words``) without stop words and bare numbers: what a
    referring sentence and a candidate node are compared on (``ReferenceFinder._by_overlap``)."""

    return frozenset(word for word in hub_words(text) if word not in STOP_WORDS and not word.isdigit())


def _rank(reference: Reference, *, definition: bool) -> Rank:
    """A reference's rank (module docstring); ``definition``: its fragment is in a definition section."""

    if reference.rule == "chapter_number":
        return Rank.CHAPTER
    if definition:
        return Rank.DEFINITION
    return Rank.EXPLICIT_ID if reference.rule == "explicit_id" else Rank.SECTION


def _add_missing[K](index: dict[K, _Sections], extra: Mapping[K, _Sections]) -> None:
    """Add the merged-heading sections of ``extra`` for the documents ``index`` has none of under that key."""

    for key, sections in extra.items():
        present = {section[0] for section in index.get(key, {})}
        for section, fragment_ids in sections.items():
            if section[0] not in present:
                index.setdefault(key, {})[section] = fragment_ids
