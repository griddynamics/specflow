"""Step ``detect``: the detectors of the rules file turn the graph into seeds (docs/graph/gap/detection.md
"Detector catalogue", "Stated open questions"; FR-GDET-01 to FR-GDET-05, FR-GDET-10, FR-GREL-03 to FR-GREL-05).

A detector is a deterministic check with no model call over the in-memory ``GraphSnapshot``. Each catalogue rule
has one function here (``RULES``); the detector's entry in the rules file gives every spec-side name it uses (types,
relations, attributes, values), so no build label, relation or attribute is written in this module (FR-GONT-04).
Slice 2 rules have no function yet: their entries are disabled by ``slice``, and an override that enables one is
reported as a failure ``detector_not_shipped``.

The relevance filter applies to the ``structure`` family only (FR-GREL-03, FR-GREL-04): a seed whose nodes lie only
in ``tracking`` sections is skipped and its nodes go into the noise report; a seed whose best section is ``mixed``
stays only when one of its nodes has obligation wording, and becomes weak (FR-GREL-05). A detector that raises is
skipped and named in ``detector_failures``; the others still run (FR-GDET-10).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Final, cast

from graph.core import normalise_id, slug
from graph.core.errors import GraphError
from graph.core.identity import explicit_id_pattern
from graph.core.ontology.compiled import BUILD_OWNER, USED_BY, USES
from graph.core.reader import GraphEdge, GraphNode
from graph.gap.identity import gap_signature, normalise_question, normalise_quote, witness_entry
from graph.gap.models import BuildFacts, NoteRow, Witness
from graph.gap.names import DetectorFamily, GapKind, Relevance, SeedStrength
from graph.gap.rules import DetectorRule, GapRules
from graph.gap.steps.relevance import MIXED, SPEC, TRACKING, RelevanceOutcome, cue_pattern, has_cue
from graph.gap.steps.snapshot import GraphSnapshot, Line, Section
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

SHIPPED_SLICE: Final = 1
"""Detectors of this slice or an earlier one run (detection.md "Detector catalogue": slice 2 ships later)."""
STRUCTURE: Final = "structure"
NOT_SHIPPED: Final = "detector_not_shipped"
_RELEVANCE_ORDER: Final[Mapping[str, int]] = {SPEC: 0, MIXED: 1, TRACKING: 2}
_SENTENCE: Final = re.compile(r"(?<=[.!?;])\s+")
_NUMBER: Final = re.compile(r"\d")
_NUMBER_WITH_UNIT: Final = re.compile(r"\d+(?:[.,]\d+)*\s*(?:%|[^\W\d_]+|[°€$£µ])")
_NUMBERED_SECTION: Final = re.compile(r"(?<!\w)(?:section|chapter|§)\s*(\d+(?:\.\d+)*)", re.IGNORECASE)
_HEADING_NUMBER: Final = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?(?:\s|$)")
_DOCUMENT_NAME: Final = re.compile(
    r"(?<![\w./-])([\w-]+\.(?:md|markdown|docx?|pdf|txt|html?|rst|adoc))(?!\w)", re.IGNORECASE
)
_NOT_WORD: Final = re.compile(r"[^\w]+")
DETECTOR_ERRORS: Final = (GraphError, ArithmeticError, AttributeError, LookupError, RuntimeError, TypeError, ValueError)
"""What a failing detector raises; it is skipped and counted, never fails the run (FR-GDET-10)."""


@dataclass(frozen=True)
class Seed:
    """One detector hit: what it is about (``ABOUT`` candidates), what an answer would also change (``IMPACTS``
    candidates), its witness, strength and the relevance of the sections it lies in."""

    detector: str
    kind: GapKind
    family: DetectorFamily
    strength: SeedStrength
    about: tuple[str, ...]
    witness: tuple[Witness, ...]
    context: tuple[str, ...] = ()
    relevance: Relevance = "spec"
    rule: str = ""

    @cached_property
    def signature(self) -> str:
        """data-model.md "Identity": detector code, sorted ``ABOUT`` ids and sorted witness entries."""

        return gap_signature(self.detector, self.about, [entry for item in self.witness for entry in _entries(item)])


def _entries(witness: Witness) -> list[dict[str, str]]:
    if witness.fact is not None:
        return [witness_entry(fact=witness.fact)]
    if witness.fragment_id is not None and witness.quote is not None:
        return [witness_entry(fragment_id=witness.fragment_id, quote=witness.quote)]
    return []


@dataclass(frozen=True)
class DetectInput:
    """What a detector reads: the snapshot, its rules, the build's persisted facts and the section relevance."""

    snapshot: GraphSnapshot
    rules: GapRules
    build: BuildFacts
    relevance: RelevanceOutcome

    @cached_property
    def cues(self) -> re.Pattern[str] | None:
        return self.relevance.cue_pattern or cue_pattern(self.rules.relevance.obligation_cues)

    def nodes(self, rule: DetectorRule) -> Iterator[GraphNode]:
        labels = set(rule.labels)
        return (node for node in self.snapshot.nodes.values() if node.label in labels)


Detector = Callable[[DetectInput, DetectorRule], Iterable[Seed]]


@dataclass(frozen=True)
class DetectOutcome:
    """The seeds of one detection, the info notes, the skipped nodes by section and the metrics."""

    seeds: tuple[Seed, ...]
    notes: tuple[NoteRow, ...] = ()
    removed: Mapping[tuple[str, tuple[str, ...]], frozenset[str]] = field(default_factory=dict)
    by_detector: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    failures: Mapping[str, str] = field(default_factory=dict)


def _seed(rule: DetectorRule, about: Iterable[str], witness: Iterable[Witness], context: Iterable[str] = ()) -> Seed:
    ids = tuple(sorted(set(about)))
    return Seed(
        detector=rule.code,
        kind=cast(GapKind, rule.kind),
        family=rule.family,
        strength=rule.strength,
        about=ids,
        witness=tuple(witness),
        context=tuple(sorted(set(context) - set(ids))),
        rule=rule.rule,
    )


def _fact(text: str) -> Witness:
    return Witness(fact=normalise_quote(text))


def _line_or_fact(snapshot: GraphSnapshot, node: GraphNode, fact: str) -> tuple[Witness, ...]:
    lines = snapshot.lines(node)
    return (lines[0].witness(),) if lines else (_fact(fact),)


def _empty(value: object) -> bool:
    return value is None or value == "" or (isinstance(value, list | tuple | set | dict) and not value)


# Structure rules


def no_spine_owner(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """MS-OWNER: a node that reaches no spine root along the spine (after inference) except the excluded ones."""

    snapshot = source.snapshot
    for node in source.nodes(rule):
        if snapshot.out_of_spine(node) or snapshot.spine_owners(node.spec_id, rule.exclude):
            continue
        fact = _fact(f"{node.spec_id} reaches no owner along the spine")
        lines = snapshot.lines(node)
        yield _seed(rule, [node.spec_id], (fact, *(line.witness() for line in lines[:1])))


def missing_relation(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """MS-PROVIDER, MS-STORE: a node with no ``relation`` edge in ``direction``."""

    snapshot, relation = source.snapshot, rule.relation or ""
    for node in source.nodes(rule):
        edges = snapshot.incoming if rule.direction == "in" else snapshot.outgoing
        if snapshot.out_of_spine(node) or any(edge.relation == relation for edge in edges.get(node.spec_id, ())):
            continue
        way = "incoming" if rule.direction == "in" else "outgoing"
        yield _seed(rule, [node.spec_id], [_fact(f"{node.spec_id} has no {way} {relation} relation")])


def empty_attribute(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """MS-CRITERIA: a node whose ``attribute`` is empty."""

    attribute = rule.attribute or ""
    for node in source.nodes(rule):
        if source.snapshot.out_of_spine(node) or not _empty(source.snapshot.value(node, attribute)):
            continue
        yield _seed(rule, [node.spec_id], [_fact(f"{node.spec_id} has no {attribute}")])


def attribute_value(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """AM-IMPLIED, AS-FLAG: a node whose ``attribute`` is ``value``."""

    attribute = rule.attribute or ""
    for node in source.nodes(rule):
        value = source.snapshot.value(node, attribute)
        if value is None or str(value) != rule.value:
            continue
        yield _seed(rule, [node.spec_id], _line_or_fact(source.snapshot, node, f"{node.spec_id} {attribute} {value}"))


def checklist(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """MS-COVERAGE: a topic of ``topics`` no eligible node of ``types`` covers by name or text; one seed per topic,
    about the project (no node)."""

    snapshot = source.snapshot
    texts = [
        f"{node.name}\n{snapshot.text(node)}".casefold()
        for node in source.nodes(rule)
        if source.relevance.of_node(snapshot, node.spec_id) != TRACKING
    ]
    for topic in rule.topics:
        cues = [cue.casefold() for cue in source.rules.checklist.get(topic, ())]
        if any(cue in text for text in texts for cue in cues):
            continue
        yield _seed(rule, [], [_fact(f"no {' or '.join(rule.types)} covers the checklist topic {topic}")])


def integration_without_interface(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """MS-CONTRACT: two modules joined by one of ``relations`` (directly, or a member requirement of one related to
    the other) with no interface of ``interfaces`` provided by one and consumed by the other."""

    snapshot = source.snapshot
    contracts = _contracts(snapshot, rule)
    for pair, via in sorted(_joined(source, rule).items()):
        if pair in contracts:
            continue
        first, second = pair
        witness = [
            _fact(
                f"{first} and {second} are joined with no {' or '.join(rule.interfaces)} {rule.provided_by} one and "
                f"{rule.consumed_by} the other"
            )
        ]
        namer = snapshot.nodes.get(via[0]) if len(via) == 1 else None
        if namer is not None:
            witness += [line.witness() for line in snapshot.lines(namer)[:1]]
        yield _seed(rule, pair, witness, context=via if namer is not None else ())


def _joined(source: DetectInput, rule: DetectorRule) -> dict[tuple[str, str], tuple[str, ...]]:
    """Module pairs joined by one of ``relations``: by the edge (its two ends) or by a member requirement (its id)."""

    snapshot = source.snapshot
    modules = {
        node.spec_id
        for node in source.nodes(rule)
        if not snapshot.out_of_spine(node) and not snapshot.matches(node, rule.exclude)
    }
    relations = set(rule.relations)
    joined: dict[tuple[str, str], tuple[str, ...]] = {}
    for edge in snapshot.edges:
        if edge.relation in relations and edge.source != edge.target and {edge.source, edge.target} <= modules:
            joined.setdefault(_pair(edge.source, edge.target), (edge.source, edge.target))
    members = set(rule.member_labels)
    for node in snapshot.nodes.values():
        if node.label not in members:
            continue
        owners = snapshot.spine_owners(node.spec_id, rule.exclude, without=relations) & modules
        others = {other for relation in relations for other in snapshot.step(node.spec_id, relation)}
        for other in sorted(others & modules - owners):
            for owner in sorted(owners):
                joined.setdefault(_pair(owner, other), (node.spec_id,))
    return joined


def _pair(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first <= second else (second, first)


def _contracts(snapshot: GraphSnapshot, rule: DetectorRule) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    interfaces = set(rule.interface_labels)
    for node in snapshot.nodes.values():
        if node.label not in interfaces:
            continue
        providers = snapshot.step(node.spec_id, rule.provided_by or "")
        consumers = snapshot.step(node.spec_id, rule.consumed_by or "")
        found |= {_pair(provider, consumer) for provider in providers for consumer in consumers if provider != consumer}
    return found


def in_scope_uses_out_of_scope(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """IN-SCOPE: a node of ``types`` whose ``attribute`` is not ``value`` that uses (impact role ``uses``) a node
    whose ``attribute`` is ``value``."""

    snapshot, ontology = source.snapshot, source.snapshot.ontology
    attribute, out = rule.attribute or "", rule.value
    labels = set(rule.labels)
    found: dict[str, set[str]] = defaultdict(set)
    for edge in snapshot.edges:
        source_node, target_node = snapshot.nodes[edge.source], snapshot.nodes[edge.target]
        role = ontology.impact_role(edge.relation, source_node.label, target_node.label)
        if role == USES:
            user, used = source_node, target_node
        elif role == USED_BY:
            user, used = target_node, source_node
        else:
            continue
        if user.label not in labels or _text(snapshot.value(user, attribute)) == out:
            continue
        if _text(snapshot.value(used, attribute)) == out:
            found[user.spec_id].add(used.spec_id)
    for user_id, used_ids in sorted(found.items()):
        facts = [_fact(f"{user_id} uses {item}, whose {attribute} is {out}") for item in sorted(used_ids)]
        yield _seed(rule, [user_id], facts, context=used_ids)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def cycle(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """IN-CYCLE: a cycle of ``relation`` among nodes of ``types``, with the edges the build cut out of a cycle."""

    snapshot, relation = source.snapshot, rule.relation or ""
    labels = set(rule.labels)
    members = {spec_id for spec_id, node in snapshot.nodes.items() if node.label in labels}
    graph: dict[str, set[str]] = defaultdict(set)
    evidence: dict[tuple[str, str], list[Witness]] = defaultdict(list)
    for edge in snapshot.edges:
        if edge.relation == relation and edge.source in members and edge.target in members:
            graph[edge.source].add(edge.target)
            evidence[(edge.source, edge.target)] += _edge_lines(snapshot, edge)
    for cut in source.build.cut_edges:
        if cut.relation == relation and cut.source_spec_id in members and cut.target_spec_id in members:
            graph[cut.source_spec_id].add(cut.target_spec_id)
    for component in _cycles(graph):
        ids = sorted(component)
        lines = [line for pair, found in sorted(evidence.items()) if set(pair) <= component for line in found]
        fact = _fact(f"{relation} cycle among {', '.join(ids)}")
        yield _seed(rule, ids, (fact, *_unique(lines)))


def _edge_lines(snapshot: GraphSnapshot, edge: GraphEdge) -> list[Witness]:
    return [
        Witness(fragment_id=item.fragment_id, quote=item.quote)
        for item in edge.evidence
        if item.quote and item.fragment_id in snapshot.fragments
    ]


def _unique(witnesses: Iterable[Witness]) -> list[Witness]:
    return list(dict.fromkeys(witnesses))


def _cycles(graph: Mapping[str, set[str]]) -> list[set[str]]:
    """The strongly connected components with a cycle (two or more nodes, or a self loop); Kosaraju, iterative."""

    reverse: dict[str, set[str]] = defaultdict(set)
    for source, targets in graph.items():
        for target in targets:
            reverse[target].add(source)
    seen: set[str] = set()
    found: list[set[str]] = []
    for root in reversed(_finish_order(graph)):
        if root in seen:
            continue
        component = _reach(reverse, root, seen)
        if len(component) > 1 or root in graph.get(root, ()):
            found.append(component)
    return found


def _finish_order(graph: Mapping[str, set[str]]) -> list[str]:
    """Every node of ``graph`` in the order a depth-first walk finishes it."""

    order: list[str] = []
    visited: set[str] = set()
    for root in sorted(graph):
        if root in visited:
            continue
        visited.add(root)
        work: list[tuple[str, Iterator[str]]] = [(root, iter(sorted(graph.get(root, ()))))]
        while work:
            node, children = work[-1]
            child = next((item for item in children if item not in visited), None)
            if child is None:
                work.pop()
                order.append(node)
                continue
            visited.add(child)
            work.append((child, iter(sorted(graph.get(child, ())))))
    return order


def _reach(graph: Mapping[str, set[str]], root: str, seen: set[str]) -> set[str]:
    """The nodes ``root`` reaches in ``graph`` outside ``seen``; marks them seen."""

    component = {root}
    seen.add(root)
    stack = [root]
    while stack:
        for item in graph.get(stack.pop(), ()):
            if item not in seen:
                seen.add(item)
                component.add(item)
                stack.append(item)
    return component


# Text rules


def vague_terms(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """AM-VAGUE: a node whose line holds a term of ``vague_terms`` and no number with a unit in the same sentence."""

    pattern = cue_pattern(source.rules.vague_terms)
    for node in source.nodes(rule):
        hits = [
            line
            for line in source.snapshot.lines(node)
            if any(
                has_cue(pattern, sentence) and not _NUMBER_WITH_UNIT.search(sentence)
                for sentence in _SENTENCE.split(line.text)
            )
        ]
        if hits:
            yield _seed(rule, [node.spec_id], [line.witness() for line in hits])


def no_number(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """AM-MEASURE: a node whose text holds no number."""

    for node in source.nodes(rule):
        lines = source.snapshot.lines(node)
        text = source.snapshot.text(node)
        if not text or _NUMBER.search(text):
            continue
        yield _seed(
            rule, [node.spec_id], [line.witness() for line in lines[:1]] or [_fact(f"{node.spec_id} holds no number")]
        )


def refused_merge(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """CT-VALUE: a pair of nodes of ``types`` the build kept apart although near-duplicate (its dedup refusals)."""

    snapshot = source.snapshot
    labels = set(rule.labels)
    for pair in source.build.refused_pairs:
        first, second = snapshot.nodes.get(pair.first_spec_id), snapshot.nodes.get(pair.second_spec_id)
        if first is None or second is None or first.spec_id == second.spec_id:
            continue
        if first.label not in labels or second.label not in labels:
            continue
        witness = [
            *_line_or_fact(snapshot, first, f"{first.spec_id} differs from {second.spec_id} ({pair.guard})"),
            *_line_or_fact(snapshot, second, f"{second.spec_id} differs from {first.spec_id} ({pair.guard})"),
        ]
        yield _seed(rule, [first.spec_id, second.spec_id], _unique(witness))


def present(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """OQ-STATED: every node of ``types`` still in the graph, about itself and the nodes it relates to."""

    snapshot = source.snapshot
    for node in source.nodes(rule):
        related = snapshot.related(node.spec_id)
        yield _seed(rule, [node.spec_id, *related], _line_or_fact(snapshot, node, f"{node.spec_id} is open"))


def placeholder_markers(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """OQ-TBD: a fragment line holding a marker of ``placeholder_markers`` whose range grounds no node of ``types``."""

    snapshot = source.snapshot
    pattern = cue_pattern(source.rules.placeholder_markers)
    grounded = _grounded_lines(snapshot, set(rule.labels))
    for fragment_id in snapshot.fragments:
        for line in snapshot.fragment_lines(fragment_id):
            if not has_cue(pattern, line.text) or (fragment_id, line.line_start) in grounded:
                continue
            yield _seed(rule, _nodes_on(snapshot, line), [line.witness()])


def dangling_reference(source: DetectInput, rule: DetectorRule) -> Iterator[Seed]:
    """MS-REF: a fragment line citing an explicit spec id, a numbered section or a document name that resolves to
    no node, heading or document of the project."""

    snapshot = source.snapshot
    index = _ReferenceIndex.of(snapshot)
    for fragment_id in snapshot.fragments:
        for line in snapshot.fragment_lines(fragment_id):
            missing = index.missing(line.text)
            if missing:
                yield _seed(rule, _nodes_on(snapshot, line), [line.witness()])


@dataclass(frozen=True)
class _ReferenceIndex:
    ids: re.Pattern[str] | None
    leads: frozenset[str]
    numbers: frozenset[str]
    documents: frozenset[str]

    @classmethod
    def of(cls, snapshot: GraphSnapshot) -> _ReferenceIndex:
        ontology = snapshot.ontology
        families = [
            family
            for label in ontology.labels
            if ontology.owner_of(label) == BUILD_OWNER
            for family in ontology.node_type(label).id_families
        ]
        leads = {
            part
            for node in snapshot.nodes.values()
            for spec_id in (node.spec_id, *node.spec_id_aliases)
            for part in _prefixes(spec_id.removeprefix(ontology.id_prefix(node.label)))
        }
        numbers = {
            found[1]
            for section in snapshot.sections
            for heading in section.heading_path
            if (found := _HEADING_NUMBER.match(heading)) is not None
        }
        documents = {
            name.casefold()
            for document in snapshot.documents.values()
            for name in (document.path or "", (document.path or "").rsplit("/", 1)[-1], document.title or "")
            if name
        }
        return cls(explicit_id_pattern(families), frozenset(leads), frozenset(numbers), frozenset(documents))

    def missing(self, text: str) -> list[str]:
        """The references of ``text`` that resolve to nothing."""

        found: list[str] = []
        if self.ids is not None:
            for match in self.ids.finditer(text):
                lead = slug("", normalise_id(match.group(0)))
                if lead not in self.leads:
                    found.append(match.group(0))
        found += [match[1] for match in _NUMBERED_SECTION.finditer(text) if match[1] not in self.numbers]
        found += [match[1] for match in _DOCUMENT_NAME.finditer(text) if match[1].casefold() not in self.documents]
        return found


def _prefixes(last: str) -> list[str]:
    words = last.split("_")
    return ["_".join(words[:size]) for size in range(1, len(words) + 1)]


def _grounded_lines(snapshot: GraphSnapshot, labels: set[str]) -> set[tuple[str, int | None]]:
    grounded: set[tuple[str, int | None]] = set()
    for node in snapshot.nodes.values():
        if node.label not in labels:
            continue
        for item in node.provenance:
            start, end = item.line_start, item.line_end if item.line_end is not None else item.line_start
            if start is None or end is None:
                continue
            grounded |= {(item.fragment_id, number) for number in range(start, end + 1)}
    return grounded


def _nodes_on(snapshot: GraphSnapshot, line: Line) -> list[str]:
    """The nodes whose evidence lies on ``line``."""

    number = line.line_start
    return sorted(
        node.spec_id
        for node in snapshot.nodes.values()
        for item in node.provenance
        if item.fragment_id == line.fragment_id
        and number is not None
        and item.line_start is not None
        and item.line_start <= number <= (item.line_end or item.line_start)
    )


RULES: Final[Mapping[str, Detector]] = {
    "no_spine_owner": no_spine_owner,
    "missing_relation": missing_relation,
    "integration_without_interface": integration_without_interface,
    "empty_attribute": empty_attribute,
    "checklist": checklist,
    "dangling_reference": dangling_reference,
    "vague_terms": vague_terms,
    "no_number": no_number,
    "attribute_value": attribute_value,
    "refused_merge": refused_merge,
    "in_scope_uses_out_of_scope": in_scope_uses_out_of_scope,
    "cycle": cycle,
    "present": present,
    "placeholder_markers": placeholder_markers,
}
"""The detector function of each catalogue rule that ships (slice 1)."""


# The driver


def detect(
    snapshot: GraphSnapshot,
    *,
    rules: GapRules,
    relevance: RelevanceOutcome,
    build: BuildFacts | None = None,
    registry: Mapping[str, Detector] = RULES,
    slice_: int = SHIPPED_SLICE,
    project_id: str = "",
) -> DetectOutcome:
    """Run every detector of ``slice_`` or earlier over ``snapshot`` and filter the structural seeds by relevance."""

    source = DetectInput(snapshot=snapshot, rules=rules, build=build or BuildFacts(), relevance=relevance)
    seeds: dict[str, Seed] = {}
    notes: list[NoteRow] = []
    removed: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    counts: dict[str, Counter[str]] = {}
    failures: dict[str, str] = {}
    for rule in rules.detectors_of_slice(slice_):
        counts[rule.code] = Counter()
        function = registry.get(rule.rule)
        if function is None:
            failures[rule.code] = NOT_SHIPPED
            continue
        try:
            found = list(function(source, rule))
        except DETECTOR_ERRORS as error:
            failures[rule.code] = error.code if isinstance(error, GraphError) else type(error).__name__
            log_event(logger, WARNING, "graph.gap.detect.detector_failed", detector=rule.code, code=failures[rule.code])
            continue
        for raw in found:
            seed = _filtered(source, raw, removed)
            if seed is None:
                counts[rule.code]["skipped_tracking"] += 1
                continue
            if _is_note(source, seed):
                notes.append(_note(seed))
                counts[rule.code]["notes"] += 1
                continue
            if seed.signature not in seeds:
                seeds[seed.signature] = seed
                counts[rule.code][seed.strength] += 1
    log_event(
        logger,
        WARNING if failures else INFO,
        "graph.gap.detect.finished",
        project_id=project_id,
        seeds=len(seeds),
        notes=len(notes),
        failures=len(failures),
    )
    return DetectOutcome(
        seeds=tuple(seeds.values()),
        notes=tuple(notes),
        removed={key: frozenset(value) for key, value in removed.items()},
        by_detector={code: dict(sorted(counter.items())) for code, counter in counts.items()},
        failures=failures,
    )


def _filtered(source: DetectInput, seed: Seed, removed: dict[tuple[str, tuple[str, ...]], set[str]]) -> Seed | None:
    """The seed with its relevance; None for a structural seed of tracking nodes, or of mixed nodes none of which
    has obligation wording (their nodes are listed in ``removed`` by section)."""

    snapshot, relevance = source.snapshot, source.relevance
    if seed.family != STRUCTURE:
        return replace(seed, relevance=_text_relevance(source, seed))
    if not seed.about:
        return seed
    found = {spec_id: relevance.of_node(snapshot, spec_id) for spec_id in seed.about}
    best = min(found.values(), key=_RELEVANCE_ORDER.__getitem__)
    if best == SPEC:
        return seed
    if best == MIXED and any(
        has_cue(source.cues, snapshot.text(snapshot.nodes[spec_id]))
        for spec_id, value in found.items()
        if value == MIXED
    ):
        return replace(seed, relevance=MIXED, strength="weak")
    for spec_id in seed.about:
        for section in snapshot.node_sections.get(spec_id, ()):
            if relevance.of(section) != SPEC:
                removed[section.ident].add(spec_id)
    return None


def _text_relevance(source: DetectInput, seed: Seed) -> Relevance:
    sections: list[Section] = [
        section
        for item in seed.witness
        if item.fragment_id is not None and (section := source.snapshot.section_of_fragment.get(item.fragment_id))
    ]
    values = [source.relevance.of(section) for section in sections] or [
        source.relevance.of_node(source.snapshot, spec_id) for spec_id in seed.about
    ]
    return min(values, key=_RELEVANCE_ORDER.__getitem__) if values else SPEC


def _is_note(source: DetectInput, seed: Seed) -> bool:
    """A stated question about out-of-scope nodes only can only ever be info (detection.md "Info notes")."""

    if seed.rule != "present":
        return False
    snapshot = source.snapshot
    others = [
        snapshot.nodes[spec_id] for spec_id in seed.about if snapshot.nodes[spec_id].label not in _labels(source, seed)
    ]
    return bool(others) and all(snapshot.out_of_spine(node) for node in others)


def _labels(source: DetectInput, seed: Seed) -> set[str]:
    rule = source.rules.detectors.get(seed.detector)
    return set(rule.labels) if rule is not None else set()


def _note(seed: Seed) -> NoteRow:
    return NoteRow(
        detector=seed.detector, spec_ids=seed.about, text="stated question about nodes outside the spine only"
    )


# Witnesses


def witness_holds(snapshot: GraphSnapshot, witness: Witness, facts: Iterable[str] = ()) -> bool:
    """True when a witness still resolves: its quote is in its active fragment (normalised), or its graph fact is
    among ``facts`` (the facts the detectors prove again on ``snapshot``)."""

    if witness.fact is not None:
        return normalise_quote(witness.fact) in set(facts)
    fragment = snapshot.fragments.get(witness.fragment_id or "")
    if fragment is None or not witness.quote:
        return False
    return normalise_quote(witness.quote) in normalise_quote(fragment.text)


def facts_of(seeds: Iterable[Seed]) -> frozenset[str]:
    return frozenset(item.fact for seed in seeds for item in seed.witness if item.fact is not None)


def question_text(snapshot: GraphSnapshot, seed: Seed) -> str | None:
    """The normalised question of a stated-question seed (its witness line), None for another seed."""

    if seed.rule != "present":
        return None
    quotes = [item.quote for item in seed.witness if item.quote]
    if quotes:
        return normalise_question(quotes[0])
    nodes = [snapshot.nodes[spec_id] for spec_id in seed.about if spec_id in snapshot.nodes]
    return normalise_question(nodes[0].name) if nodes else None


def same_question(first: str, second: str) -> bool:
    """Two normalised questions ask the same when their words are equal (punctuation and spacing aside); questions
    worded differently are caught by the duplicate check of a site (investigation.md "Checks")."""

    return _NOT_WORD.sub(" ", first).strip() == _NOT_WORD.sub(" ", second).strip()


def seeds_by_section(
    snapshot: GraphSnapshot, seeds: Sequence[Seed]
) -> dict[tuple[str, tuple[str, ...]] | None, list[Seed]]:
    """Seeds by the section of their first witness quote (or of their first node), None when they have neither."""

    found: dict[tuple[str, tuple[str, ...]] | None, list[Seed]] = defaultdict(list)
    for seed in seeds:
        found[witness_section(snapshot, seed)].append(seed)
    return found


def witness_section(snapshot: GraphSnapshot, seed: Seed) -> tuple[str, tuple[str, ...]] | None:
    for item in seed.witness:
        section = snapshot.section_of_fragment.get(item.fragment_id or "")
        if section is not None:
            return section.ident
    for spec_id in seed.about:
        sections = snapshot.node_sections.get(spec_id, ())
        if sections:
            return sections[0].ident
    return None
