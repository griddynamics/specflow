"""Models of the ontology file format (docs/graph/ontology.md "Format").

These models describe one YAML file as written, before groups are expanded, files are merged or
references are resolved. Every model is frozen, strict and forbids unknown keys, so check 1 (known keys,
required fields, value types) is pydantic validation; the loader validates each entry on its own so
that one bad entry does not hide the problems of the others.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

Owner = Literal["build", "gap", "plan"]
Origin = Literal["extracted", "analysis", "human", "planner"]
Tier = Literal["context", "spec", "governance", "plan"]
Role = Literal["anchor", "group", "atomic"]
AttributeKind = Literal["text", "enum", "ref"]
IdentityKey = Literal["id", "surface"]
IdentityScope = Literal["type", "group"]

OWNER_ORIGINS: Final[dict[str, frozenset[str]]] = {
    "build": frozenset({"extracted"}),
    "gap": frozenset({"analysis", "human"}),
    "plan": frozenset({"planner"}),
}
"""The origins each owner may declare (docs/graph/architecture.md "Write ownership")."""

TEMPLATE_PARAMETERS: Final[dict[str, frozenset[str]]] = {
    "endpoints": frozenset({"subject", "via", "mentions", "target", "min_targets", "max_targets"}),
    "realization": frozenset({"subject", "member_relation", "container_relation", "container", "max_hops"}),
    "attach_orphan": frozenset({"subject", "targets", "max_candidates"}),
}
"""The inference templates (code, docs/graph/analysis.md) and the parameters each one takes."""

OPTIONAL_TEMPLATE_PARAMETERS: Final[dict[str, frozenset[str]]] = {
    "endpoints": frozenset({"mentions"}),
}
"""Template parameters a rule may leave out (docs/graph/ontology.md "Inference templates")."""


class FormatModel(BaseModel):
    """Base of every format model: frozen, strict types, no unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CorpusDef(FormatModel):
    framing: str = ""
    exclude: list[str] = Field(default_factory=list)


class AttributeDef(FormatModel):
    """One attribute; which of the optional fields apply depends on ``type`` (checked by the loader)."""

    type: AttributeKind
    description: str = ""
    max_chars: int | None = Field(default=None, gt=0)
    values: str | list[str] | dict[str, str] | None = None
    aliases: dict[str, str] = Field(default_factory=dict)
    target: str | None = None
    store: bool = True


class IdDef(FormatModel):
    format: str


class IdentityDef(FormatModel):
    key: IdentityKey
    scope: IdentityScope = "type"


class NodeGroupDef(FormatModel):
    """A node group: fields its member types inherit; never a label."""

    origin: Origin | None = None
    tier: Tier | None = None
    role: Role | None = None
    identity: IdentityDef | None = None
    description: str = ""
    guidance: str = ""
    attributes: dict[str, AttributeDef] = Field(default_factory=dict)


class NodeTypeDef(FormatModel):
    label: str
    group: str | None = None
    origin: Origin | None = None
    tier: Tier | None = None
    role: Role | None = None
    id: IdDef | None = None
    id_families: list[str] = Field(default_factory=list)
    identity: IdentityDef | None = None
    description: str = ""
    guidance: str = ""
    attributes: dict[str, AttributeDef] = Field(default_factory=dict)


class PairDef(FormatModel):
    source: str = Field(alias="from")
    to: str
    default: bool = False
    cue_required: bool = False
    exclusive: bool = False


class EdgeTypeDef(FormatModel):
    origin: Origin
    tier: Tier
    description: str = ""
    pairs: list[PairDef] = Field(min_length=1)
    symmetric: bool = False
    id_references: bool = False
    cues: list[str] = Field(default_factory=list)
    derive: list[str] = Field(default_factory=list)


class SpinePathDef(FormatModel):
    """``{via: [...], min, max}`` or ``{sequence: [...]}``; exactly one form (checked by the loader)."""

    via: list[str] | None = None
    sequence: list[str] | None = None
    min: int | None = Field(default=None, ge=1)
    max: int | None = Field(default=None, ge=1)


class SpineDef(FormatModel):
    sources: list[str] = Field(min_length=1)
    roots: list[str] = Field(min_length=1)
    paths: list[SpinePathDef] = Field(min_length=1)


class InferenceRuleDef(FormatModel):
    """One rule; which parameters apply depends on ``template`` (checked by the loader)."""

    id: str = Field(min_length=1)
    template: str
    prompt: str
    writes: str
    subject: str | None = None
    via: list[str] | None = None
    mentions: bool | None = None
    target: str | None = None
    min_targets: int | None = Field(default=None, ge=1)
    max_targets: int | None = Field(default=None, ge=1)
    member_relation: str | None = None
    container_relation: str | None = None
    container: str | None = None
    max_hops: int | None = Field(default=None, ge=1)
    targets: list[str] | None = None
    max_candidates: int | None = Field(default=None, ge=1)


class InferenceDef(FormatModel):
    cap: int = Field(gt=0)
    floor: float = Field(ge=0.0, le=1.0)
    rules: list[InferenceRuleDef] = Field(default_factory=list)


class FileHeaderDef(FormatModel):
    """Top level of one file; sections are kept raw and validated entry by entry."""

    format_version: Literal[1]
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    owner: Owner
    corpus: Any = None
    vocabularies: dict[str, Any] = Field(default_factory=dict)
    node_groups: dict[str, Any] = Field(default_factory=dict)
    node_types: dict[str, Any] = Field(default_factory=dict)
    edge_types: dict[str, Any] = Field(default_factory=dict)
    spine: Any = None
    inference: Any = None
