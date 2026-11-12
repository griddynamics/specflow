"""Plan-side change reports, run records and revision models.

The host converts the build's change report into ``BuildChangeReport`` before it is given to
``PlanContext``: ``graph.plan`` never imports ``graph.build``. Every model is frozen, rejects unknown
fields and round-trips through JSON. Mapping fields are normalised to sorted mappings, so their dump
is deterministic.
"""

from __future__ import annotations

import json
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
    PlanLabel,
    PlanMode,
    PlannedBy,
    PlanRelation,
    StoryKind,
    StoryProgress,
    StoryStatus,
    TaskProgress,
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


def _json_text(value: object) -> object:
    """A JSON property as Neo4j stores it (text) decoded; other values unchanged."""

    return json.loads(value) if isinstance(value, str) else value


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
    consumed_build_ids: tuple[str, ...] = ()
    graph_fingerprint: str | None = None
    # None only while the plan's first persist runs: its first batch creates the node, its last writes these.
    ontology_version: str | None = None
    plan_ontology_version: str | None = None
    rules_version: str | None = None
    applying: bool = False
    last_run_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @field_validator("applying", mode="before")
    @classmethod
    def _applying(cls, value: object) -> object:
        """Neo4j stores the applying run id, or nothing."""

        return bool(value) if value is None or isinstance(value, str) else value

    @field_validator("consumed_build_ids", mode="before")
    @classmethod
    def _builds(cls, value: object) -> object:
        return () if value is None else value


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
    prompt_ref: str | None = None
    status: AreaStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_run_id: str | None = None

    @field_validator("interfaces", "not_buildable", mode="before")
    @classmethod
    def _json(cls, value: object) -> object:
        return () if value is None else _json_text(value)


class StoryCore(PlanModel):
    """The fields of a persisted story that reads show (``PlanStory`` adds the derived progress)."""

    story_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    kind: StoryKind
    anchor: str | None = None
    chapter: ChapterRef | None = None
    title: str = Field(min_length=1)
    summary: str | None = None
    prompt_ref: str | None = None
    release: str | None = None
    status: StoryStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_run_id: str | None = None

    @field_validator("chapter", mode="before")
    @classmethod
    def _json(cls, value: object) -> object:
        return _json_text(value)


class StoryInfo(StoryCore):
    """A persisted story as the plan library reads it: the story and its claim (data-model.md "Story")."""

    claimed_by: str | None = None
    claimed_at: AwareDatetime | None = None
    agent_label: str | None = None
    branch: str | None = None
    note: str | None = None
    pr_url: str | None = None
    # The token of the last story claim: read for the claim calls and the plan checks, never dumped or shown.
    claim_token: str | None = Field(default=None, exclude=True, repr=False)

    def core(self) -> dict[str, Any]:
        """The fields reads show, without the claim."""

        return self.model_dump(include=set(StoryCore.model_fields))


class TaskCore(PlanModel):
    """The fields of a persisted task that reads show (``PlanTask`` adds its waits and revision neighbours)."""

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

    @field_validator("done_when", mode="before")
    @classmethod
    def _json(cls, value: object) -> object:
        return () if value is None else _json_text(value)

    @field_validator("implements", "release", mode="before")
    @classmethod
    def _list(cls, value: object) -> object:
        return () if value is None else value


class CurrentTask(TaskCore):
    """A persisted task as Snapshot, Diff and the lifecycle see it: the task and its claim and completion record."""

    blocked_by: str | None = None
    progress: TaskProgress | None = None
    completed_by: str | None = None
    completed_at: AwareDatetime | None = None
    # The claim token: read for the lifecycle calls and the plan checks, never dumped or shown.
    claim_token: str | None = Field(default=None, exclude=True, repr=False)

    def core(self) -> dict[str, Any]:
        """The fields reads show, without the claim and completion record."""

        return self.model_dump(include=set(TaskCore.model_fields))


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


class Wait(PlanModel):
    """A wait between target tasks, by task key: ``source`` waits for ``target``; ``rule`` 0 is a planner wait."""

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: Literal["planner", "stitch"]
    rule: int = Field(default=0, ge=0, le=8)


class StitchArtifact(PlanModel):
    """Every wait of the target plan, and the stitch waits dropped to break cycles."""

    waits: tuple[Wait, ...] = ()
    dropped: tuple[Wait, ...] = ()


ReviewSeverity = Literal["blocking", "advisory"]


class ReviewIssue(PlanModel):
    """One advisory finding of the review; it changes nothing."""

    area_id: str = Field(min_length=1)
    task_id: str | None = None
    problem: str = Field(min_length=1)
    severity: ReviewSeverity


class ReviewArtifact(PlanModel):
    issues: tuple[ReviewIssue, ...] = ()
    failed: bool = False
    skipped: bool = False
    outcome: PlannerOutcome = PlannerOutcome()


NodeOpKind = Literal["create", "update", "reset", "supersede", "retire", "revive", "keep"]


class NodeOp(PlanModel):
    """One plan node write; a task write applies only while its status is ``expected_status``."""

    op: NodeOpKind
    label: PlanLabel
    id: str = Field(min_length=1)
    expected_status: TaskStatus | None = None
    properties: dict[str, Any] = {}


class LinkOp(PlanModel):
    """One plan relationship write. A spec end is a ``spec_id`` in deletes and a node ``uuid`` in creates."""

    type: PlanRelation
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    key: str = Field(min_length=1)
    properties: dict[str, Any] = {}


class SupersededTask(PlanModel):
    task_id: str = Field(min_length=1)
    successor: str = Field(min_length=1)
    cause: str = Field(min_length=1)


class PlanChangesSummary(PlanModel):
    """What a run changed, by id."""

    tasks_added: tuple[str, ...] = ()
    tasks_updated: tuple[str, ...] = ()
    tasks_reopened: tuple[str, ...] = ()
    tasks_superseded: tuple[SupersededTask, ...] = ()
    tasks_retired: tuple[str, ...] = ()
    tasks_kept: tuple[str, ...] = ()
    areas_added: tuple[str, ...] = ()
    areas_retired: tuple[str, ...] = ()
    areas_reactivated: tuple[str, ...] = ()
    stories_added: tuple[str, ...] = ()
    stories_retired: tuple[str, ...] = ()
    stories_reactivated: tuple[str, ...] = ()


class DiffArtifact(PlanModel):
    """The writes from the current plan to the target plan, and what they mean."""

    areas: tuple[NodeOp, ...] = ()
    stories: tuple[NodeOp, ...] = ()
    tasks: tuple[NodeOp, ...] = ()
    links_delete: tuple[LinkOp, ...] = ()
    links_create: tuple[LinkOp, ...] = ()
    plan_properties: dict[str, Any] = {}
    summary: PlanChangesSummary = PlanChangesSummary()
    stale_commits: dict[str, str] = {}
    unverified_completed: tuple[str, ...] = ()
    in_progress_changed: tuple[str, ...] = ()
    affected_dependants: tuple[str, ...] = ()
    revisions: MeaningRevisions = MeaningRevisions()


class PersistArtifact(PlanModel):
    """Persist progress: batches applied so far (the cursor) and the tasks skipped as conflicts."""

    cursor: int = Field(default=0, ge=0)
    batches: int = Field(default=0, ge=0)
    conflicts: tuple[str, ...] = ()


class PlanCheckMetrics(PlanModel):
    """Counts of the plan checks; ``stale_claims`` only when a staleness limit was given."""

    outdated: bool = False
    ready: int = 0
    blocked: int = 0
    failed: int = 0
    stale_claims: int | None = None
    stale_commits: int = 0
    not_buildable: dict[str, int] = {}
    open_questions_on_tasks: int = 0
    stories: dict[str, int] = {}


class PlanCheckReport(PlanModel):
    """Read-only checks of the persisted plan: ``ok`` when no check has a problem; ids only."""

    ok: bool
    problems: dict[str, tuple[str, ...]] = {}
    metrics: PlanCheckMetrics = PlanCheckMetrics()


class PlannerCounts(PlanModel):
    """One planner's answers in a run."""

    calls: int = 0
    reused: int = 0
    fallbacks: int = 0
    invalid: int = 0


class PlanningSummary(PlanModel):
    """How the run planned: planner counts, review issues, dropped dependencies."""

    describe: PlannerCounts = PlannerCounts()
    project: PlannerCounts = PlannerCounts()
    tasks: PlannerCounts = PlannerCounts()
    review_issues: dict[str, int] = {}
    review_failed: bool = False
    dropped_dependencies: int = 0


class StepUsage(PlanModel):
    """Model calls, tokens and duration of one step (approximate when the gateway is shared)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0


class PlanMetrics(PlanModel):
    """Counts of the target plan and the run."""

    areas: int = 0
    stories_by_kind: dict[str, int] = {}
    tasks_by_status: dict[str, int] = {}
    tasks_by_phase: dict[str, int] = {}
    not_buildable: dict[str, int] = {}
    open_questions_on_tasks: int = 0
    oversized_task: int = 0
    ambiguous_membership: int = 0
    cross_cutting: int = 0
    unplaced: int = 0
    describe_truncated: int = 0
    steps: dict[str, StepUsage] = {}


class PlanResult(PlanModel):
    """The result of a plan run; tasks, stories, areas and spec nodes are named by id, never by text."""

    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: PlanRunStatus
    mode: PlanMode
    unchanged: bool = False
    build_id: str = Field(min_length=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime
    failure: str | None = None
    changes: PlanChangesSummary = PlanChangesSummary()
    stale_commits: dict[str, str] = {}
    unverified_completed: tuple[str, ...] = ()
    in_progress_changed: tuple[str, ...] = ()
    affected_dependants: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    planning: PlanningSummary = PlanningSummary()
    checks: PlanCheckReport | None = None
    metrics: PlanMetrics = PlanMetrics()


class PlanTask(TaskCore):
    """A task as reads return it (never its claim token), with its waits and revision neighbours."""

    waits_for: tuple[str, ...] = ()
    waited_by: tuple[str, ...] = ()
    predecessor: str | None = None
    successor: str | None = None


class AreaRef(PlanModel):
    """A work area as a task list names it."""

    area_id: str
    title: str | None


class StoryRef(PlanModel):
    """A story as a task list names it; the title is None for a story that is not active."""

    story_id: str
    title: str | None


class TaskItem(PlanModel):
    """One task of a task list: what an agent needs to pick it, with its area, story and readiness."""

    task_id: str
    title: str
    phase: str
    status: str
    complexity: str
    area: AreaRef
    story: StoryRef
    ready: bool
    waits_for: tuple[str, ...]
    planned_by: str


class FeatureRef(PlanModel):
    """A feature node by its id and name."""

    spec_id: str
    name: str


class FeatureTasks(PlanModel):
    """Every open or completed task that works on one feature, ordered by ``task_id``."""

    feature: FeatureRef
    items: tuple[TaskItem, ...]


class TaskPath(PlanModel):
    """The prerequisites of one task in build order, then the task; ``completed`` counts finished prerequisites."""

    task_id: str
    items: tuple[TaskItem, ...]
    completed: int


class Claim(PlanModel):
    """A claimed task and the token every later transition of the claim must carry."""

    task: PlanTask
    claim_token: str = Field(min_length=1)


class PlanArea(WorkAreaInfo):
    """A work area with its members, task counts by status and story counts by progress."""

    members: tuple[str, ...] = ()
    task_counts: dict[str, int] = {}
    story_counts: dict[str, int] = {}


class PlanStory(StoryCore):
    """A story with its derived progress and task counts by status."""

    progress: StoryProgress
    task_counts: dict[str, int] = {}


class StoryMember(PlanModel):
    """A plannable node whose home story this is, with the open or completed tasks that implement it."""

    spec_id: str = Field(min_length=1)
    label: str | None = None
    name: str | None = None
    task_ids: tuple[str, ...] = ()
    not_buildable_reason: NotBuildableReason | None = None
    covered: bool = False


class StoryDetail(PlanModel):
    story: PlanStory
    anchor: dict[str, Any] | None = None
    members: tuple[StoryMember, ...] = ()
    tasks: tuple[PlanTask, ...] = ()
    waits_on: tuple[str, ...] = ()
    waited_by: tuple[str, ...] = ()


class WorkEntry(PlanModel):
    """One task of a story's work: the story's open tasks and their unfinished prerequisites."""

    task_id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    status: TaskStatus
    ready: bool = False
    waits_for: tuple[str, ...] = ()
    claimed_by: str | None = None
    claimed_at: AwareDatetime | None = None


class StoryWork(PlanModel):
    story_id: str = Field(min_length=1)
    entries: tuple[WorkEntry, ...] = ()


class DagTask(PlanModel):
    task_id: str = Field(min_length=1)
    story_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    status: TaskStatus
    phase: Phase


class DagStory(PlanModel):
    story_id: str = Field(min_length=1)
    area_id: str = Field(min_length=1)
    progress: StoryProgress
    task_ids: tuple[str, ...] = ()


class PlanDag(PlanModel):
    """Open and completed tasks in topological order with their waits, and the story projection."""

    tasks: tuple[DagTask, ...] = ()
    edges: tuple[tuple[str, str], ...] = ()
    stories: tuple[DagStory, ...] = ()
    story_edges: tuple[tuple[str, str], ...] = ()
    story_groups: tuple[tuple[str, ...], ...] = ()


class BriefNode(PlanModel):
    """An implemented spec node with its evidence."""

    spec_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    name: str
    attributes: dict[str, Any] = {}
    statements: tuple[dict[str, Any], ...] = ()
    provenance: tuple[dict[str, Any], ...] = ()
    assumption: bool = False


class BriefContract(PlanModel):
    """A completed direct prerequisite: what the task builds on."""

    task_id: str = Field(min_length=1)
    title: str
    commit: str | None = None
    implements: tuple[str, ...] = ()
    interfaces: tuple[dict[str, Any], ...] = ()


class BriefQuestion(PlanModel):
    spec_id: str = Field(min_length=1)
    statement: str = ""
    provenance: tuple[dict[str, Any], ...] = ()
    about: str = Field(min_length=1)


class BriefNeighbour(PlanModel):
    spec_id: str = Field(min_length=1)
    label: str
    name: str
    relation: str
    direction: Literal["out", "in"]
    of: str
    basis: str | None = None
    confidence: float | None = None


class BriefPlacement(PlanModel):
    """How an implemented node was placed in the area, with the edges the placement followed."""

    spec_id: str = Field(min_length=1)
    rule: int | None = None
    via: tuple[dict[str, Any], ...] = ()


class TaskBrief(PlanModel):
    """What one coding agent needs for one task, assembled when it is asked for."""

    task: PlanTask
    done_when: tuple[DoneWhen, ...] = ()
    story: PlanStory | None = None
    area: WorkAreaInfo | None = None
    profile: dict[str, Any] = {}
    implements: tuple[BriefNode, ...] = ()
    contracts: tuple[BriefContract, ...] = ()
    open_questions: tuple[BriefQuestion, ...] = ()
    neighbours: tuple[BriefNeighbour, ...] = ()
    placement: tuple[BriefPlacement, ...] = ()
    spec_changed: bool = False
    missing: tuple[str, ...] = ()


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
