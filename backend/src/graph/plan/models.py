"""Plan-side change reports, run records and revision models.

The host converts the build's change report into ``BuildChangeReport`` before it is given to
``PlanContext``: ``graph.plan`` never imports ``graph.build``. Every model is frozen, rejects unknown
fields and round-trips through JSON. Mapping fields are normalised to sorted mappings, so their dump
is deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from graph.core.errors import ConfigurationError
from graph.plan.names import (
    AreaKind,
    AreaStatus,
    BlockReason,
    Complexity,
    DoneWhenSource,
    InterfaceKind,
    NotBuildableReason,
    Phase,
    PlanMode,
    PlannedBy,
    StoryKind,
    StoryStatus,
    TaskStatus,
)

Basis = Literal["stated", "implied", "inferred"]
ChangeClass = Literal[
    "added",
    "removed",
    "merged",
    "carried_over",
    "rekeyed",
    "changed_meaning",
    "changed_links",
    "changed_evidence",
]
ChangedEdgeKind = Literal["added", "removed", "changed"]
PlanStepName = Literal[
    "snapshot",
    "partition",
    "describe",
    "project",
    "tasks",
    "stitch",
    "review",
    "diff",
    "persist",
    "check",
]
StepState = Literal["pending", "running", "done", "failed"]
PlanRunStatus = Literal["running", "succeeded", "partially_succeeded", "failed"]


class PlanModel(BaseModel):
    """Base of every plan model: frozen, no unknown fields, mappings sorted on dump."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _sorted_mapping(cls, value: object) -> object:
        if isinstance(value, Mapping):
            return {key: value[key] for key in sorted(value)}
        return value


class PlanOptions(PlanModel):
    """Options that shape planning; ``review_model`` is excluded from reuse keys and fingerprints."""

    review: bool = True
    task_size: int = Field(default=5, ge=1)
    planner_members: int = Field(default=40, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=5)
    review_model: str | None = None

    def __init__(self, **data: Any) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            raise ConfigurationError("invalid_options") from error


class ChangedSpecNode(PlanModel):
    """One changed spec node, by id, with its build's change classes."""

    spec_id: str = Field(min_length=1)
    classes: tuple[ChangeClass, ...]
    previous_spec_ids: tuple[str, ...] = ()


class ChangedSpecEdge(PlanModel):
    """One changed spec edge, ends by ``spec_id``; inferred edges included."""

    source: str = Field(min_length=1)
    relation: str = Field(min_length=1)
    target: str = Field(min_length=1)
    change: ChangedEdgeKind
    basis: Basis


class BuildChangeReport(PlanModel):
    """What one build changed, as the host passes it to a plan run."""

    build_id: str = Field(min_length=1)
    previous_build_id: str | None = None
    nodes: tuple[ChangedSpecNode, ...] = ()
    edges: tuple[ChangedSpecEdge, ...] = ()


class PlanRunRecord(PlanModel):
    """One plan run: frozen inputs, versions, step progress and final status."""

    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    mode: PlanMode
    replan: bool
    build_id: str = Field(min_length=1)
    options: PlanOptions
    rules_version: str = Field(min_length=1)
    plan_ontology_version: str = Field(min_length=1)
    fingerprint: str | None = None
    started_at: AwareDatetime
    progress: dict[PlanStepName, StepState] = Field(default_factory=dict)
    status: PlanRunStatus = "running"
    failure: str | None = None

    @property
    def finished(self) -> bool:
        """Whether the run has a terminal status."""

        return self.status in ("succeeded", "partially_succeeded", "failed")


class MeaningRevisions(PlanModel):
    """The build id a changed spec node was last consumed under, and that build chain."""

    nodes: dict[str, str] = Field(default_factory=dict)
    consumed: tuple[str, ...] = ()


class DoneWhen(PlanModel):
    """One checkable completion statement."""

    text: str = Field(min_length=1)
    source: DoneWhenSource


class PlanInfo(PlanModel):
    """The persisted plan head of a project."""

    plan_id: str = Field(min_length=1)
    profile: str | None = None
    last_build_id: str | None = None
    graph_fingerprint: str | None = None
    ontology_version: str = Field(min_length=1)
    plan_ontology_version: str = Field(min_length=1)
    rules_version: str = Field(min_length=1)
    applying: bool = False
    last_run_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class WorkAreaInfo(PlanModel):
    """A persisted work area, without derived aggregates."""

    area_id: str = Field(min_length=1)
    kind: AreaKind
    anchor: str | None = None
    title: str | None = None
    description: str | None = None
    interfaces: tuple[dict[str, Any], ...] = ()
    not_buildable: tuple[dict[str, Any], ...] = ()
    planned_by: PlannedBy
    status: AreaStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_run_id: str | None = None


class StoryInfo(PlanModel):
    """A persisted story, without derived progress."""

    story_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    kind: StoryKind
    anchor: str | None = None
    chapter: ChapterRef | None = None
    title: str = Field(min_length=1)
    summary: str | None = None
    release: str | None = None
    status: StoryStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_run_id: str | None = None


class CurrentTask(PlanModel):
    """A persisted task as Snapshot and Diff see it."""

    task_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    revision: int = Field(ge=1)
    story_id: str = Field(min_length=1)
    phase: Phase
    title: str = Field(min_length=1)
    description: str | None = None
    done_when: tuple[DoneWhen, ...] = ()
    complexity: Complexity
    implements: tuple[str, ...] = ()
    release: tuple[str, ...] = ()
    has_open_questions: bool = False
    planned_by: PlannedBy
    prompt_ref: str | None = None
    spec_build_id: str | None = None
    status: TaskStatus
    claimed_by: str | None = None
    claimed_at: AwareDatetime | None = None
    attempts: int = Field(default=0, ge=0)
    commit: str | None = None
    block_reason: BlockReason | None = None
    note: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_run_id: str | None = None
    last_run_id: str | None = None


class PlanLink(PlanModel):
    """One persisted plan relationship with its stored key and endpoint ids."""

    type: str = Field(min_length=1)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    edge_key: str = Field(min_length=1)
    properties: tuple[tuple[str, Any], ...] = ()


class CurrentPlan(PlanModel):
    """The current persisted plan that Snapshot and Diff compare with."""

    plan: PlanInfo | None
    areas: tuple[WorkAreaInfo, ...] = ()
    stories: tuple[StoryInfo, ...] = ()
    tasks: tuple[CurrentTask, ...] = ()
    links: tuple[PlanLink, ...] = ()


class ChangeSet(PlanModel):
    """Merged change reports, as the artifact stores them."""

    meaning: dict[str, ChangeClass] = {}
    links: frozenset[str] = frozenset()


class ProvenanceView(PlanModel):
    """One evidence fragment of a spec node, as Snapshot presents it to planners."""

    fragment_id: str = Field(min_length=1)
    document_id: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    quote: str | None = None


class ChapterRef(PlanModel):
    """The source document and chapter of a spec node."""

    document: str = Field(min_length=1)
    heading: str = Field(min_length=1)


class SpecNodeView(PlanModel):
    """A plannable or context spec node, without evidence text."""

    spec_id: str = Field(min_length=1)
    uuid: str = Field(min_length=1)
    label: str = Field(min_length=1)
    name: str = Field(min_length=1)
    attributes: dict[str, Any] = {}
    statements: tuple[dict[str, Any], ...] = ()
    provenance: tuple[ProvenanceView, ...] = ()
    aliases: tuple[str, ...] = ()
    last_build_id: str | None = None
    chapter: ChapterRef | None = None


class SpecEdgeView(PlanModel):
    """A domain edge touching a plannable node."""

    relation: str = Field(min_length=1)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    basis: str | None = None
    confidence: float | None = None
    edge_key: str = Field(min_length=1)


class SnapshotArtifact(PlanModel):
    """Everything Snapshot and the later steps need."""

    mode: PlanMode
    unchanged: bool = False
    build_id: str = Field(min_length=1)
    plannable: tuple[SpecNodeView, ...] = ()
    context: tuple[SpecNodeView, ...] = ()
    edges: tuple[SpecEdgeView, ...] = ()
    aliases: dict[str, str] = {}
    current: CurrentPlan
    change_set: ChangeSet
    revisions: MeaningRevisions
    fingerprint: str = Field(min_length=1)
    previous_open: bool = False


class AreaPlan(PlanModel):
    """A target work area, before Diff turns it into writes."""

    area_id: str = Field(min_length=1)
    kind: AreaKind
    anchor: str | None = None
    status: AreaStatus
    members: tuple[str, ...] = ()


class StoryPlan(PlanModel):
    """A target story, before Diff turns it into writes."""

    story_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    kind: StoryKind
    anchor: str | None = None
    chapter: ChapterRef | None = None
    title: str = Field(min_length=1)
    release: str | None = None
    members: tuple[str, ...] = ()
    ordinal: int = Field(ge=1)
    status: StoryStatus


class Placement(PlanModel):
    """One member's area, the rule that placed it and the edges it followed."""

    area_id: str = Field(min_length=1)
    rule: int
    via: tuple[str, ...] = ()


class PlanningPart(PlanModel):
    """Whole stories (or one chunk of a large story) planned by one Tasks call."""

    part_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    number: int = Field(ge=1)
    story_ids: tuple[str, ...]
    members: tuple[str, ...]


class PartitionArtifact(PlanModel):
    """The pure result of the partition rules 1-7, with the stories and planning parts."""

    areas: tuple[AreaPlan, ...] = ()
    stories: tuple[StoryPlan, ...] = ()
    placements: dict[str, Placement] = {}
    home_story: dict[str, str] = {}
    parts: tuple[PlanningPart, ...] = ()
    counts: dict[str, int] = {}


class AreaInterface(PlanModel):
    """What an area offers other areas: a spec Interface (``planned_by`` rules) or one the describer adds."""

    name: str = Field(min_length=1)
    kind: InterfaceKind
    spec_id: str | None = None
    spec_ids: tuple[str, ...] = ()
    summary: str | None = None
    planned_by: PlannedBy


class PlannerOutcome(PlanModel):
    """How one planner answer came about: model calls, reuse, fallback and invalid answers."""

    calls: int = 0
    reused: bool = False
    fallback: bool = False
    invalid: int = 0
    problems: tuple[str, ...] = ()


class PlannedArea(PlanModel):
    """Title, description and interfaces of an area."""

    title: str = Field(min_length=1)
    description: str | None = None
    interfaces: tuple[AreaInterface, ...] = ()
    planned_by: PlannedBy
    prompt_ref: str | None = None


class DescribeArtifact(PlanModel):
    """The Describe step: every active area and the summary of its stories."""

    areas: dict[str, PlannedArea] = {}
    story_summaries: dict[str, str] = {}
    outcomes: dict[str, PlannerOutcome] = {}
    truncated: tuple[str, ...] = ()


class Convention(PlanModel):
    """A convention or policy every task follows, with the spec nodes it rests on."""

    text: str = Field(min_length=1)
    spec_ids: tuple[str, ...] = ()


class Profile(PlanModel):
    """The project profile."""

    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    layout: str | None = None
    test_command: str | None = None
    test_paths: tuple[str, ...] = ()
    spec_ids: tuple[str, ...] = ()
    conventions: tuple[Convention, ...] = ()


class PlannedTask(PlanModel):
    """A target task before Diff; ``task_key`` is its id without revision, ``waits`` are task keys."""

    task_key: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    phase: Phase
    title: str = Field(min_length=1)
    description: str | None = None
    done_when: tuple[DoneWhen, ...] = ()
    complexity: Complexity
    implements: tuple[str, ...] = ()
    release: tuple[str, ...] = ()
    has_open_questions: bool = False
    planned_by: PlannedBy
    prompt_ref: str | None = None
    waits: tuple[str, ...] = ()


class ProjectArtifact(PlanModel):
    """The Project step: profile, setup and closing tasks and their story summaries."""

    profile: Profile = Profile()
    setup: tuple[PlannedTask, ...] = ()
    closing: tuple[PlannedTask, ...] = ()
    summaries: dict[str, str] = {}
    outcome: PlannerOutcome = PlannerOutcome()


class NotBuildable(PlanModel):
    """A member the Tasks planner returned as no code work."""

    spec_id: str = Field(min_length=1)
    reason: NotBuildableReason
    note: str = Field(default="", max_length=300)


class TasksArtifact(PlanModel):
    """The Tasks step: the tasks of every area and the members that are not buildable."""

    tasks: tuple[PlannedTask, ...] = ()
    not_buildable: dict[str, tuple[NotBuildable, ...]] = {}
    outcomes: dict[str, PlannerOutcome] = {}
    oversized: tuple[str, ...] = ()


class PlanStepEstimate(PlanModel):
    """Model calls and estimated input tokens of one step."""

    calls: int = 0
    input_tokens: int = 0


class PlanEstimate(PlanModel):
    """What a plan run would cost, without calling a model."""

    mode: PlanMode
    unchanged: bool = False
    steps: dict[PlanStepName, PlanStepEstimate] = {}


PlanRunRecord.model_rebuild()
CurrentPlan.model_rebuild()
SnapshotArtifact.model_rebuild()
PartitionArtifact.model_rebuild()
