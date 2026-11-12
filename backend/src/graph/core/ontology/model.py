"""Models of the ontology file format."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

Owner = Literal["build", "gap", "plan"]
Origin = Literal["extracted", "analysis", "human", "agent", "planner"]
Tier = Literal["context", "spec", "governance", "plan"]
Role = Literal["anchor", "group", "atomic"]
AttributeKind = Literal["text", "enum", "ref"]
IdentityKey = Literal["id", "surface"]
IdentityScope = Literal["type", "group"]
Direction = Literal["in", "out"]
ImpactRole = Literal["uses", "used_by", "coupled", "related", "contains", "none"]
"""How a relation pair carries a change, for ``GraphReader.trace`` (trace.md "Impact roles")."""

FORMAT_VERSION: Final = 2
"""The ontology file format this library reads."""

OWNER_ORIGINS: Final[dict[str, frozenset[str]]] = {
    "build": frozenset({"extracted"}),
    "gap": frozenset({"analysis", "human", "agent"}),
    "plan": frozenset({"planner"}),
}
"""The origins each owner may declare."""

TEMPLATE_PARAMETERS: Final[dict[str, frozenset[str]]] = {
    "endpoints": frozenset({"subject", "via", "mentions", "target", "min_targets", "max_targets", "exclude_owned_by"}),
    "realization": frozenset({"subject", "member_relation", "container_paths", "container"}),
    "attach_orphan": frozenset(
        {"subject", "targets", "max_candidates", "exclude_where", "target_exclude_where", "batch", "batch_prompt"}
    ),
    "cross_document": frozenset(
        {
            "subject",
            "members",
            "via",
            "mentions",
            "min_documents",
            "max_pairs",
            "pair_floor",
            "max_candidates",
            "exclude_where",
            "also_writes",
        }
    ),
}
"""The inference templates and the parameters each one takes."""

OPTIONAL_TEMPLATE_PARAMETERS: Final[dict[str, frozenset[str]]] = {
    "endpoints": frozenset({"mentions", "exclude_owned_by"}),
    "attach_orphan": frozenset({"exclude_where", "target_exclude_where", "batch", "batch_prompt"}),
    "cross_document": frozenset({"mentions", "exclude_where", "also_writes"}),
}
"""Template parameters a rule may leave out."""
CROSS_DOCUMENT: Final = "cross_document"
"""The template whose rules may name ``also_writes`` and a list of ``subject`` keys."""
REALIZATION: Final = "realization"
"""The template whose rules may name ``reads`` (check 16)."""
RULE_FIELDS: Final = frozenset({"id", "template", "prompt", "writes", "cap", "first_cap", "floor", "reads"})
"""Fields every rule has, whatever its template; the rest are template parameters."""

Values = Annotated[list[str], Field(min_length=1)]
Condition = dict[str, Values]
"""``{attribute: [values]}``: a ``where``, spine ``exclude`` or ``exclude_where`` condition."""


class FormatModel(BaseModel):
    """Base of every format model: frozen, strict types, no unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class CorpusDef(FormatModel):
    framing: str = ""
    exclude: list[str] = Field(default_factory=list)


class InheritDef(FormatModel):
    """Read-time inheritance: a null value is filled from the owner one ``via`` hop away."""

    via: str


class AttributeDef(FormatModel):
    """One attribute; which of the optional fields apply depends on ``type`` (checked by the loader)."""

    type: AttributeKind
    description: str = ""
    max_chars: int | None = Field(default=None, gt=0)
    values: str | list[str] | dict[str, str] | None = None
    aliases: dict[str, str] = Field(default_factory=dict)
    target: str | None = None
    store: bool = True
    default: str | None = None
    inherit: InheritDef | None = None


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


class SupportDef(FormatModel):
    """An edge that supports a node: ``relation`` ending at it (``in``) or starting at it (``out``)."""

    relation: str
    direction: Direction


class NodeTypeDef(FormatModel):
    label: str
    group: str | None = None
    origin: Origin | None = None
    tier: Tier | None = None
    role: Role | None = None
    id: IdDef | None = None
    id_families: list[str] = Field(default_factory=list)
    identity: IdentityDef | None = None
    merge_across_labels: bool = True
    supported_by: list[SupportDef] = Field(default_factory=list)
    description: str = ""
    guidance: str = ""
    attributes: dict[str, AttributeDef] = Field(default_factory=dict)


class PairDef(FormatModel):
    source: str = Field(alias="from")
    to: str
    default: bool = False
    default_from: list[str] = Field(default_factory=list)
    cue_required: bool = False
    exclusive: bool = False
    owner: bool = False
    owner_rank: int = Field(default=1, ge=1)
    source_where: Condition = Field(default_factory=dict)
    target_where: Condition = Field(default_factory=dict)
    where_reason: str = "condition_failed"
    otherwise: str | None = None
    max_depth: int | None = Field(default=None, ge=1)
    not_across: list[str] = Field(default_factory=list)
    distinct_owner: bool = False
    redundant_via_owner: bool = False
    impact: ImpactRole | None = None


class DeriveViaDef(FormatModel):
    """An implied edge from each module a hub is ``consumer`` of to the module it is ``provider`` of."""

    consumer: str
    provider: str


class EdgeTypeDef(FormatModel):
    origin: Origin
    tier: Tier
    description: str = ""
    pairs: list[PairDef] = Field(min_length=1)
    symmetric: bool = False
    acyclic: bool = False
    stated_only: bool = False
    id_references: bool = False
    cues: list[str] = Field(default_factory=list)
    derive: list[str] = Field(default_factory=list)
    derive_via: DeriveViaDef | None = None
    impact: ImpactRole | None = None


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
    exclude: Condition = Field(default_factory=dict)


class AnsweredElsewhereDef(FormatModel):
    """The answered-elsewhere check of Conform."""

    label: str
    prompt: str
    candidates: int = Field(gt=0)
    min_cosine: float = Field(ge=0.0, le=1.0)
    floor: float = Field(ge=0.0, le=1.0)
    cap: int = Field(gt=0)


class ConformDef(FormatModel):
    """The owner-conflict judge of normalisation and the relation conflict losers become."""

    judge_prompt: str
    cap: int = Field(gt=0)
    floor: float = Field(ge=0.0, le=1.0)
    fallback_relation: str
    answered_elsewhere: AnsweredElsewhereDef | None = None


class InferenceRuleDef(FormatModel):
    """One rule; which parameters apply depends on ``template`` (checked by the loader)."""

    id: str = Field(min_length=1)
    template: str
    prompt: str
    writes: str
    cap: int | None = Field(default=None, gt=0)
    first_cap: int | None = Field(default=None, gt=0)
    floor: float | None = Field(default=None, ge=0.0, le=1.0)
    reads: list[str] | None = Field(default=None, min_length=1)
    subject: str | list[str] | None = None
    members: str | None = None
    via: list[str] | None = None
    mentions: bool | None = None
    target: str | None = None
    min_targets: int | None = Field(default=None, ge=1)
    max_targets: int | None = Field(default=None, ge=1)
    member_relation: str | None = None
    container: str | None = None
    container_paths: list[SpinePathDef] | None = Field(default=None, min_length=1)
    exclude_owned_by: list[str] | None = Field(default=None, min_length=1)
    targets: list[str] | None = None
    max_candidates: int | None = Field(default=None, ge=1)
    exclude_where: Condition | None = None
    target_exclude_where: Condition | None = None
    min_documents: int | None = Field(default=None, ge=2)
    max_pairs: int | None = Field(default=None, ge=1)
    pair_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    also_writes: list[str] | None = Field(default=None, min_length=1)
    batch: int | None = Field(default=None, ge=2)
    batch_prompt: str | None = None


class InferenceDef(FormatModel):
    cap: int = Field(gt=0)
    first_cap: int | None = Field(default=None, gt=0)
    first_budget: int | None = Field(default=None, gt=0)
    floor: float = Field(ge=0.0, le=1.0)
    rules: list[InferenceRuleDef] = Field(default_factory=list)


class FileHeaderDef(FormatModel):
    """Top level of one file; sections are kept raw and validated entry by entry."""

    format_version: Literal[2]
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    owner: Owner
    corpus: Any = None
    vocabularies: dict[str, Any] = Field(default_factory=dict)
    node_groups: dict[str, Any] = Field(default_factory=dict)
    node_types: dict[str, Any] = Field(default_factory=dict)
    edge_types: dict[str, Any] = Field(default_factory=dict)
    spine: Any = None
    conform: Any = None
    inference: Any = None
