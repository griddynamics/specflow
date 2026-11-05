"""Known terms for node extraction: hub catalog, fragment vectors and per-unit selection."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np

from graph.build.changes import EdgeWindowParams, NearnessMode
from graph.build.models import (
    CanonicalNode,
    CatalogParts,
    FragmentRec,
    HubName,
    KnownTermsShown,
    RawNode,
    Unit,
    Window,
)
from graph.build.prompts import BREADCRUMB_SEPARATOR, KNOWN_TERM_ALIASES_SHOWN, KnownTerm
from graph.build.queries import READ_PERSISTED_HUBS
from graph.build.resolve import hub_surface, hub_value_words, hub_words, surface_values, tokens
from graph.build.vectors import Vector, VectorCache
from graph.core.db import Executor
from graph.core.identity import STOP_WORDS
from graph.core.ontology import Ontology
from logger.logger import WARNING, get_logger, log_event

logger = get_logger("graph.build.terms")

CATALOG_NEAREST: Final = 20
"""Closest foreign hubs per fragment considered for an edge catalog."""
CATALOG_MIN_COSINE: Final = 0.40
"""Minimum cosine for a foreign hub to enter an edge catalog by nearness."""
CATALOG_RELATIVE_COSINE: Final = 0.8
"""Share of a fragment's best foreign-hub cosine a hub must also reach to enter the catalog."""
CATALOG_GROUP_MAX: Final = 4
"""Hub-surface groups one catalog hub may join by alias links at most (``merge_catalog``)."""
FULL_NAME_RATIO: Final = 2
"""A linked full name beats an abbreviation given up to this many times as often (``merge_catalog``)."""
ALIAS_MIN_CHARS: Final = 2
"""An alias of at most this many characters is dropped (``weak_alias``)."""
GENERIC_ALIAS_WORDS: Final = frozenset(
    {"service", "system", "app", "application", "platform", "component", "tool", "it", "this", "that", "they"}
)
"""Generic words: an alias made only of them (and stop words) names no hub ("the service")."""
MENTIONED_TERMS_MAX: Final = 100
"""Safety cap on the mentioned known terms of one unit (the most mentioned first); hitting it is logged
and counted (``TermRanker.capped_units``)."""
_ALIAS_KEY: Final = ":"
"""Separates a term's position and an alias's position in a ``KnownTerms`` index key."""


# Fragment vectors


async def fragment_vectors(fragments: Sequence[FragmentRec], vectors: VectorCache) -> dict[str, np.ndarray]:
    """Embedding of each fragment's stripped text by fragment id, for ranking only, from the vector
    store (``graph.build.vectors``; reused by content across builds); each distinct text is embedded
    at most once. A fragment without text (all lines masked) gets no vector."""

    texts = {fragment.fragment_id: fragment.text.strip() for fragment in fragments if fragment.text.strip()}
    if not texts:
        return {}
    return dict(zip(texts, await vectors.get_or_embed(list(texts.values())), strict=True))


async def hub_vectors(hubs: Sequence[HubName], vectors: VectorCache) -> dict[str, np.ndarray]:
    """Embedding of each distinct hub name, by name (vector store first)."""

    names = list(dict.fromkeys(hub.name for hub in hubs if hub.name.strip()))
    if not names:
        return {}
    return dict(zip(names, await vectors.get_or_embed(names), strict=True))


# Persisted hubs (update builds)


async def persisted_hubs(executor: Executor, project_id: str, ontology: Ontology) -> list[HubName]:
    """Known-term hubs of an ``update`` build: the name and label of every persisted spec node of a hub label, sorted by
    name and label; names equal after whitespace folding count once.
    """

    labels = sorted(ontology.hub_labels)
    if not labels:
        return []
    rows = await executor.read(READ_PERSISTED_HUBS, project_id, labels=labels)
    hubs: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        name = " ".join(str(row.get("name") or "").split())
        label = str(row.get("label") or "")
        if name and label in ontology.hub_labels:
            aliases = hubs.setdefault((name, label), [])
            keys = [str(key) for key in row.get("aliases") or () if key]
            aliases.extend(value for value in surface_values(keys) if value not in aliases)
    return [
        HubName(name=name, label=label, aliases=tuple(sorted(aliases)))
        for (name, label), aliases in sorted(hubs.items())
    ]


# Hub catalog (from the hub pass)


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One hub item of the hub pass as the catalog merge reads it: name, label, the other names it
    gave and the document of its unit."""

    name: str
    label: str
    aliases: tuple[str, ...] = ()
    document_id: str = ""


@dataclass(frozen=True, slots=True)
class CatalogMerge:
    """The merged hub catalog (sorted by name and label) and the merge's counts."""

    hubs: tuple[HubName, ...] = ()
    links_refused: int = 0
    """Alias links refused because the joined component would hold more than ``CATALOG_GROUP_MAX`` names."""
    aliases_dropped: int = 0
    """Aliases dropped: another label's hub name, a hub name outside the component, or claimed by
    two or more components."""

    @property
    def aliases(self) -> int:
        return sum(len(hub.aliases) for hub in self.hubs)


def _fold(text: str) -> str:
    return " ".join(text.split())


@dataclass
class _CatalogGroup:
    """The items of one label and hub surface: name variants with their counts and documents, and the
    other names they gave by surface."""

    variants: Counter[str] = field(default_factory=Counter)
    documents: dict[str, set[str]] = field(default_factory=dict)
    aliases: dict[str, set[str]] = field(default_factory=dict)
    alias_items: Counter[str] = field(default_factory=Counter)
    """Items that gave each alias surface."""


class _Components:
    """Union-find over catalog groups that refuses a component of more than ``limit`` groups."""

    def __init__(self, keys: Iterable[tuple[str, str]], limit: int) -> None:
        self._parent = {key: key for key in keys}
        self._size = dict.fromkeys(self._parent, 1)
        self._limit = limit

    def find(self, key: tuple[str, str]) -> tuple[str, str]:
        while self._parent[key] != key:
            self._parent[key] = self._parent[self._parent[key]]
            key = self._parent[key]
        return key

    def join(self, first: tuple[str, str], second: tuple[str, str]) -> bool:
        """Join the two components; False (nothing joined) when the result would be too large."""

        one, two = self.find(first), self.find(second)
        if one == two:
            return True
        if self._size[one] + self._size[two] > self._limit:
            return False
        low, high = sorted((one, two))
        self._parent[high] = low
        self._size[low] += self._size.pop(high)
        return True


def _variant_rank(group: _CatalogGroup) -> Callable[[str], tuple[int, int, str]]:
    return lambda name: (-group.variants[name], -len(group.documents.get(name, ())), name)


def merge_catalog(entries: Iterable[CatalogEntry], labels: Collection[str]) -> CatalogMerge:
    """The hub catalog of ``entries``: pure and deterministic."""

    groups, weak = _catalog_groups(entries, labels)
    dropped = weak + _drop_other_label_aliases(groups)
    members, refused = _linked(groups)
    kept, more = _kept_aliases(groups, members)
    hubs = [
        _merged_hub(root, {key[1]: groups[key] for key in keys}, kept.get(root, {})) for root, keys in members.items()
    ]
    return CatalogMerge(
        hubs=tuple(sorted(hubs, key=lambda hub: (hub.name, hub.label))),
        links_refused=refused,
        aliases_dropped=dropped + more,
    )


_GroupKey = tuple[str, str]
"""A catalog group: label and hub surface."""


def weak_alias(text: str) -> bool:
    """Whether an alias is too weak to name a hub: at most ``ALIAS_MIN_CHARS`` characters, or only
    stop words (``identity.STOP_WORDS``) and generic words (``GENERIC_ALIAS_WORDS``)."""

    words = tokens(text)
    return len(text.strip()) <= ALIAS_MIN_CHARS or all(
        word in STOP_WORDS or word in GENERIC_ALIAS_WORDS for word in words
    )


_NOT_WORD: Final = re.compile(r"[\W_]+")


def squashed(surface: str) -> str:
    """A hub surface without spaces and punctuation: equal for one name written with and without them."""

    return _NOT_WORD.sub("", surface)


def _catalog_groups(
    entries: Iterable[CatalogEntry], labels: Collection[str]
) -> tuple[dict[_GroupKey, _CatalogGroup], int]:
    """The entries by label and hub surface (folded; empty names, unknown labels and self aliases left
    out) and the number of weak aliases dropped (``weak_alias``)."""

    groups: dict[_GroupKey, _CatalogGroup] = {}
    weak = 0
    for entry in entries:
        name = _fold(entry.name)
        surface = hub_surface(name) if name else ""
        if not surface or entry.label not in labels:
            continue
        group = groups.setdefault((entry.label, surface), _CatalogGroup())
        group.variants[name] += 1
        documents = group.documents.setdefault(name, set())
        if entry.document_id:
            documents.add(entry.document_id)
        for alias in entry.aliases:
            text = _fold(alias)
            alias_surface = hub_surface(text) if text else ""
            if not alias_surface or alias_surface == surface:
                continue
            if weak_alias(text):
                weak += 1
                continue
            group.aliases.setdefault(alias_surface, set()).add(text)
            group.alias_items[alias_surface] += 1
    return groups, weak


def _drop_other_label_aliases(groups: Mapping[_GroupKey, _CatalogGroup]) -> int:
    """Drop every alias naming a group of another label; returns how many."""

    labels_of: dict[str, set[str]] = {}
    for label, surface in groups:
        labels_of.setdefault(surface, set()).add(label)
    dropped = 0
    for (label, _), group in groups.items():
        for alias_surface in sorted(group.aliases):
            if labels_of.get(alias_surface, set()) - {label}:
                del group.aliases[alias_surface]
                dropped += 1
    return dropped


def _linked(groups: Mapping[_GroupKey, _CatalogGroup]) -> tuple[dict[_GroupKey, list[_GroupKey]], int]:
    """The components joined by alias links and by spacing (sorted links, a too-large join refused): the groups of each
    component by its root, and the refused links.
    """

    components = _Components(sorted(groups), CATALOG_GROUP_MAX)
    alias_links = {
        ((label, surface), (label, alias_surface))
        for (label, surface), group in groups.items()
        for alias_surface in group.aliases
        if (label, alias_surface) in groups
    }
    by_squash: dict[tuple[str, str], list[_GroupKey]] = {}
    for label, surface in sorted(groups):
        if squashed(surface):
            by_squash.setdefault((label, squashed(surface)), []).append((label, surface))
    spacing_links = {(keys[0], other) for keys in by_squash.values() for other in keys[1:]}
    links = sorted(alias_links | spacing_links)
    refused = sum(1 for first, second in links if not components.join(first, second))
    members: dict[_GroupKey, list[_GroupKey]] = {}
    for key in sorted(groups):
        members.setdefault(components.find(key), []).append(key)
    return members, refused


def _kept_aliases(
    groups: Mapping[_GroupKey, _CatalogGroup], members: Mapping[_GroupKey, Sequence[_GroupKey]]
) -> tuple[dict[_GroupKey, dict[str, str]], int]:
    """The aliases each component keeps (lowest text per surface) and the number dropped: an alias
    naming a group outside its component, or claimed by two or more components."""

    dropped = 0
    claims: dict[str, dict[_GroupKey, set[str]]] = {}
    for root, keys in members.items():
        own = {surface for _, surface in keys}
        for key in keys:
            for alias_surface, texts in groups[key].aliases.items():
                if alias_surface in own:
                    continue
                if (key[0], alias_surface) in groups:
                    dropped += 1  # another hub's name, its link refused
                    continue
                claims.setdefault(alias_surface, {}).setdefault(root, set()).update(texts)
    kept: dict[_GroupKey, dict[str, str]] = {}
    for alias_surface, by_root in claims.items():
        if len(by_root) > 1:
            dropped += len(by_root)
            continue
        ((root, texts),) = by_root.items()
        kept.setdefault(root, {})[alias_surface] = min(texts)
    return kept, dropped


def _full_name(members: Mapping[str, _CatalogGroup], name: str, rank: Callable[[str], Any]) -> str:
    """``name``, or a fuller name: of the member groups whose items link the others by alias (an abbreviation's full
    name), longer than ``name`` and whose items and links reach ``1 / FULL_NAME_RATIO`` of the items of ``name``'s
    group, the shortest name (then the most items and links, then the lowest); ``name`` when none qualifies.
    """

    own = {surface: sum(group.variants.values()) for surface, group in members.items()}
    floor = own[hub_surface(name)]
    words = len(tokens(name))
    fuller = []
    for surface, group in members.items():
        credit = sum(count for linked, count in group.alias_items.items() if linked in members and linked != surface)
        best = min(group.variants, key=rank)
        weight = own[surface] + credit
        if credit and len(tokens(best)) > words and weight * FULL_NAME_RATIO >= floor:
            fuller.append((len(tokens(best)), len(best), -weight, best))
    return min(fuller)[3] if fuller else name


def _merged_hub(root: tuple[str, str], members: Mapping[str, _CatalogGroup], aliases: Mapping[str, str]) -> HubName:
    """One catalog hub from the groups of a component (by hub surface) and its kept aliases (by surface)."""

    joined = _CatalogGroup()
    for group in members.values():
        joined.variants.update(group.variants)
        for name, documents in group.documents.items():
            joined.documents.setdefault(name, set()).update(documents)
    rank = _variant_rank(joined)
    name = _full_name(members, min(joined.variants, key=rank), rank)
    surfaces: dict[str, str] = {}
    for group in members.values():
        best = min(group.variants, key=rank)
        surfaces.setdefault(hub_surface(best), best)
    others = [text for surface, text in surfaces.items() if surface != hub_surface(name)]
    others += [text for surface, text in aliases.items() if surface not in surfaces]
    return HubName(
        name=name,
        label=root[0],
        aliases=tuple(sorted(set(others))),
        document_ids=tuple(sorted({document for documents in joined.documents.values() for document in documents})),
    )


def catalog_from_hub_pass(
    raw_nodes: Iterable[RawNode], document_of: Mapping[str, str], labels: Collection[str]
) -> CatalogMerge:
    """The hub catalog of a build : ``merge_catalog`` over the raw nodes of the hub
    pass (answered and reused) of ``labels``, each with its aliases and the document of its unit
    (``document_of``: unit id to document id)."""

    return merge_catalog(
        (
            CatalogEntry(raw.name, raw.label, raw.aliases, document_of.get(raw.unit_id, ""))
            for raw in raw_nodes
            if raw.label in labels
        ),
        labels,
    )


def _hub_surfaces(hub: HubName) -> set[str]:
    """The hub surfaces of a hub: its name's and each alias's (an alias also as written, since a
    persisted alias is already a surface value)."""

    found = {hub_surface(hub.name)}
    for alias in hub.aliases:
        found.update({hub_surface(alias), alias})
    found.discard("")
    return found


def with_catalog(persisted: Sequence[HubName], catalog: Sequence[HubName]) -> tuple[list[HubName], int]:
    """Known-term hubs of an ``update`` build: the persisted hubs merged with the catalog, sorted by name and label, and
    the number of ambiguous catalog hubs.
    """

    hubs = [HubName(name=hub.name, label=hub.label, aliases=tuple(hub.aliases)) for hub in persisted]
    index: dict[tuple[str, str], list[int]] = {}
    for position, hub in enumerate(hubs):
        for surface in _hub_surfaces(hub):
            index.setdefault((hub.label, surface), []).append(position)
    added: list[HubName] = []
    ambiguous = 0
    for hub in catalog:
        matches = sorted({found for surface in _hub_surfaces(hub) for found in index.get((hub.label, surface), ())})
        if len(matches) > 1:
            ambiguous += 1
        elif matches:
            target = hubs[matches[0]]
            have = _hub_surfaces(target)
            extra = [name for name in (hub.name, *hub.aliases) if hub_surface(name) not in have]
            hubs[matches[0]] = target.model_copy(update={"aliases": (*target.aliases, *dict.fromkeys(extra))})
        else:
            added.append(hub)
    return sorted([*hubs, *added], key=lambda hub: (hub.name, hub.label)), ambiguous


# Name occurrences


class NameIndex:
    """Whole-word occurrences of names in token streams, for many names at once."""

    def __init__(self, entries: Iterable[tuple[str, str]], values: Iterable[tuple[str, str]] = ()) -> None:
        self._keys: dict[tuple[str, ...], list[str]] = {}
        needles = [(needle, key) for name, key in entries for needle in _name_needles(name)]
        needles += [(tuple(hub_value_words(value)), key) for value, key in values]
        for needle, key in needles:
            if not needle:
                continue
            keys = self._keys.setdefault(needle, [])
            if key not in keys:
                keys.append(key)
        lengths: dict[str, set[int]] = {}
        for needle in self._keys:
            lengths.setdefault(needle[0], set()).add(len(needle))
        self._lengths = {first: sorted(sizes, reverse=True) for first, sizes in lengths.items()}

    def __bool__(self) -> bool:
        return bool(self._keys)

    def count(self, words: Sequence[str]) -> Counter[str]:
        """Occurrences per key in ``words`` (longest name first, occurrences never overlap)."""

        counts: Counter[str] = Counter()
        size = len(words)
        index = 0
        while index < size:
            step = 1
            for length in self._lengths.get(words[index], ()):
                if index + length > size:
                    continue
                keys = self._keys.get(tuple(words[index : index + length]))
                if keys:
                    for key in keys:
                        counts[key] += 1
                    step = length
                    break
            index += step
        return counts


def _name_needles(name: str) -> tuple[tuple[str, ...], ...]:
    """The word sequences of a hub name in hub form (``NameIndex``), each once."""

    surface = hub_surface(name)
    return tuple(dict.fromkeys((tuple(hub_value_words(surface)), tuple(hub_words(surface)))))


def hub_name_index(hubs: Iterable[tuple[str, str, Sequence[str]]]) -> NameIndex:
    """The ``NameIndex`` of hub mentions (``MENTIONED_IN``, hub statements and the attributes pass): ``(key, name,
    identity keys)`` per hub, found by its name and by the value of each ``surface`` key among its identity keys (its
    own aliases and those of the persisted node it continues).
    """

    listed = [(key, name, list(keys)) for key, name, keys in hubs]
    return NameIndex(
        ((name, key) for key, name, _ in listed),
        ((value, key) for key, _, keys in listed for value in surface_values(keys)),
    )


def named_in(index: NameIndex, fragments: Iterable[FragmentRec]) -> dict[str, dict[str, int]]:
    """Per key of ``index``: fragment id to the whole-word occurrences of its names there, in fragment
    order (``NameIndex.count``: longest name first). Each fragment's tokens are read once."""

    result: dict[str, dict[str, int]] = {}
    if not index:
        return result
    for fragment in fragments:
        counts = index.count(fragment_words(fragment))
        for key in sorted(counts):
            result.setdefault(key, {})[fragment.fragment_id] = counts[key]
    return result


_EXACT_WORD: Final = re.compile(r"\w+")


def exact_words(text: str) -> list[str]:
    """The words of ``text`` as written (NFKC, case kept), for case-exact one-word alias matches."""

    return _EXACT_WORD.findall(unicodedata.normalize("NFKC", text))


def fragment_words(fragment: FragmentRec) -> list[str]:
    """The token stream of a fragment's text in hub form (``resolve.hub_words``), for ``NameIndex``."""

    return hub_words(fragment.text)


_HEADING_LINE: Final = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(?P<title>.*?))?[ \t#]*$")
_FENCE_LINE: Final = re.compile(r"^ {0,3}(?:```|~~~)(?![`~]*[ \t]*[*_]+[ \t]*$)")  # "```*" closes italic inline code


def body_lines(text: str) -> tuple[str, tuple[str, ...]]:
    """A fragment text split into its body (every line but the markdown heading lines outside fenced code, joined by
    line breaks) and the titles of those heading lines, in order (``EdgeCatalog``: a heading line repeats its section's
    heading, so a hub it names is named in a heading).
    """

    body: list[str] = []
    titles: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE_LINE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING_LINE.match(line)
        if heading is None:
            body.append(line)
        elif heading["title"]:
            titles.append(heading["title"])
    return "\n".join(body), tuple(titles)


def heading_section(heading_path: Sequence[str], lines: Sequence[str]) -> str:
    """The section a line of a fragment sits in, joined by ``BREADCRUMB_SEPARATOR``: the fragment's heading path, then
    the markdown headings of its text above the line (``lines``: the fragment's lines up to it, or only its heading and
    code fence lines) still open there, outside fenced code; a heading already on the path (the fragment's own heading
    line) is not repeated.
    """

    path = [part.strip() for part in heading_path if part.strip()]
    open_headings: list[tuple[int, str]] = []
    fenced = False
    for line in lines:
        if _FENCE_LINE.match(line):
            fenced = not fenced
            continue
        heading = None if fenced else _HEADING_LINE.match(line)
        if heading is None or not heading["title"]:
            continue
        level = len(line.strip().split(maxsplit=1)[0])
        while open_headings and open_headings[-1][0] >= level:
            open_headings.pop()
        open_headings.append((level, heading["title"].strip()))
    path.extend(title for _, title in open_headings if title not in path)
    return BREADCRUMB_SEPARATOR.join(path)


def _unit_rows(unit: Unit, fragment_vectors: Mapping[str, Vector]) -> np.ndarray | None:
    rows = [fragment_vectors[fragment_id] for fragment_id in unit.fragment_ids if fragment_id in fragment_vectors]
    return _normalised_rows(rows) if rows else None


def _normalised_rows(rows: Sequence[Vector]) -> np.ndarray:
    matrix = np.asarray(rows, dtype=float)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalised: np.ndarray = matrix / norms
    return normalised


# Known terms


@dataclass(frozen=True, slots=True)
class _Term:
    name: str
    label: str
    aliases: tuple[str, ...]


class KnownTerms:
    """Known-term selection over one hub list: the name index and the hub vectors, built once."""

    def __init__(self, hubs: Sequence[HubName], hub_vectors: Mapping[str, Vector]) -> None:
        self.capped_units = 0
        """Units whose mentioned terms hit ``MENTIONED_TERMS_MAX``."""
        found: dict[tuple[str, str], tuple[str, list[str]]] = {}
        for hub in sorted(hubs, key=lambda item: (item.name, item.label)):
            surface = hub_surface(hub.name)
            if not surface:
                continue
            aliases = found.setdefault((surface, hub.label), (hub.name, []))[1]
            aliases.extend(alias for alias in hub.aliases if alias not in aliases and hub_surface(alias) != surface)
        self._terms = [_Term(name, label, tuple(aliases)) for (_, label), (name, aliases) in found.items()]
        self._terms.sort(key=lambda term: (term.name, term.label))
        alias_keys: list[tuple[str, str]] = []
        self._exact: dict[str, list[str]] = {}
        for index, term in enumerate(self._terms):
            for number, alias in enumerate(term.aliases):
                key = f"{index}{_ALIAS_KEY}{number}"
                if len(tokens(alias)) == 1:
                    self._exact.setdefault(unicodedata.normalize("NFKC", alias.strip()), []).append(key)
                else:
                    alias_keys.append((alias, key))
        self._index = NameIndex(
            [*((term.name, str(index)) for index, term in enumerate(self._terms)), *alias_keys], alias_keys
        )
        self._ranked = [
            index
            for index, term in enumerate(self._terms)
            if (vector := hub_vectors.get(term.name)) is not None and float(np.linalg.norm(vector)) > 0.0
        ]
        self._matrix = (
            _normalised_rows([hub_vectors[self._terms[index].name] for index in self._ranked]) if self._ranked else None
        )

    def for_unit(
        self,
        unit: Unit,
        fragments_by_id: Mapping[str, FragmentRec],
        fragment_vectors: Mapping[str, Vector],
        limit: int,
    ) -> list[KnownTerm]:
        """``for_fragments`` over every fragment of ``unit``."""

        return self.for_fragments(unit.fragment_ids, fragments_by_id, fragment_vectors, limit)

    def for_window(
        self,
        unit: Unit,
        window: Window,
        fragments_by_id: Mapping[str, FragmentRec],
        fragment_vectors: Mapping[str, Vector],
        limit: int,
    ) -> list[KnownTerm]:
        """``for_fragments`` over the fragments of ``window`` of ``unit`` (the build's selection: a
        window is shown the terms its own fragments mention or are near)."""

        start, end = window
        return self.for_fragments(unit.fragment_ids[start:end], fragments_by_id, fragment_vectors, limit)

    def for_fragments(
        self,
        fragment_ids: Sequence[str],
        fragments_by_id: Mapping[str, FragmentRec],
        fragment_vectors: Mapping[str, Vector],
        limit: int,
    ) -> list[KnownTerm]:
        """The known terms of ``fragment_ids``: every term they mention by name or alias (by name; at most
        ``MENTIONED_TERMS_MAX``), then at most ``limit`` others by the best cosine to one of their vectors, at
        ``CATALOG_MIN_COSINE`` or above (descending, ties by name).
        """

        counts: Counter[str] = Counter()
        for fragment_id in fragment_ids:
            if fragment_id in fragments_by_id:
                text = fragments_by_id[fragment_id].text
                counts.update(self._index.count(fragment_words(fragments_by_id[fragment_id])))
                if self._exact:
                    counts.update(key for word in exact_words(text) for key in self._exact.get(word, ()))
        occurring: dict[int, set[int]] = {}
        mentions: Counter[int] = Counter()
        for key, count in counts.items():
            term, _, alias = key.partition(_ALIAS_KEY)
            aliases = occurring.setdefault(int(term), set())
            mentions[int(term)] += count
            if alias:
                aliases.add(int(alias))
        chosen = sorted(occurring)
        if len(chosen) > MENTIONED_TERMS_MAX:  # safety cap: the most mentioned, ties by name
            self.capped_units += 1
            chosen = sorted(sorted(chosen, key=lambda index: (-mentions[index], index))[:MENTIONED_TERMS_MAX])
            log_event(logger, WARNING, "graph.known_terms.capped", mentioned=len(occurring), shown=MENTIONED_TERMS_MAX)
        result = [self._shown(index, occurring[index], mentioned=True) for index in chosen]
        present = [fragment_vectors[fid] for fid in fragment_ids if fid in fragment_vectors]
        rows = _normalised_rows(present) if present else None
        if limit <= 0 or rows is None or self._matrix is None:
            return result
        scores = np.round(np.max(self._matrix @ rows.T, axis=1), 9).tolist()
        near = sorted(
            (
                (-score, self._terms[index].name, index)
                for index, score in zip(self._ranked, scores, strict=True)
                if index not in occurring and score >= CATALOG_MIN_COSINE
            )
        )
        return result + [self._shown(index, set(), mentioned=False) for _, _, index in near[:limit]]

    def _shown(self, index: int, occurring: set[int], *, mentioned: bool) -> KnownTerm:
        term = self._terms[index]
        order = sorted(
            range(len(term.aliases)),
            key=lambda number: (number not in occurring, len(term.aliases[number]), term.aliases[number]),
        )
        shown = tuple(term.aliases[number] for number in order[:KNOWN_TERM_ALIASES_SHOWN])
        return KnownTerm(term.name, term.label, shown, mentioned=mentioned)


def shown_terms(terms: Sequence[KnownTerm]) -> KnownTermsShown:
    """What the artifact keeps of known terms shown (one window's, or a unit's windows' joined): the
    names shown, each once, mentioned (in any window) and nearby (the others)."""

    mentioned = tuple(dict.fromkeys(term.name for term in terms if term.mentioned))
    seen = set(mentioned)
    return KnownTermsShown(
        mentioned=mentioned,
        nearby=tuple(dict.fromkeys(term.name for term in terms if not term.mentioned and term.name not in seen)),
    )


def window_key(unit_id: str, window: Window) -> str:
    """The key of a window's known terms: ``<unit_id>@<start>-<end>``."""

    return f"{unit_id}@{window[0]}-{window[1]}"


class TermRanker:
    """The known terms of a build's windows for a hub list (``KnownTerms.for_window``), by ``window_key``, with the
    fragment vectors read once per build (vector store first) and the hub-name vectors per hub list.
    """

    def __init__(self, fragments: Sequence[FragmentRec], vectors: VectorCache, limit: int) -> None:
        self._fragments = list(fragments)
        self._by_id = {fragment.fragment_id: fragment for fragment in self._fragments}
        self._vectors = vectors
        self._limit = limit
        self._fragment_vectors: dict[str, np.ndarray] | None = None
        self.capped_units = 0
        """Windows whose mentioned terms hit ``MENTIONED_TERMS_MAX`` (summed over every call)."""

    async def __call__(
        self, hubs: Sequence[HubName], windows: Sequence[tuple[Unit, Window]]
    ) -> dict[str, list[KnownTerm]]:
        if not hubs or not windows:
            return {}
        if self._fragment_vectors is None:
            self._fragment_vectors = await fragment_vectors(self._fragments, self._vectors)
        ranking = KnownTerms(hubs, await hub_vectors(hubs, self._vectors))
        found = {
            window_key(unit.unit_id, window): ranking.for_window(
                unit, window, self._by_id, self._fragment_vectors, self._limit
            )
            for unit, window in windows
        }
        self.capped_units += ranking.capped_units
        return found


# Edge catalog


class EdgeCatalog:
    """The cids an edge call over a unit may link, indexes built once per build."""

    def __init__(
        self,
        *,
        fragments_by_id: Mapping[str, FragmentRec],
        canonical: Sequence[CanonicalNode],
        hub_cids: frozenset[str] | set[str],
        name_vectors: Mapping[str, Vector],
        fragment_vectors: Mapping[str, Vector],
        limit: int,
        aliases: Mapping[str, Sequence[str]] | None = None,
        params: EdgeWindowParams | None = None,
    ) -> None:
        self._fragments = fragments_by_id
        self._fragment_vectors = fragment_vectors
        self._limit = max(limit, 0)
        self._heading_part = params is not None
        self._nearness: NearnessMode = params.nearness if params is not None else "name"
        ordered = sorted(canonical, key=lambda node: node.cid)
        self._nodes = {node.cid: node for node in ordered}
        self._hub_cids = frozenset(node.cid for node in ordered if node.cid in hub_cids)
        self._grounded: dict[str, list[str]] = {}
        for node in ordered:
            for fragment_id in dict.fromkeys(node.fragment_ids):
                self._grounded.setdefault(fragment_id, []).append(node.cid)
        hubs = [node for node in ordered if node.cid in hub_cids]
        extra = aliases or {}
        self._mentions = NameIndex(
            ((node.name, node.cid) for node in hubs),
            (
                (value, node.cid)
                for node in hubs
                for value in surface_values([*node.identity_aliases, *extra.get(node.cid, ())])
            ),
        )
        self._ranked = [node.cid for node in hubs if node.cid in name_vectors]
        self._matrix = _normalised_rows([name_vectors[cid] for cid in self._ranked]) if self._ranked else None
        self._position = {cid: index for index, cid in enumerate(self._ranked)}

    def for_unit(self, unit: Unit) -> list[str]:
        """The unit's catalog by the full rule: local, mentioned, then nearest hubs."""

        return self.select(unit).cids

    def select(self, unit: Unit, kept: Sequence[str] | None = None, referenced: Sequence[str] = ()) -> UnitCatalog:
        """The unit's catalog by part; ``kept``: the nearness hubs to keep instead of ranking;
        ``referenced``: the cids the unit references (class docstring)."""

        local = sorted({cid for fid in unit.fragment_ids for cid in self._grounded.get(fid, ())})
        excluded = set(local)
        body = self._heading_part
        counts = self._named_in_text(unit.fragment_ids, body_only=body)
        foreign = sorted(cid for cid in counts if cid not in excluded)[: self._limit]
        excluded.update(foreign)
        heading: list[str] = []
        if self._heading_part:
            named = self._named_in_headings(unit.fragment_ids, with_lines=True)
            heading = sorted(cid for cid in named if cid not in excluded and cid not in counts)
            heading = heading[: self._limit - len(foreign)]
            excluded.update(heading)
        room = self._limit - len(foreign) - len(heading)
        if self._nearness == "off":
            near: list[str] = []
        elif kept is not None:
            near = [cid for cid in dict.fromkeys(kept) if cid in self._position and cid not in excluded]
            near = near[: max(room, 0)]
        else:
            near = self._nearest(unit, local, excluded, room)
        excluded.update(near)
        extra = [cid for cid in dict.fromkeys(referenced) if cid in self._nodes and cid not in excluded]
        return UnitCatalog(local=local, mentioned=foreign, near=near, heading=heading, referenced=extra)

    def _named_in_text(self, fragment_ids: Iterable[str], *, body_only: bool = False) -> Counter[str]:
        """Hub mentions in the text of ``fragment_ids``; ``body_only``: in its body lines only
        (``body_lines``)."""

        counts: Counter[str] = Counter()
        for fragment_id in fragment_ids:
            fragment = self._fragments.get(fragment_id)
            if fragment is None:
                continue
            words = hub_words(body_lines(fragment.text)[0]) if body_only else fragment_words(fragment)
            counts.update(self._mentions.count(words))
        return counts

    def _named_in_headings(self, fragment_ids: Iterable[str], *, with_lines: bool = False) -> set[str]:
        """Hubs named by an element of the heading path of one of ``fragment_ids`` and, ``with_lines``,
        by a heading line of its text (``body_lines``); each heading read once."""

        elements = dict.fromkeys(
            element
            for fragment_id in fragment_ids
            if fragment_id in self._fragments
            for element in (
                *self._fragments[fragment_id].heading_path,
                *(body_lines(self._fragments[fragment_id].text)[1] if with_lines else ()),
            )
        )
        return {cid for element in elements for cid in self._mentions.count(hub_words(element))}

    def window_catalog(
        self, unit: Unit, window: Window, catalog: UnitCatalog, referenced: Mapping[str, Sequence[str]] | None = None
    ) -> tuple[str, ...]:
        """The window catalog of ``window`` of ``unit``: the identity keys, sorted and unique, of the nodes grounded in
        the window's fragments, of the catalog's hubs named in their text or heading path, and of the catalog's
        ``referenced`` nodes the window's fragments reference (``referenced``: cids by fragment id,
        ``references.UnitReferences.by_fragment``).
        """

        start, end = window
        fragment_ids = unit.fragment_ids[start:end]
        shown = set(catalog.cids)
        cids = {cid for fragment_id in fragment_ids for cid in self._grounded.get(fragment_id, ())}
        named = set(self._named_in_text(fragment_ids)) | self._named_in_headings(fragment_ids)
        cids.update(cid for cid in named if cid in shown)
        listed = set(catalog.referenced)
        cids.update(
            cid for fragment_id in fragment_ids for cid in (referenced or {}).get(fragment_id, ()) if cid in listed
        )
        return tuple(sorted({self._nodes[cid].identity_key for cid in cids if cid in self._nodes}))

    def section_hints(self, unit: Unit, catalog: UnitCatalog, labels: Collection[str]) -> dict[str, str]:
        """Section hints: per local cid of one of ``labels`` (the owner targets of the ownership line,
        ``prompts.ownership_targets``), the heading path of its first fragment in the unit, joined by
        ``BREADCRUMB_SEPARATOR``; a node whose fragment has no heading gets none.
        """

        order = {fragment_id: index for index, fragment_id in enumerate(unit.fragment_ids)}
        hints: dict[str, str] = {}
        for cid in catalog.local:
            node = self._nodes.get(cid)
            if node is None or node.label not in labels:
                continue
            first = min((fid for fid in node.fragment_ids if fid in order), key=order.__getitem__, default=None)
            fragment = self._fragments.get(first) if first is not None else None
            parts = [part.strip() for part in fragment.heading_path if part.strip()] if fragment else []
            path = BREADCRUMB_SEPARATOR.join(parts)
            if path:
                hints[cid] = path
        return hints

    def _nearest(self, unit: Unit, local: Sequence[str], excluded: set[str], room: int) -> list[str]:
        rows = _unit_rows(unit, self._fragment_vectors)
        if room <= 0 or rows is None or self._matrix is None:
            return []
        scores = np.round(rows @ self._matrix.T, 9)
        grounded = [self._position[cid] for cid in local if cid in self._position]
        if grounded:
            scores[:, grounded] = -np.inf
        # Each fragment's floor, from its best cosine to any foreign hub (mentioned ones included).
        floors = np.maximum(CATALOG_MIN_COSINE, CATALOG_RELATIVE_COSINE * np.max(scores, axis=1))
        blocked = [self._position[cid] for cid in excluded if cid in self._position]
        if blocked:
            scores[:, blocked] = -np.inf
        best: dict[int, float] = {}
        for row, floor in zip(scores, floors.tolist(), strict=True):
            for index in np.argsort(-row, kind="stable")[:CATALOG_NEAREST].tolist():
                score = float(row[index])
                if score != -np.inf and score >= floor:
                    best[index] = max(best.get(index, -2.0), score)
        ranked = sorted(best, key=lambda index: (-best[index], self._ranked[index]))
        return [self._ranked[index] for index in ranked[:room]]


@dataclass(frozen=True, slots=True)
class UnitCatalog:
    """An edge catalog by part (``EdgeCatalog.select``): local nodes, mentioned hubs, nearness hubs,
    hubs named only in a heading path and referenced nodes; ``cids`` lists them local, mentioned,
    heading, near, referenced."""

    local: list[str]
    mentioned: list[str]
    near: list[str]
    heading: list[str] = field(default_factory=list)
    referenced: list[str] = field(default_factory=list)

    @property
    def cids(self) -> list[str]:
        return [*self.local, *self.mentioned, *self.heading, *self.near, *self.referenced]

    def parts(self) -> CatalogParts:
        """The foreign parts as the edges artifact keeps them (``EdgesArtifact.catalog_parts``)."""

        return CatalogParts(
            mentioned=tuple(self.mentioned),
            heading=tuple(self.heading),
            near=tuple(self.near),
            referenced=tuple(self.referenced),
        )
