"""The validated, immutable ``Ontology`` and its parts (docs/graph/ontology.md, tech-specs 4.4).

The loader builds an ``Ontology`` from merged, checked files; nothing here re-validates. Groups are
already expanded to labels, so every lookup works on labels and relation names. Library code knows
this format, never its content: no label, relation or id prefix appears here as a literal.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from graph.core.errors import ConformanceError
from graph.core.identity import normalise_id

BUILT_IN_LABELS: Final = frozenset({"SpecNode", "Document", "Fragment"})
BUILT_IN_RELATIONS: Final = frozenset({"CONTAINS", "REFERENCED_BY", "MENTIONED_IN"})
BUILT_IN_NAMES: Final = BUILT_IN_LABELS | BUILT_IN_RELATIONS
EVIDENCE_LABELS: Final = frozenset({"Document", "Fragment"})
"""Built-in labels a relation of another owner than build may end at, to cite evidence."""

BUILD_OWNER: Final = "build"
EXTRACTED_ORIGIN: Final = "extracted"
HUB_ROLES: Final = frozenset({"anchor", "group"})

FingerprintPart = Literal["nodes", "edges", "attributes", "resolution"]
DropReason = Literal["unknown_relation", "illegal_pair", "cue_missing"]

_ID_FAMILY = re.compile(r"^[A-Z]+")


@dataclass(frozen=True)
class IdentityRule:
    """How a node's identity key is formed.

    ``key`` is ``id`` (explicit id, falling back to ``fallback``) or ``surface``; ``scope`` is the
    resolved scope name: the group key for ``scope: group``, else the label.
    """

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


@dataclass(frozen=True)
class Pair:
    """One legal ``(source label, target label)`` pair of a relation, groups expanded."""

    source: str
    target: str
    default: bool = False
    cue_required: bool = False
    exclusive: bool = False


@dataclass(frozen=True)
class DeriveRule:
    """Each resolved ``attribute`` reference of a source node yields a derived ``relation`` edge."""

    relation: str
    source_labels: frozenset[str]
    attribute: str
    target_labels: frozenset[str]


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
    derive: tuple[DeriveRule, ...] = ()

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


@dataclass(frozen=True)
class InferenceRule:
    """One inference rule with endpoints resolved to labels.

    Per template (docs/graph/ontology.md): ``endpoints`` uses ``subject``, ``via`` (relations from the
    subject to a target), ``mentions`` (targets mentioned in the subject's evidence fragments count
    too), ``targets`` (its ``target``), ``min_targets``, ``max_targets``; ``realization`` uses ``subject``,
    ``member_relation``, ``container_relation``, ``targets`` (its ``container``), ``max_hops``;
    ``attach_orphan`` uses ``subject``, ``targets``, ``max_candidates``.
    """

    id: str
    template: str
    prompt: str
    writes: str
    subject: frozenset[str]
    targets: frozenset[str]
    via: tuple[str, ...] = ()
    mentions: bool = False
    member_relation: str | None = None
    container_relation: str | None = None
    min_targets: int | None = None
    max_targets: int | None = None
    max_hops: int | None = None
    max_candidates: int | None = None


@dataclass(frozen=True)
class InferenceSpec:
    cap: int
    floor: float
    rules: tuple[InferenceRule, ...]


@dataclass(frozen=True)
class Legalised:
    """The edge is kept as ``relation``; ``flipped`` means source and target swap."""

    relation: str
    flipped: bool


@dataclass(frozen=True)
class Dropped:
    reason: DropReason


class _GraphitiModel(BaseModel):
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
    """Judge prompt text by reference ``<name>.<revision>``."""
    declared_labels: tuple[str, ...] = ()
    """Node type labels in the order the ontology files declare them (tie-breaks use this order)."""

    _by_label: Mapping[str, NodeType] = field(init=False, repr=False)
    _family_labels: Mapping[str, str] = field(init=False, repr=False)
    _legal: Mapping[tuple[str, str, str], Pair] = field(init=False, repr=False)
    _defaults: Mapping[tuple[str, str], str] = field(init=False, repr=False)
    _entity_models: Mapping[str, type[BaseModel]] = field(init=False, repr=False)
    _edge_models: Mapping[str, type[BaseModel]] = field(init=False, repr=False)
    _attribute_models: Mapping[str, type[BaseModel]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        by_label = {node_type.label: node_type for node_type in self.node_types.values()}
        families = {
            family: node_type.label for node_type in self.node_types.values() for family in node_type.id_families
        }
        legal: dict[tuple[str, str, str], Pair] = {}
        defaults: dict[tuple[str, str], str] = {}
        for relation in self.relations.values():
            for pair in relation.pairs:
                legal[(relation.name, pair.source, pair.target)] = pair
                if pair.default:
                    defaults[(pair.source, pair.target)] = relation.name
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

    def normalise_attribute(self, label: str, name: str, raw: Any) -> str | None:
        """Normalise a model-returned attribute value; None when unknown, empty or not allowed.

        Text is stripped and cut to ``max_chars``; an enum value is matched case-insensitively to
        the declared values, then to the aliases; a reference is the stripped name it gives.
        """

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
        return self._defaults.get((source_label, target_label))

    def legalise(
        self,
        relation: str,
        source_label: str,
        target_label: str,
        evidence_lines: Sequence[str],
    ) -> Legalised | Dropped:
        """Legalise one extracted edge (docs/graph/build.md "Conform", step 3).

        In order: keep a legal relation; flip a reversed one (never a symmetric relation); use the
        pair's default relation; a ``cue_required`` pair is only chosen when one of the relation's
        cues matches an evidence line; otherwise drop with a reason.
        """

        known = relation in self.relations
        if known:
            pair = self._legal.get((relation, source_label, target_label))
            if pair is not None and self._cue_ok(relation, pair, evidence_lines):
                return Legalised(relation, flipped=False)
            if not self.relations[relation].symmetric:
                reverse = self._legal.get((relation, target_label, source_label))
                if reverse is not None and self._cue_ok(relation, reverse, evidence_lines):
                    return Legalised(relation, flipped=True)
        default = self._defaults.get((source_label, target_label))
        if default is not None:
            pair = self._legal[(default, source_label, target_label)]
            if self._cue_ok(default, pair, evidence_lines):
                return Legalised(default, flipped=False)
        if not known:
            return Dropped("unknown_relation")
        held = (relation, source_label, target_label) in self._legal or (
            relation,
            target_label,
            source_label,
        ) in self._legal
        return Dropped("cue_missing" if held else "illegal_pair")

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

    @property
    def id_reference_relations(self) -> tuple[str, ...]:
        return tuple(sorted(r.name for r in self.relations.values() if r.id_references))

    # Graphiti-facing models

    def entity_models(self) -> dict[str, type[BaseModel]]:
        """Extracted types for Graphiti ``extract_nodes``: label to a model whose docstring is the prompt.

        Graphiti reads only the docstring of an entity type; attributes are filled by a separate
        pass (``attribute_output_model``), so the models have no fields.
        """

        return dict(self._entity_models)

    def edge_models(self) -> dict[str, type[BaseModel]]:
        """Extracted relations for Graphiti ``extract_edges``: name to a field-less model (docstring)."""

        return dict(self._edge_models)

    def edge_type_map(self) -> dict[tuple[str, str], list[str]]:
        """``(source label, target label)`` to the extracted relations legal on it.

        Groups are expanded; symmetric relations appear in both directions (their pairs hold both
        ways). Only pairs between extracted types are listed.
        """

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
        """Structured output model of the attributes pass for one extracted label.

        Every attribute is an optional string; enum fields list their values in the field
        description and are normalised afterwards with ``normalise_attribute``.
        """

        try:
            return self._attribute_models[label]
        except KeyError:
            raise ConformanceError("ontology_no_attribute_model", label=label) from None

    def _build_entity_models(self) -> dict[str, type[BaseModel]]:
        models: dict[str, type[BaseModel]] = {}
        for node_type in sorted(self.node_types.values(), key=lambda t: t.label):
            if node_type.extracted:
                doc = "\n\n".join(part for part in (_clean(node_type.description), _clean(node_type.guidance)) if part)
                models[node_type.label] = create_model(node_type.label, __base__=_GraphitiModel, __doc__=doc)
        return models

    def _build_edge_models(self) -> dict[str, type[BaseModel]]:
        models: dict[str, type[BaseModel]] = {}
        for relation in sorted(self.relations.values(), key=lambda r: r.name):
            if relation.extracted:
                models[relation.name] = create_model(
                    relation.name, __base__=_GraphitiModel, __doc__=_clean(relation.description)
                )
        return models

    def _build_attribute_models(self) -> dict[str, type[BaseModel]]:
        models: dict[str, type[BaseModel]] = {}
        for node_type in sorted(self.node_types.values(), key=lambda t: t.label):
            if not node_type.extracted:
                continue
            fields: dict[str, Any] = {}
            for spec in node_type.attributes.values():
                fields[spec.name] = (str | None, Field(default=None, description=_field_text(spec)))
            models[node_type.label] = create_model(
                f"{node_type.label}Attributes",
                __base__=_GraphitiModel,
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

    def judge_prompt(self, ref: str) -> str:
        try:
            return self.judge_prompts[ref]
        except KeyError:
            raise ConformanceError("ontology_unknown_judge_prompt", prompt=ref) from None


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
