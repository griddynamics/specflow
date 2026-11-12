"""The argument and result models of the agent reads of the spec graph: ``get_story_brief``, ``trace``, ``get_node``,
``search_spec``, ``similar``, ``get_evidence``, ``path`` and ``expand_brief`` (tools.md). The API maps library
objects to these (``graph.core`` and ``graph.gap`` types never reach this package); cross-field rules are checked by
the API (``invalid_arguments``), not here."""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_contracts.models import MAX_NEIGHBOURS, ProjectArguments
from graph.plan.models import BriefContract, BriefNeighbour, BriefNode, BriefQuestion, DoneWhen, WorkAreaInfo
from graph.plan.names import Complexity, Phase, StoryKind, StoryProgress, StoryState, TaskProgress, TaskStatus

MAX_TRACE_SPEC_IDS: Final = 20
MAX_EDGE_LIMIT: Final = 50
MAX_TRACE_LIMIT: Final = 200
MAX_SEARCH_LIMIT: Final = 20
MAX_QUERY_CHARS: Final = 500
MAX_SIMILAR_LIMIT: Final = 20
MAX_CONTEXT_LINES: Final = 5
MAX_PATH_HOPS: Final = 4
MIN_BUDGET_TOKENS: Final = 500
MAX_BUDGET_TOKENS: Final = 8000
MAX_ID_CHARS: Final = 200
MAX_NAMES: Final = 50

TraceDirection = Literal["based_on", "affects"]
TraceBasis = Literal["trusted", "any"]
NodePart = Literal["edges", "plan", "provenance", "findings"]
NODE_PARTS: Final[tuple[NodePart, ...]] = ("edges", "plan", "provenance", "findings")
PlanState = Literal["completed", "in_progress", "open", "unplanned"]
EdgeDirection = Literal["out", "in"]
SearchMatch = Literal["id", "text", "vector", "both"]
ExpansionVia = Literal["graph", "similar", "both"]
PathStepKind = Literal["forward", "backward", "scope"]


class Holder(BaseModel):
    """The person behind a claim, a task or a finding: no user id, token or email (tools.md "Holders")."""

    name: str | None
    you: bool
    agent_label: str | None = None


class FindingItem(BaseModel):
    """An open finding about a node or a story."""

    finding_id: str
    kind: str
    text: str
    blocking: bool
    about: list[str]
    author: Holder


class TaskRef(BaseModel):
    """A task of a node: its holder while it is in progress, its commit once it is completed."""

    task_id: str
    story_id: str
    status: str
    holder: Holder | None = None
    commit: str | None = None


# --- get_story_brief ------------------------------------------------------------------------------------------


class GetStoryBriefArgs(ProjectArguments):
    story_id: str = Field(
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="The story_id of a story from list_stories, or the one claim_story answered.",
    )
    neighbours: int = Field(
        default=30,
        ge=0,
        le=MAX_NEIGHBOURS,
        description="How many neighbouring specification nodes to include.",
    )


class BriefStory(BaseModel):
    story_id: str
    kind: StoryKind
    title: str
    summary: str | None
    anchor: str | None
    release: str | None
    state: StoryState


class BriefTask(BaseModel):
    """A task of the story with its ``done_when`` items (source ``spec`` binding, ``planner`` the planner's reading)."""

    task_id: str
    title: str
    phase: Phase
    status: TaskStatus
    complexity: Complexity
    progress: TaskProgress | None
    waits_for: list[str]
    done_when: list[DoneWhen]


class StoryBriefBody(BaseModel):
    story: BriefStory
    area: WorkAreaInfo | None
    profile: dict[str, Any]
    tasks: list[BriefTask]
    implements: list[BriefNode]
    contracts: list[BriefContract]
    open_questions: list[BriefQuestion]
    findings: list[FindingItem]
    neighbours: list[BriefNeighbour]
    spec_changed: bool
    missing: list[str]


class StoryBriefResult(BaseModel):
    project_id: str
    brief: StoryBriefBody


# --- trace ----------------------------------------------------------------------------------------------------


class TraceQuery(BaseModel):
    """The arguments of ``trace`` besides the project: the query parameters of its route."""

    story_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="A story_id: trace from the specification nodes its tasks implement.",
    )
    spec_ids: Annotated[list[str], Field(min_length=1, max_length=MAX_TRACE_SPEC_IDS)] | None = Field(
        default=None, description="1 to 20 spec_ids to trace from. Give this or story_id, not both."
    )
    direction: TraceDirection = Field(
        description="based_on: what the start nodes depend on. affects: what depends on them."
    )
    depth: int = Field(default=2, ge=1, le=3, description="How many edges to follow, 1 to 3.")
    basis: TraceBasis = Field(
        default="trusted", description="trusted: stated and implied edges. any: also inferred edges."
    )
    include_owner: bool = Field(default=True, description="Also start from the module that owns the start nodes.")
    relations: list[str] | None = Field(
        default=None,
        max_length=MAX_NAMES,
        description="Only these relations of the project's ontology; leave out for all.",
    )
    limit: int = Field(
        default=100, ge=1, le=MAX_TRACE_LIMIT, description="The most nodes to return, 1 to 200 (no paging)."
    )


class TraceArgs(TraceQuery, ProjectArguments):
    pass


class TraceOwnerItem(BaseModel):
    """An owner of a start node, up to the first spine root: the trace's context."""

    spec_id: str
    label: str
    name: str
    plan_state: PlanState | None
    tasks: list[TaskRef]
    findings: int


class TraceNodeItem(BaseModel):
    """A node the trace reached, with the edge from its ``parent`` that reached it first and its plan state."""

    spec_id: str
    label: str
    name: str
    hops: int
    parent: str
    relation: str
    step: Literal["forward", "backward", "scope"]
    role: str
    basis: str
    confidence: float | None
    rule: str | None
    weak: bool
    via_owner: bool
    spine_root: bool
    plan_state: PlanState | None
    tasks: list[TaskRef]
    findings: int


class TraceStoryItem(BaseModel):
    """A story that holds tasks of the traced nodes."""

    story_id: str
    title: str | None
    area_id: str
    progress: StoryProgress
    holders: list[Holder]


class SpecTrace(BaseModel):
    direction: TraceDirection
    sources: list[str]
    context: list[TraceOwnerItem]
    nodes: list[TraceNodeItem]
    stories: list[TraceStoryItem]
    summary: dict[str, int] | None
    open_questions: list[BriefQuestion]
    unknown: list[str]
    truncated: bool
    seeds_truncated: bool


# --- get_node -------------------------------------------------------------------------------------------------


class NodeQuery(BaseModel):
    """The arguments of ``get_node`` besides the project: the query parameters of its route."""

    spec_id: str = Field(
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="The spec_id of a node from a brief, a trace or a search; aliases resolve.",
    )
    include: list[NodePart] = Field(
        default=list(NODE_PARTS), min_length=1, description="The parts to return; a part left out is null."
    )
    edge_limit: int = Field(
        default=25, ge=1, le=MAX_EDGE_LIMIT, description="The most edges to list; the rest are counted per relation."
    )


class GetNodeArgs(NodeQuery, ProjectArguments):
    pass


class NodeStatement(BaseModel):
    fragment_id: str
    document_id: str | None
    text: str


class NodeBody(BaseModel):
    spec_id: str
    label: str
    name: str
    spec_id_aliases: list[str]
    attributes: dict[str, Any]
    statements: list[NodeStatement]


class EdgeItem(BaseModel):
    """One domain edge of the node: ``direction`` ``out`` leaves it, ``in`` arrives; ``spec_id``, ``label`` and
    ``name`` are the other end."""

    relation: str
    direction: EdgeDirection
    spec_id: str
    label: str | None
    name: str | None
    basis: str | None
    confidence: float | None
    rule: str | None


class RelationCount(BaseModel):
    relation: str
    direction: EdgeDirection
    count: int


class NodeEdges(BaseModel):
    """At most ``edge_limit`` edges (stated first); ``by_relation`` counts all of them, so a hub is summarised."""

    items: list[EdgeItem]
    by_relation: list[RelationCount]
    total: int


class NodePlanPart(BaseModel):
    plan_state: PlanState
    tasks: list[TaskRef]


class NodeProvenance(BaseModel):
    document_id: str | None
    path: str | None
    fragment_id: str
    line_start: int | None
    line_end: int | None
    quote: str | None


class NodeDetail(BaseModel):
    node: NodeBody
    edges: NodeEdges | None
    plan: NodePlanPart | None
    provenance: list[NodeProvenance] | None
    findings: list[FindingItem] | None


# --- search_spec ----------------------------------------------------------------------------------------------


class SearchQuery(BaseModel):
    """The arguments of ``search_spec`` besides the project: the query parameters of its route."""

    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARS, description="The term or phrase to search for.")
    near: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="A story_id or spec_id: hits within 3 hops of it (of the story's nodes) rank first.",
    )
    labels: list[str] | None = Field(
        default=None, max_length=MAX_NAMES, description="Only nodes of these labels; leave out for all."
    )
    limit: int = Field(default=10, ge=1, le=MAX_SEARCH_LIMIT, description="The most hits to return, 1 to 20.")


class SearchSpecArgs(SearchQuery, ProjectArguments):
    pass


class SpecHit(BaseModel):
    spec_id: str
    label: str
    name: str
    score: float
    matched: SearchMatch
    hops_from_near: int | None


class SpecSearch(BaseModel):
    project_id: str
    items: list[SpecHit]


# --- similar --------------------------------------------------------------------------------------------------


class SimilarQuery(BaseModel):
    """The arguments of ``similar`` besides the project: the query parameters of its route."""

    story_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="A story_id: compare with the specification nodes its tasks implement.",
    )
    spec_ids: Annotated[list[str], Field(min_length=1, max_length=MAX_TRACE_SPEC_IDS)] | None = Field(
        default=None, description="1 to 20 spec_ids to compare with. Give this or story_id, not both."
    )
    exclude_linked: bool = Field(
        default=True, description="Leave out nodes that already have an edge to one of the compared nodes."
    )
    limit: int = Field(default=5, ge=1, le=MAX_SIMILAR_LIMIT, description="The most nodes to return, 1 to 20.")
    min_score: float = Field(
        default=0.75, ge=0, le=1, description="The lowest similarity score, 0 to 1 ((1 + cosine) / 2)."
    )


class SimilarArgs(SimilarQuery, ProjectArguments):
    pass


class SimilarHit(BaseModel):
    """A node that reads like the compared node ``like``, with its score and plan state."""

    spec_id: str
    label: str
    name: str
    score: float
    like: str
    plan_state: PlanState | None


class SimilarNodes(BaseModel):
    project_id: str
    sources: list[str]
    unknown: list[str]
    items: list[SimilarHit]


# --- get_evidence ---------------------------------------------------------------------------------------------


class EvidenceQuery(BaseModel):
    """The arguments of ``get_evidence`` besides the project: the query parameters of its route."""

    spec_id: str = Field(
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="The spec_id of a node from a brief, a trace or a search; aliases resolve.",
    )
    context_lines: int = Field(
        default=2, ge=0, le=MAX_CONTEXT_LINES, description="Lines of context before and after each quote, 0 to 5."
    )


class GetEvidenceArgs(EvidenceQuery, ProjectArguments):
    pass


class EvidenceOther(BaseModel):
    """Another node built from the same lines."""

    spec_id: str
    label: str
    name: str


class EvidenceSource(BaseModel):
    """One evidence fragment: its document and lines, the quote with its context lines, the other nodes there."""

    document_id: str | None
    path: str | None
    fragment_id: str
    line_start: int | None
    line_end: int | None
    quote: str
    before: list[str]
    after: list[str]
    others: list[EvidenceOther]


class Evidence(BaseModel):
    project_id: str
    spec_id: str
    items: list[EvidenceSource]


# --- path -----------------------------------------------------------------------------------------------------


class PathQuery(BaseModel):
    """The arguments of ``path`` besides the project: the query parameters of its route (``from`` and ``to`` on
    the wire; ``from`` is a Python keyword, so the fields are named ``from_id`` and ``to_id``)."""

    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True, validate_by_alias=True)

    from_id: str = Field(
        alias="from",
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="The spec_id the chains start from; aliases resolve.",
    )
    to_id: str = Field(
        alias="to", min_length=1, max_length=MAX_ID_CHARS, description="The spec_id the chains end at; aliases resolve."
    )
    max_hops: int = Field(
        default=MAX_PATH_HOPS, ge=1, le=MAX_PATH_HOPS, description="The most edges in a chain, 1 to 4."
    )


class PathArgs(PathQuery, ProjectArguments):
    pass


class PathStepItem(BaseModel):
    """One step of a chain: the node it reaches and the edge it takes."""

    spec_id: str
    label: str
    name: str
    relation: str
    step: PathStepKind
    role: str
    basis: str


class PathChainItem(BaseModel):
    hops: int
    steps: list[PathStepItem]


class NodePaths(BaseModel):
    """At most 3 shortest chains; ``direction`` ``based_on`` (``from`` depends on ``to``), ``affects`` (the reverse)
    or null (no chain)."""

    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True, validate_by_alias=True)

    project_id: str
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    direction: TraceDirection | None
    items: list[PathChainItem]


# --- expand_brief ---------------------------------------------------------------------------------------------


class ExpandQuery(BaseModel):
    """The arguments of ``expand_brief`` besides the project: the query parameters of its route."""

    story_id: str = Field(
        min_length=1,
        max_length=MAX_ID_CHARS,
        description="The story_id of a story from list_stories, or the one claim_story answered.",
    )
    budget_tokens: int = Field(
        default=3000,
        ge=MIN_BUDGET_TOKENS,
        le=MAX_BUDGET_TOKENS,
        description="The most estimated tokens of text to return, 500 to 8000.",
    )


class ExpandBriefArgs(ExpandQuery, ProjectArguments):
    pass


class ExpansionHit(BaseModel):
    """A node to read next: its score, how it was found and its statement or quote with its estimated tokens."""

    spec_id: str
    label: str
    name: str
    score: float
    via: ExpansionVia
    text: str
    tokens: int


class BriefExpansion(BaseModel):
    project_id: str
    story_id: str
    items: list[ExpansionHit]
    tokens: int
    left_out: int
