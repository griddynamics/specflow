"""The validated, immutable ``Ontology`` and its parts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal, NamedTuple, get_args

from pydantic import BaseModel, ConfigDict, Field, create_model

from graph.core.errors import ConformanceError
from graph.core.identity import normalise_id

BUILT_IN_LABELS: Final = frozenset({"SpecNode", "Document", "Fragment"})
BUILT_IN_RELATIONS: Final = frozenset({"CONTAINS", "REFERENCED_BY", "MENTIONED_IN"})
BUILT_IN_NAMES: Final = BUILT_IN_LABELS | BUILT_IN_RELATIONS
EVIDENCE_LABELS: Final = frozenset({"Document", "Fragment"})
"""Built-in labels a relation of another owner than build may end at, to cite evidence."""

BUILD_OWNER: Final = "build"
PROMPT_DIGEST_LENGTH: Final = 12
"""Hex characters of the prompt text digest in ``Ontology.judge_prompt_version``."""
EXTRACTED_ORIGIN: Final = "extracted"
HUB_ROLES: Final = frozenset({"anchor", "group"})

FingerprintPart = Literal["nodes", "edges", "attributes", "resolution"]
DropReason = Literal["unknown_relation", "illegal_pair", "cue_missing"]
QuarantineReason = Literal[
    "unknown_relation",
    "illegal_pair",
    "cue_missing",
    "locality",
    "condition_failed",
    "external_owner",
    "owner_conflict",
    "judge_rejected",
    "depth_cut",
    "cycle_broken",
    "invalid_proposal",
]
"""Why Conform or inference cannot keep an edge."""
QUARANTINE_REASONS: Final = frozenset(get_args(QuarantineReason))
DEFAULT_WHERE_REASON: Final = "condition_failed"

QUOTE_ATTRIBUTE: Final = "quote"
"""Format-level attribute name: a node's verbatim source text; Conform grounds it in the node's fragments and stores the
grounded text.
"""
_NOT_ASKED: Final[frozenset[str]] = frozenset({QUOTE_ATTRIBUTE})
"""Attributes the attributes pass never asks for: the node call returns them with the node itself
(``attribute_output_model`` and the attributes prompt leave them out)."""

USES: Final = "uses"
USED_BY: Final = "used_by"
COUPLED: Final = "coupled"
RELATED: Final = "related"
CONTAINS: Final = "contains"
NO_IMPACT: Final = "none"
IMPACT_INVERSE: Final[Mapping[str, str]] = MappingProxyType({USES: USED_BY, USED_BY: USES})
"""The inverse of each impact role that is not its own inverse (trace.md "Impact roles")."""

Condition = Mapping[str, frozenset[str]]
"""``{attribute: allowed values}``: a pair ``where``, the spine ``exclude`` or a rule's ``exclude_where``."""

_ID_FAMILY = re.compile(r"^[A-Z]+")


@dataclass(frozen=True)
class IdentityRule:
    """How a node's identity key is formed."""

    key: Literal["id", "surface"]
    scope: str
    fallback: Literal["span", "surface"] | None


@dataclass(frozen=True)
class AttributeSpec:
    """One attribute of a node type, with the vocabulary and aliases already resolved."""

    name: str
    kind: Literal["text", "enum", "ref"]
    description: str
    max_chars: int | None = None
    values: tuple[str, ...] = ()
    value_descriptions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    aliases: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    """Case-folded alias to declared value."""
    target_labels: frozenset[str] = frozenset()
    store: bool = True
    default: str | None = None
    """Declared value Conform stores when the value is null."""
    inherit_via: str | None = None
    """Read time only: a null value is filled from the owner one ``inherit_via`` hop away."""


@dataclass(frozen=True)
class Support:
    """An edge that supports a node: ``relation`` ending at it (``in``) or starting at it (``out``)."""

    relation: str
    direction: Literal["in", "out"]


@dataclass(frozen=True)
class NodeType:
    """A declared node type with its group's fields inherited."""

    key: str
    label: str
    owner: str
    origin: str
    tier: str
    role: str | None
    group: str | None
    id_format: str | None
    id_prefix: str | None
    id_families: tuple[str, ...]
    identity: IdentityRule
    description: str
    guidance: str
    attributes: Mapping[str, AttributeSpec]
    merge_across_labels: bool = True
    supported_by: tuple[Support, ...] = ()

    @property
    def extracted(self) -> bool:
        return self.origin == EXTRACTED_ORIGIN


@dataclass(frozen=True)
class NodeGroup:
    """A node group: its prompt text and member labels. Never a label itself."""

    key: str
    description: str
    guidance: str
    labels: tuple[str, ...]


def _no_condition() -> Condition:
    return MappingProxyType({})


@dataclass(frozen=True)
class Pair:
    """One legal ``(source label, target label)`` pair of ``relation``, groups expanded, with its flags."""

    relation: str
    source: str
    target: str
    default: bool = False
    default_from: frozenset[str] = frozenset()
    """Empty: the default for any extracted relation; else only for an edge extracted as one of these."""
    cue_required: bool = False
    exclusive: bool = False
    owner: bool = False
    owner_rank: int = 1
    source_where: Condition = field(default_factory=_no_condition, hash=False)
    target_where: Condition = field(default_factory=_no_condition, hash=False)
    where_reason: str = DEFAULT_WHERE_REASON
    otherwise: str | None = None
    max_depth: int | None = None
    not_across: tuple[str, ...] = ()
    distinct_owner: bool = False
    redundant_via_owner: bool = False
    impact: str = NO_IMPACT
    """How the pair carries a change (``uses``, ``used_by``, ``coupled``, ``related``, ``contains``,
    ``none``): its own ``impact``, else its relation's, else ``none``."""


@dataclass(frozen=True)
class DeriveRule:
    """Each resolved ``attribute`` reference of a source node yields a derived ``relation`` edge."""

    relation: str
    source_labels: frozenset[str]
    attribute: str
    target_labels: frozenset[str]


@dataclass(frozen=True)
class DeriveViaRule:
    """An implied ``relation`` edge from each module a hub is ``consumer`` of to the module it is
    ``provider`` of (``consumer`` and ``provider`` are relations from the hub)."""

    relation: str
    consumer: str
    provider: str


@dataclass(frozen=True)
class Relation:
    name: str
    owner: str
    origin: str
    tier: str
    description: str
    pairs: tuple[Pair, ...]
    symmetric: bool = False
    id_references: bool = False
    cues: tuple[re.Pattern[str], ...] = ()
    """Searched anywhere in an evidence line, case-insensitive (``cue_required`` pairs)."""
    cue_words: tuple[re.Pattern[str], ...] = ()
    """The same cues matched only as whole words, case-insensitive (``Ontology.cue_matches``)."""
    derive: tuple[DeriveRule, ...] = ()
    acyclic: bool = False
    stated_only: bool = False
    derive_via: DeriveViaRule | None = None
    impact: str = NO_IMPACT
    """The relation's ``impact``, the default of its pairs (``none`` when the file declares none)."""

    @property
    def extracted(self) -> bool:
        return self.origin == EXTRACTED_ORIGIN


@dataclass(frozen=True)
class SpinePath:
    """``via``: 1..n hops over any of ``relations``, ``min_hops``..``max_hops``; ``sequence``: in order."""

    relations: tuple[str, ...]
    sequence: bool
    min_hops: int
    max_hops: int


@dataclass(frozen=True)
class Spine:
    sources: frozenset[str]
    roots: frozenset[str]
    paths: tuple[SpinePath, ...]
    exclude: Condition = field(default_factory=_no_condition, hash=False)
    """A source is skipped by spine checks and metrics when it or its owner matches."""


@dataclass(frozen=True)
class AnsweredElsewhereSpec:
    """The answered-elsewhere check of Conform: nodes of ``label`` are judged by ``prompt`` against at most
    ``candidates`` fragments of other units with a cosine of at least ``min_cosine``; an answer of confidence ``floor``
    or more drops the node; at most ``cap`` fresh calls per build.
    """

    label: str
    prompt: str
    candidates: int
    min_cosine: float
    floor: float
    cap: int


@dataclass(frozen=True)
class ConformSpec:
    """The owner-conflict judge of normalisation."""

    judge_prompt: str
    cap: int
    floor: float
    fallback_relation: str
    answered_elsewhere: AnsweredElsewhereSpec | None = None
    """None when the file turns the answered-elsewhere check off (no ``answered_elsewhere`` block)."""


@dataclass(frozen=True)
class InferenceRule:
    """One inference rule with endpoints resolved to labels."""

    id: str
    template: str
    prompt: str
    writes: str
    subject: frozenset[str]
    targets: frozenset[str]
    cap: int | None = None
    first_cap: int | None = None
    floor: float | None = None
    reads: tuple[str, ...] = ()
    also_writes: tuple[str, ...] = ()
    via: tuple[str, ...] = ()
    mentions: bool = False
    member_relation: str | None = None
    min_targets: int | None = None
    max_targets: int | None = None
    max_candidates: int | None = None
    exclude_owned_by: frozenset[str] = frozenset()
    container_paths: tuple[SpinePath, ...] = ()
    exclude_where: Condition = field(default_factory=_no_condition, hash=False)
    target_exclude_where: Condition = field(default_factory=_no_condition, hash=False)
    min_documents: int | None = None
    max_pairs: int | None = None
    pair_floor: float | None = None
    batch: int | None = None
    batch_prompt: str | None = None
    """``attach_orphan``: the judge prompt subjects are judged together with (required with ``batch``;
    named without ``batch`` too, so a replay can turn batching on); None: no batch prompt."""

    @property
    def written(self) -> tuple[str, ...]:
        """Every relation the rule writes: ``writes``, then its ``also_writes`` in file order."""

        return (self.writes, *self.also_writes)


@dataclass(frozen=True)
class InferenceSpec:
    """The ``inference`` section: ``cap`` (fresh subjects per rule in every build), ``first_cap`` (fresh subjects per
    rule at most in a ``first`` or ``rebuild`` build; None: the ``cap``), ``first_budget`` (estimated input tokens of
    such a build's inference calls; None: no budget), ``floor`` and the rules in file order.
    """

    cap: int
    floor: float
    rules: tuple[InferenceRule, ...]
    first_cap: int | None = None
    first_budget: int | None = None

    def cap_of(self, rule: InferenceRule) -> int:
        """Subjects judged per build for ``rule``: its own ``cap``, else the ontology's ``cap``."""

        return self.cap if rule.cap is None else rule.cap

    def first_cap_of(self, rule: InferenceRule) -> int:
        """Fresh subjects ``rule`` judges at most in a ``first`` or ``rebuild`` build: its own
        ``first_cap``, else the ontology's ``first_cap``; never below ``cap_of(rule)``, the cap every rule
        keeps in every mode."""

        first_cap = self.first_cap if rule.first_cap is None else rule.first_cap
        return max(self.cap_of(rule), first_cap or 0)

    def reads_of(self, rule: InferenceRule) -> tuple[InferenceRule, ...]:
        """The rules ``rule`` reads (its ``reads``), in the order it names them."""

        by_id = {other.id: other for other in self.rules}
        return tuple(by_id[rule_id] for rule_id in rule.reads)

    def floor_of(self, rule: InferenceRule) -> float:
        """The least confidence ``rule`` accepts: its own ``floor``, else the ontology's ``floor``."""

        return self.floor if rule.floor is None else rule.floor


@dataclass(frozen=True)
class Legalised:
    """The edge is kept as ``relation``; ``flipped`` means source and target swap."""

    relation: str
    flipped: bool


@dataclass(frozen=True)
class Dropped:
    reason: DropReason


class NodeView(NamedTuple):
    """A node as legalisation sees it: its label and its attribute values (by attribute name)."""

    label: str
    attributes: Mapping[str, object] = MappingProxyType({})


class _PromptModel(BaseModel):
    model_config = ConfigDict(frozen=True)


def _clean(text: str) -> str:
    return text.strip()


def _family_of(explicit_id: str) -> str | None:
    match = _ID_FAMILY.match(normalise_id(explicit_id))
    return match.group(0) if match else None


def _canonical_hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _attribute_text(raw: Any) -> str | None:
    """A model-returned attribute value as stripped text; None when missing, of another type or empty."""

    if raw is None:
        return None
    if isinstance(raw, bool):
        text = "true" if raw else "false"
    elif isinstance(raw, str | int | float):
        text = str(raw)
    else:
        return None
    return text.strip() or None


@dataclass(frozen=True, eq=False)
class Ontology:
    """A validated, immutable ontology: the merged files of build and the installed submodules."""

    name: str
    version: str
    framing: str
    exclude: tuple[str, ...]
    node_types: Mapping[str, NodeType]
    """By type key."""
    node_groups: Mapping[str, NodeGroup]
    relations: Mapping[str, Relation]
    """By relation name."""
    spine: Spine
    inference: InferenceSpec
    judge_prompts: Mapping[str, str] = field(repr=False)
    """Judge prompt text by reference (the file ``judge_prompts/<ref>.md``)."""
    declared_labels: tuple[str, ...] = ()
    """Node type labels in the order the ontology files declare them (tie-breaks use this order)."""
    conform: ConformSpec | None = None
    """The ``conform`` section; None when the file has none."""
    declared_relations: tuple[str, ...] = ()
    """Relation names in the order the ontology files declare them ("first declared" default pair)."""

    _by_label: Mapping[str, NodeType] = field(init=False, repr=False)
    _family_labels: Mapping[str, str] = field(init=False, repr=False)
    _legal: Mapping[tuple[str, str, str], Pair] = field(init=False, repr=False)
    _defaults: Mapping[tuple[str, str], tuple[str, ...]] = field(init=False, repr=False)
    _entity_models: Mapping[str, type[BaseModel]] = field(init=False, repr=False)
    _edge_models: Mapping[str, type[BaseModel]] = field(init=False, repr=False)
    _attribute_models: Mapping[str, type[BaseModel]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        by_label = {node_type.label: node_type for node_type in self.node_types.values()}
        families = {
            family: node_type.label for node_type in self.node_types.values() for family in node_type.id_families
        }
        legal: dict[tuple[str, str, str], Pair] = {}
        defaults: dict[tuple[str, str], tuple[str, ...]] = {}
        order = [name for name in self.declared_relations if name in self.relations]
        order += sorted(set(self.relations) - set(order))
        for name in order:
            for pair in self.relations[name].pairs:
                legal[(name, pair.source, pair.target)] = pair
                if pair.default:
                    defaults[(pair.source, pair.target)] = (*defaults.get((pair.source, pair.target), ()), name)
        values: dict[str, Any] = {
            "_by_label": MappingProxyType(by_label),
            "_family_labels": MappingProxyType(families),
            "_legal": MappingProxyType(legal),
            "_defaults": MappingProxyType(defaults),
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "_entity_models", MappingProxyType(self._build_entity_models()))
        object.__setattr__(self, "_edge_models", MappingProxyType(self._build_edge_models()))
        object.__setattr__(self, "_attribute_models", MappingProxyType(self._build_attribute_models()))

    # Names and ownership

    @property
    def labels(self) -> tuple[str, ...]:
        """Every declared label, sorted."""

        return tuple(sorted(self._by_label))

    @property
    def extracted_labels(self) -> tuple[str, ...]:
        return tuple(sorted(t.label for t in self.node_types.values() if t.extracted))

    @property
    def extracted_relations(self) -> tuple[str, ...]:
        return tuple(sorted(r.name for r in self.relations.values() if r.extracted))

    @property
    def hub_labels(self) -> frozenset[str]:
        """Labels of extracted types whose role is ``anchor`` or ``group``."""

        return frozenset(t.label for t in self.node_types.values() if t.role in HUB_ROLES)

    def is_declared(self, name: str) -> bool:
        """True for a label or relation the ontology declares; built-in names are not declared."""

        return name in self._by_label or name in self.relations

    def owner_of(self, name: str) -> str | None:
        """The owner of a declared label or relation; ``build`` for built-in names; else None."""

        if name in self._by_label:
            return self._by_label[name].owner
        if name in self.relations:
            return self.relations[name].owner
        if name in BUILT_IN_NAMES:
            return BUILD_OWNER
        return None

    def node_type(self, label: str) -> NodeType:
        try:
            return self._by_label[label]
        except KeyError:
            raise ConformanceError("ontology_unknown_label", label=label) from None

    def relation(self, name: str) -> Relation:
        try:
            return self.relations[name]
        except KeyError:
            raise ConformanceError("ontology_unknown_relation", relation=name) from None

    # Node lookups

    def requirement_scope(self, label: str) -> str:
        """The identity scope of ``label``: its group key when the group shares one scope, else itself."""

        return self.node_type(label).identity.scope

    def scope_labels(self, scope: str) -> frozenset[str]:
        """Every label whose identity scope is ``scope``."""

        return frozenset(t.label for t in self.node_types.values() if t.identity.scope == scope)

    def label_for_id_family(self, explicit_id: str) -> str | None:
        """The label whose id families name ``explicit_id`` (its leading letters), or None."""

        family = _family_of(explicit_id)
        return self._family_labels.get(family) if family else None

    def id_prefix(self, label: str) -> str:
        prefix = self.node_type(label).id_prefix
        if prefix is None:
            raise ConformanceError("ontology_no_id_format", label=label)
        return prefix

    def identity_rule(self, label: str) -> IdentityRule:
        return self.node_type(label).identity

    def attribute_specs(self, label: str) -> Mapping[str, AttributeSpec]:
        return self.node_type(label).attributes

    def attribute_default(self, label: str, attribute: str) -> str | None:
        """The declared ``default`` of an attribute; None when it has none or the label or attribute is
        unknown."""

        node_type = self._by_label.get(label)
        spec = node_type.attributes.get(attribute) if node_type else None
        return spec.default if spec else None

    def inherit_rules(self, label: str) -> Mapping[str, str]:
        """Attribute name to the ``inherit.via`` relation, for the label's inheriting attributes."""

        return MappingProxyType(
            {name: spec.inherit_via for name, spec in self.node_type(label).attributes.items() if spec.inherit_via}
        )

    def supported_by(self, label: str) -> tuple[Support, ...]:
        """The edges one of which a node of the label needs after normalisation; empty: no such rule."""

        return self.node_type(label).supported_by

    def merge_across_labels(self, label: str) -> bool:
        return self.node_type(label).merge_across_labels

    def normalise_attribute(self, label: str, name: str, raw: Any) -> str | None:
        """Normalise a model-returned attribute value; None when unknown, empty or not allowed."""

        spec = self.node_type(label).attributes.get(name)
        if spec is None:
            return None
        text = _attribute_text(raw)
        if text is None:
            return None
        if spec.kind == "text":
            return text[: spec.max_chars] if spec.max_chars else text
        if spec.kind == "enum":
            folded = text.casefold()
            for value in spec.values:
                if value.casefold() == folded:
                    return value
            return spec.aliases.get(folded)
        return text

    # Edges

    def pairs(self, relation: str) -> tuple[Pair, ...]:
        return self.relation(relation).pairs

    def is_legal(self, relation: str, source_label: str, target_label: str) -> bool:
        """True when the pair is declared for the relation (cues are not considered)."""

        return (relation, source_label, target_label) in self._legal

    def is_symmetric(self, relation: str) -> bool:
        return self.relation(relation).symmetric

    def default_relation(self, source_label: str, target_label: str) -> str | None:
        """The first declared default relation of the label pair, or None. Label only: it ignores the
        ends' attributes and the pairs' ``where``; ``default_for`` chooses by attributes."""

        defaults = self._defaults.get((source_label, target_label), ())
        return defaults[0] if defaults else None

    def default_relations(self, source_label: str, target_label: str) -> tuple[str, ...]:
        """Every default relation of the label pair, in declaration order."""

        return self._defaults.get((source_label, target_label), ())

    def pair_rule(self, relation: str, source_label: str, target_label: str) -> Pair | None:
        """The declared pair of ``relation`` from ``source_label`` to ``target_label`` with its flags."""

        return self._legal.get((relation, source_label, target_label))

    def impact_role(self, relation: str, source_label: str, target_label: str) -> str:
        """The impact role of an edge of ``relation`` stored from ``source_label`` to ``target_label``: its
        pair's, else (an undeclared pair) the relation's; ``none`` for an undeclared relation."""

        pair = self._legal.get((relation, source_label, target_label))
        if pair is not None:
            return pair.impact
        known = self.relations.get(relation)
        return known.impact if known is not None else NO_IMPACT

    def owner_pairs(self, source_label: str) -> tuple[Pair, ...]:
        """The ``owner`` pairs of a source label over all relations, lowest ``owner_rank`` first."""

        pairs = [pair for (_, source, _), pair in self._legal.items() if source == source_label and pair.owner]
        return tuple(sorted(pairs, key=lambda p: (p.owner_rank, p.relation, p.target)))

    def default_for(self, source: NodeView | str, target: NodeView | str) -> str | None:
        """The default relation for the ends, as ``legalise`` chooses it (cues aside) for an edge of no listed relation,
        so a ``default_from`` pair never applies: the first declared default whose ``where`` the ends meet, else the
        first declared; None when the label pair has none.
        """

        ordered = self._ordered_defaults(_view(source), _view(target), None)
        return ordered[0].relation if ordered else None

    def _ordered_defaults(self, source: NodeView, target: NodeView, extracted: str | None) -> list[Pair]:
        """The default pairs of the ends' labels that apply to an edge extracted as ``extracted`` (a
        ``default_from`` pair only when it lists it): those whose ``where`` holds first, each part in
        declaration order."""

        defaults = [
            pair
            for name in self.default_relations(source.label, target.label)
            if not (pair := self._legal[(name, source.label, target.label)]).default_from
            or extracted in pair.default_from
        ]
        held = [pair for pair in defaults if self.where_holds(pair, source, target)]
        return [*held, *(pair for pair in defaults if pair not in held)]

    def condition_matches(self, condition: Condition, node: NodeView | str) -> bool:
        """True when ``node`` matches a spine ``exclude`` or a rule's ``exclude_where``: every attribute of
        ``condition`` has a listed value, a null value read as the attribute's ``default``.
        """

        view = _view(node)
        if not condition:
            return False
        for attribute, allowed in condition.items():
            value = view.attributes.get(attribute)
            if value is None:
                value = self.attribute_default(view.label, attribute)
            if value is None or _value_text(value) not in allowed:
                return False
        return True

    def where_holds(self, pair: Pair, source: NodeView | str, target: NodeView | str) -> bool:
        """True when the ends meet the pair's ``source_where`` and ``target_where``: an end's value (or
        its attribute ``default`` when null) is listed; a null value without a default holds."""

        return self._meets(_view(source), pair.source_where) and self._meets(_view(target), pair.target_where)

    def _meets(self, node: NodeView, condition: Condition) -> bool:
        for attribute, allowed in condition.items():
            value = node.attributes.get(attribute)
            if value is None:
                value = self.attribute_default(node.label, attribute)
            if value is not None and _value_text(value) not in allowed:
                return False
        return True

    def legalise(
        self,
        relation: str,
        source: NodeView | str,
        target: NodeView | str,
        evidence_lines: Sequence[str],
    ) -> Legalised | Dropped:
        """Legalise one extracted edge."""

        source_view, target_view = _view(source), _view(target)
        source_label, target_label = source_view.label, target_view.label
        known = relation in self.relations
        if known:
            pair = self._legal.get((relation, source_label, target_label))
            if pair is not None and self._cue_ok(relation, pair, evidence_lines):
                return Legalised(relation, flipped=False)
            if not self.relations[relation].symmetric:
                reverse = self._legal.get((relation, target_label, source_label))
                if reverse is not None and self._cue_ok(relation, reverse, evidence_lines):
                    return Legalised(relation, flipped=True)
        for pair in self._ordered_defaults(source_view, target_view, relation):
            if self._cue_ok(pair.relation, pair, evidence_lines):
                return Legalised(pair.relation, flipped=False)
        if not known:
            return Dropped("unknown_relation")
        held = (relation, source_label, target_label) in self._legal or (
            relation,
            target_label,
            source_label,
        ) in self._legal
        return Dropped("cue_missing" if held else "illegal_pair")

    def cue_matches(self, relation: str, text: str) -> bool:
        """True when one of ``relation``'s cues occurs in ``text`` as whole words, ignoring case (the
        cue gate of a ``stated_only`` relation a ``cross_document`` rule writes); False for a relation
        without cues. An undeclared relation is a ``ConformanceError``."""

        return any(cue.search(text) for cue in self.relation(relation).cue_words)

    def cue_gated_rules(self, relation: str) -> frozenset[str]:
        """Ids of the inference rules that may write ``relation`` although it is ``stated_only``: the rules naming it in
        ``also_writes`` (only ``cross_document`` rules can, and only when the relation has cues; check 12).
        """

        if not self.relation(relation).stated_only:
            return frozenset()
        return frozenset(rule.id for rule in self.inference.rules if relation in rule.also_writes)

    def _cue_ok(self, relation: str, pair: Pair, evidence_lines: Sequence[str]) -> bool:
        if not pair.cue_required:
            return True
        cues = self.relations[relation].cues
        return any(cue.search(line) for cue in cues for line in evidence_lines)

    @property
    def exclusive_pairs(self) -> frozenset[tuple[str, str, str]]:
        """``(relation, source label, target label)`` of every ``exclusive`` pair."""

        return frozenset(key for key, pair in self._legal.items() if pair.exclusive)

    @property
    def derive_rules(self) -> tuple[DeriveRule, ...]:
        return tuple(rule for relation in self.relations.values() for rule in relation.derive)

    def derive_via_rules(self) -> tuple[DeriveViaRule, ...]:
        return tuple(r.derive_via for r in self.relations.values() if r.derive_via is not None)

    def acyclic_relations(self) -> frozenset[str]:
        return frozenset(r.name for r in self.relations.values() if r.acyclic)

    def stated_only_relations(self) -> frozenset[str]:
        return frozenset(r.name for r in self.relations.values() if r.stated_only)

    @property
    def id_reference_relations(self) -> tuple[str, ...]:
        return tuple(sorted(r.name for r in self.relations.values() if r.id_references))

    # Prompt-facing models

    def entity_models(self) -> dict[str, type[BaseModel]]:
        """Extracted types for the node prompt: label to a model whose docstring is the type's prompt text."""

        return dict(self._entity_models)

    def edge_models(self) -> dict[str, type[BaseModel]]:
        """Extracted relations for the edge prompt: name to a field-less model (docstring)."""

        return dict(self._edge_models)

    def edge_type_map(self) -> dict[tuple[str, str], list[str]]:
        """``(source label, target label)`` to the extracted relations legal on it."""

        extracted = set(self.extracted_labels)
        result: dict[tuple[str, str], list[str]] = {}
        for relation in sorted(self.relations.values(), key=lambda r: r.name):
            if not relation.extracted:
                continue
            for pair in relation.pairs:
                if pair.source in extracted and pair.target in extracted:
                    names = result.setdefault((pair.source, pair.target), [])
                    if relation.name not in names:
                        names.append(relation.name)
        return dict(sorted(result.items()))

    def attribute_output_model(self, label: str) -> type[BaseModel]:
        """Structured output model of the attributes pass for one extracted label."""

        try:
            return self._attribute_models[label]
        except KeyError:
            raise ConformanceError("ontology_no_attribute_model", label=label) from None

    def _build_entity_models(self) -> dict[str, type[BaseModel]]:
        models: dict[str, type[BaseModel]] = {}
        for node_type in sorted(self.node_types.values(), key=lambda t: t.label):
            if node_type.extracted:
                doc = "\n\n".join(part for part in (_clean(node_type.description), _clean(node_type.guidance)) if part)
                models[node_type.label] = create_model(node_type.label, __base__=_PromptModel, __doc__=doc)
        return models

    def _build_edge_models(self) -> dict[str, type[BaseModel]]:
        models: dict[str, type[BaseModel]] = {}
        for relation in sorted(self.relations.values(), key=lambda r: r.name):
            if relation.extracted:
                models[relation.name] = create_model(
                    relation.name, __base__=_PromptModel, __doc__=_clean(relation.description)
                )
        return models

    def _build_attribute_models(self) -> dict[str, type[BaseModel]]:
        models: dict[str, type[BaseModel]] = {}
        for node_type in sorted(self.node_types.values(), key=lambda t: t.label):
            if not node_type.extracted:
                continue
            fields: dict[str, Any] = {}
            for spec in node_type.attributes.values():
                if spec.name in _NOT_ASKED:
                    continue
                fields[spec.name] = (str | None, Field(default=None, description=_field_text(spec)))
            models[node_type.label] = create_model(
                f"{node_type.label}Attributes",
                __base__=_PromptModel,
                __doc__=f"Attribute values of one {node_type.label} node, null when the text gives none.",
                **fields,
            )
        return models

    # Prompt parts

    def fingerprint(self, part: FingerprintPart) -> str:
        """sha256 of the canonical JSON of the ontology fields that reach one kind of prompt."""

        extracted_types = [t for t in self.node_types.values() if t.extracted]
        extracted_types.sort(key=lambda t: t.label)
        corpus = {"framing": self.framing, "exclude": list(self.exclude)}
        if part == "nodes":
            groups = sorted({t.group for t in extracted_types if t.group})
            content: Any = {
                "corpus": corpus,
                "types": [
                    {
                        "label": t.label,
                        "role": t.role,
                        "group": t.group,
                        "id_families": list(t.id_families),
                        "description": t.description,
                        "guidance": t.guidance,
                    }
                    for t in extracted_types
                ],
                "groups": [
                    {
                        "key": key,
                        "description": self.node_groups[key].description,
                        "guidance": self.node_groups[key].guidance,
                    }
                    for key in groups
                    if key in self.node_groups
                ],
            }
        elif part == "edges":
            content = {
                "corpus": corpus,
                "labels": [t.label for t in extracted_types],
                "relations": [
                    {
                        "name": r.name,
                        "description": r.description,
                        "symmetric": r.symmetric,
                        "pairs": sorted([p.source, p.target] for p in r.pairs),
                        # Present only when set, so a file without the flag keeps its fingerprint.
                        **({"stated_only": True} if r.stated_only else {}),
                    }
                    for r in sorted(self.relations.values(), key=lambda r: r.name)
                    if r.extracted
                ],
            }
        elif part == "attributes":
            content = {
                "corpus": corpus,
                "types": [
                    {
                        "label": t.label,
                        "attributes": [
                            {
                                "name": a.name,
                                "kind": a.kind,
                                "description": a.description,
                                "max_chars": a.max_chars,
                                "values": list(a.values),
                                "value_descriptions": dict(a.value_descriptions),
                                "targets": sorted(a.target_labels),
                            }
                            for a in sorted(t.attributes.values(), key=lambda a: a.name)
                        ],
                    }
                    for t in extracted_types
                ],
            }
        elif part == "resolution":
            content = {
                "corpus": corpus,
                "types": [
                    {
                        "label": t.label,
                        "role": t.role,
                        "identity": [t.identity.key, t.identity.scope, t.identity.fallback],
                        "description": t.description,
                    }
                    for t in extracted_types
                ],
            }
        else:
            raise ConformanceError("ontology_unknown_fingerprint_part", part=str(part))
        return _canonical_hash({"part": part, "content": content})

    # Analysis

    @property
    def spine_relations(self) -> frozenset[str]:
        return frozenset(name for path in self.spine.paths for name in path.relations)

    def spine_exclude(self) -> Condition:
        return self.spine.exclude

    def conform_spec(self) -> ConformSpec | None:
        return self.conform

    def judge_prompt(self, ref: str) -> str:
        try:
            return self.judge_prompts[ref]
        except KeyError:
            raise ConformanceError("ontology_unknown_judge_prompt", prompt=ref) from None

    def judge_prompt_version(self, ref: str) -> str:
        """``<ref>@<first 12 hex of the prompt text's SHA-256>``: names the exact prompt text, so stored
        judgements keyed under it are dropped when the prompt file changes."""

        digest = hashlib.sha256(self.judge_prompt(ref).encode("utf-8")).hexdigest()[:PROMPT_DIGEST_LENGTH]
        return f"{ref}@{digest}"


def _view(node: NodeView | str) -> NodeView:
    return NodeView(node) if isinstance(node, str) else node


def _value_text(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _field_text(spec: AttributeSpec) -> str:
    text = _clean(spec.description)
    if spec.kind == "text" and spec.max_chars:
        return f"{text} (at most {spec.max_chars} characters)".strip()
    if spec.kind == "enum":
        options = "; ".join(
            f"{value}: {spec.value_descriptions[value]}" if spec.value_descriptions.get(value) else value
            for value in spec.values
        )
        return f"{text}. One of: {options}".strip()
    if spec.kind == "ref":
        return f"{text} (its name as written)".strip()
    return text
