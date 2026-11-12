"""The agent reads of the spec graph: ``get_story_brief``, ``trace``, ``get_node``, ``search_spec``, ``similar``,
``get_evidence``, ``path`` and ``expand_brief`` (tools.md, architecture.md "Story and spec routes").

Same ``AgentRoute``, auth, membership-first dependency chain and infrastructure errors as ``api.routers.agent``. Each
route calls the libraries and maps their answers to ``agent_contracts`` models (``api.agent_views``); argument rules
that span arguments or need the ontology are checked here before any library call (``invalid_arguments``,
``unknown_relation``). Nothing here writes SQL or Cypher.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final

from fastapi import APIRouter, Query

from agent_contracts import (
    BriefExpansion,
    Evidence,
    EvidenceQuery,
    ExpandQuery,
    GetStoryBriefArgs,
    NodeDetail,
    NodePaths,
    NodeQuery,
    PathQuery,
    SearchMatch,
    SearchQuery,
    SimilarNodes,
    SimilarQuery,
    SpecHit,
    SpecSearch,
    SpecTrace,
    StoryBriefResult,
    TraceQuery,
)
from api.agent_reads import agent_reads
from api.agent_views import (
    evidence_view,
    expansion_view,
    finding_items,
    holders_of,
    node_body,
    node_edges,
    node_plan_part,
    node_provenance,
    path_view,
    shown_edges,
    similar_view,
    story_brief_body,
    trace_user_ids,
    trace_view,
)
from api.deps import AgentProjectDep, AgentUserDep, RuntimeDep
from api.errors import (
    ApiError,
    invalid_arguments,
    no_plan,
    node_not_found,
    story_not_found,
    unknown_relation,
)
from api.routers.agent import AgentRoute
from api.runtime import Runtime
from graph.core.expand import MAX_SEED_IDS, MAX_SIMILAR_SOURCES
from graph.core.reader import BUILD, GraphNode, NodeHit
from graph.gap import Finding
from graph.plan.errors import PlanReadError
from graph.plan.findings_port import open_finding

router = APIRouter(prefix="/agent", tags=["agent"], route_class=AgentRoute)

_PLAN_ERRORS: Final[dict[str, Callable[[], ApiError]]] = {"no_plan": no_plan, "story_not_found": story_not_found}
_NEIGHBOURS = GetStoryBriefArgs.model_fields["neighbours"]
NeighboursQuery = Annotated[int, *_NEIGHBOURS.metadata]
DEFAULT_NEIGHBOURS: Final[int] = _NEIGHBOURS.get_default()
StoryIdQuery = Annotated[str, *GetStoryBriefArgs.model_fields["story_id"].metadata]
NODE_FINDINGS: Final = 50  # the most open findings one node shows
NEAR_HOPS: Final = 3
NEAR_FETCH_FACTOR: Final = 3
NEAR_FETCH_MAX: Final = 60
_MATCH: Final[dict[str, SearchMatch]] = {"fulltext": "text", "id": "id", "vector": "vector", "both": "both"}


async def _plan_read[T](call: Callable[[], Awaitable[T]]) -> T:
    """``call``'s answer; a plan read that finds nothing becomes the error of the same code."""

    try:
        return await call()
    except PlanReadError as error:
        mapped = _PLAN_ERRORS.get(error.code)
        if mapped is None:
            raise
        raise mapped() from None


@router.get("/story-brief", response_model=StoryBriefResult)
async def get_story_brief(
    project: AgentProjectDep,
    runtime: RuntimeDep,
    user: AgentUserDep,
    story_id: StoryIdQuery,
    neighbours: NeighboursQuery = DEFAULT_NEIGHBOURS,
) -> StoryBriefResult:
    """The plan library's brief of one story, with its open findings; authors are named from the project's members."""

    graph_id = str(project.id)
    plan = (await agent_reads(runtime)).plan
    brief = await _plan_read(lambda: plan.story_brief(graph_id, story_id, neighbours=neighbours))
    holders = await holders_of(runtime, project.id, user.id, [item.created_by for item in brief.findings])
    return StoryBriefResult(project_id=graph_id, brief=story_brief_body(brief, holders))


@router.get("/trace", response_model=SpecTrace)
async def trace(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, args: Annotated[TraceQuery, Query()]
) -> SpecTrace:
    """The directed walk from a story or from spec ids with the plan overlay; the arguments are checked first."""

    if (args.story_id is None) == (args.spec_ids is None):
        raise invalid_arguments()
    reads = await agent_reads(runtime)
    if args.relations is not None:
        domain = {name for name in reads.ontology.relations if reads.ontology.owner_of(name) == BUILD}
        if not set(args.relations) <= domain:
            raise unknown_relation()
    graph_id = str(project.id)
    found = await _plan_read(
        lambda: reads.plan.trace(
            graph_id,
            story_id=args.story_id,
            spec_ids=args.spec_ids,
            direction=args.direction,
            depth=args.depth,
            basis=args.basis,
            relations=args.relations,
            include_owner=args.include_owner,
            limit=args.limit,
        )
    )
    holders = await holders_of(runtime, project.id, user.id, trace_user_ids(found))
    return trace_view(found, holders)


@router.get("/node", response_model=NodeDetail)
async def get_node(
    project: AgentProjectDep, runtime: RuntimeDep, user: AgentUserDep, args: Annotated[NodeQuery, Query()]
) -> NodeDetail:
    """One node with the parts asked for: edges (a hub's summarised past ``edge_limit``), plan, provenance, findings."""

    graph_id = str(project.id)
    reads = await agent_reads(runtime)
    node = await reads.reader.node(graph_id, args.spec_id)
    if node is None:
        raise node_not_found()
    parts = set(args.include)
    edges = None
    edge_list = []
    ends: dict[str, GraphNode] = {}
    if "edges" in parts:
        edge_list = await reads.reader.neighbours(graph_id, node.spec_id)
        others = {
            edge.target if edge.source == node.spec_id else edge.source
            for edge in shown_edges(edge_list, node.spec_id, args.edge_limit)
        }
        ends = await reads.reader.nodes_by_spec_ids(graph_id, sorted(others))
    plan = await reads.plan.node_plan(graph_id, node.spec_id, node.spec_id_aliases) if "plan" in parts else None
    open_findings: list[Finding] = []
    if "findings" in parts and reads.findings is not None:
        page = await reads.findings.findings(graph_id, spec_ids=[node.spec_id], limit=NODE_FINDINGS)
        open_findings = list(page.findings)
    named = [task.claimed_by for task in plan.tasks] if plan is not None else []
    holders = await holders_of(runtime, project.id, user.id, [*named, *(item.created_by for item in open_findings)])
    if "edges" in parts:
        edges = node_edges(edge_list, node.spec_id, args.edge_limit, ends)
    return NodeDetail(
        node=node_body(node),
        edges=edges,
        plan=node_plan_part(plan, holders),
        provenance=node_provenance(node) if "provenance" in parts else None,
        findings=finding_items([open_finding(item) for item in open_findings], holders)
        if "findings" in parts
        else None,
    )


@router.get("/spec-search", response_model=SpecSearch)
async def search_spec(
    project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[SearchQuery, Query()]
) -> SpecSearch:
    """Hybrid search by id, text and meaning; with ``near``, 3 x ``limit`` hits (at most 60) are re-ranked by hops."""

    reads = await agent_reads(runtime)
    if args.labels is not None:
        spec_labels = {label for label in reads.ontology.labels if reads.ontology.owner_of(label) == BUILD}
        if not set(args.labels) <= spec_labels:
            raise invalid_arguments()
    graph_id = str(project.id)
    sources = await _near_nodes(runtime, graph_id, args.near) if args.near is not None else []
    fetch = args.limit if args.near is None else min(NEAR_FETCH_FACTOR * args.limit, NEAR_FETCH_MAX)
    hits = await reads.reader.find_nodes(graph_id, args.query, labels=args.labels, limit=fetch)
    distance: dict[str, int] = {}
    if sources and hits:
        distance = await reads.reader.hops(graph_id, sources, [hit.node.spec_id for hit in hits], max_hops=NEAR_HOPS)
        hits = _near_first(hits, distance)
    return SpecSearch(
        project_id=graph_id,
        items=[
            SpecHit(
                spec_id=hit.node.spec_id,
                label=hit.node.label,
                name=hit.node.name,
                score=hit.score,
                matched=_MATCH[hit.matched],
                hops_from_near=distance.get(hit.node.spec_id),
            )
            for hit in hits[: args.limit]
        ],
    )


@router.get("/similar", response_model=SimilarNodes)
async def similar(
    project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[SimilarQuery, Query()]
) -> SimilarNodes:
    """Nodes whose stored vectors read like the story's implemented nodes or the given ones, with plan state."""

    if (args.story_id is None) == (args.spec_ids is None):
        raise invalid_arguments()
    graph_id = str(project.id)
    reads = await agent_reads(runtime)
    sources = args.spec_ids or []
    story_id = args.story_id
    if story_id is not None:
        brief = await _plan_read(lambda: reads.plan.story_brief(graph_id, story_id, neighbours=0))
        sources = [node.spec_id for node in brief.implements][:MAX_SIMILAR_SOURCES]
    if not sources:
        return SimilarNodes(project_id=graph_id, sources=[], unknown=[], items=[])
    found = await reads.reader.similar(
        graph_id, sources, exclude_linked=args.exclude_linked, min_score=args.min_score, limit=args.limit
    )
    states = (
        await reads.plan.plan_states(graph_id, {item.spec_id: item.aliases for item in found.items})
        if found.items
        else None
    )
    return similar_view(graph_id, found, states)


@router.get("/evidence", response_model=Evidence)
async def get_evidence(
    project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[EvidenceQuery, Query()]
) -> Evidence:
    """The source lines behind one node, with context lines and the other nodes built from them."""

    graph_id = str(project.id)
    items = await (await agent_reads(runtime)).reader.evidence(graph_id, args.spec_id, context_lines=args.context_lines)
    if items is None:
        raise node_not_found()
    return evidence_view(graph_id, args.spec_id, items)


@router.get("/node-path", response_model=NodePaths)
async def path(project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[PathQuery, Query()]) -> NodePaths:
    """At most three shortest directed chains between two nodes, modules never inside one."""

    graph_id = str(project.id)
    reader = (await agent_reads(runtime)).reader
    found = await reader.paths(graph_id, args.from_id, args.to_id, max_hops=args.max_hops)
    if found is None:
        raise node_not_found()
    return path_view(graph_id, args.from_id, args.to_id, found)


@router.get("/brief-expansion", response_model=BriefExpansion)
async def expand_brief(
    project: AgentProjectDep, runtime: RuntimeDep, args: Annotated[ExpandQuery, Query()]
) -> BriefExpansion:
    """What to read next beyond the story's brief: seeds its implemented nodes, its neighbours left out."""

    graph_id = str(project.id)
    reads = await agent_reads(runtime)
    brief = await _plan_read(lambda: reads.plan.story_brief(graph_id, args.story_id))
    seeds = [node.spec_id for node in brief.implements][:MAX_SEED_IDS]
    if not seeds:
        return BriefExpansion(project_id=graph_id, story_id=args.story_id, items=[], tokens=0, left_out=0)
    found = await reads.reader.expand(
        graph_id,
        seeds,
        exclude=[item.spec_id for item in brief.neighbours],
        budget_tokens=args.budget_tokens,
    )
    return expansion_view(graph_id, args.story_id, found)


async def _near_nodes(runtime: Runtime, graph_id: str, near: str) -> list[str]:
    """Where a search near ``near`` starts: the node itself, or the nodes of the story; neither is
    ``invalid_arguments``."""

    reads = await agent_reads(runtime)
    node = await reads.reader.node(graph_id, near)
    if node is not None:
        return [node.spec_id]
    try:
        return list(await reads.plan.story_nodes(graph_id, near))
    except PlanReadError:
        raise invalid_arguments() from None


def _near_first(hits: list[NodeHit], distance: dict[str, int]) -> list[NodeHit]:
    """Hits within reach of the start first, by hops then score; the others keep the library's order after them."""

    reached = sorted(
        (hit for hit in hits if hit.node.spec_id in distance),
        key=lambda hit: (distance[hit.node.spec_id], -hit.score),
    )
    return [*reached, *(hit for hit in hits if hit.node.spec_id not in distance)]
