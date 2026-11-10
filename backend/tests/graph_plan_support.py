"""Shared plan test helpers: ontology/rules, the synthetic shop reader and build reports."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from graph.build.models import BuildResult, ChangedEdge
from graph.core import load_ontology
from graph.core.ontology import Ontology
from graph.core.queries import DOCUMENT, FRAGMENT
from graph.core.reader import GraphDocument, GraphEdge, GraphFragment, GraphNode
from graph.core.writer import NodeRef
from graph.plan.answers import (
    ConventionAnswer,
    DescribeAnswer,
    DoneWhenAnswer,
    InterfaceAnswer,
    ProfileAnswer,
    ProjectAnswer,
    ReviewAnswer,
    StorySummaryAnswer,
    TaskAnswer,
    TasksAnswer,
)
from graph.plan.describe import describe
from graph.plan.fallback import spec_interfaces
from graph.plan.files import open_plan_store
from graph.plan.inputs import setup_members
from graph.plan.models import (
    BuildChangeReport,
    ChangedSpecEdge,
    ChangedSpecNode,
    ChangeSet,
    CurrentPlan,
    CurrentTask,
    DescribeArtifact,
    DiffArtifact,
    MeaningRevisions,
    PlanInfo,
    PlanLink,
    PlanOptions,
    ProjectArtifact,
    StoryInfo,
    TasksArtifact,
    WorkAreaInfo,
)
from graph.plan.names import PlanMode
from graph.plan.options import PlanContext
from graph.plan.partition import partition
from graph.plan.ports import PlanStore
from graph.plan.project import project
from graph.plan.prompts import PromptName, gateway_prompt
from graph.plan.reader import spec_aliases
from graph.plan.rules import PlanRules, load_plan_rules
from graph.plan.run import StepFunctions, run_with
from graph.plan.snapshot import snapshot
from graph.plan.tasks import plan_tasks
from graph.plan.view import PlanView
from graph_probes.base import SPEC_NODE, EdgeSpec, Match, select
from graph_support import NO_ANSWER, GraphProject, ScriptedGateway, ScriptedRequest
from test_graph_schema import SchemaDriver

__all__ = [
    "SchemaDriver",
    "SnapshotQueries",
    "applied_plan",
    "change_reports",
    "generic_planners",
    "live_context",
    "next_view",
    "plan_ontology",
    "plan_rules",
    "planned_live_shop",
    "planned_shop",
    "planner_key",
    "planner_payload",
    "planning_context",
    "script_plan",
    "shop_answers",
    "shop_graph",
    "shop_partition",
    "shop_snapshot",
    "shop_view",
    "write_spec_graph",
]

PLANNER_KINDS: tuple[PromptName, ...] = ("describe", "project", "tasks", "review")

_SHOP_FIXTURE: Path = Path(__file__).resolve().parent / "fixtures" / "graph" / "plan" / "shop.yaml"
_EMPTY_REVISIONS = MeaningRevisions()


def plan_ontology() -> Ontology:
    """The merged ontology the shipped plan rules target."""

    return load_ontology()


def plan_rules(ontology: Ontology | None = None) -> PlanRules:
    """The shipped plan rules, validated against the merged ontology."""

    return load_plan_rules(ontology=ontology or plan_ontology())


@dataclass(frozen=True)
class _ShopFixture:
    build_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    documents: tuple[GraphDocument, ...]
    fragments: tuple[GraphFragment, ...]


@dataclass(frozen=True)
class ShopGraphReader:
    """A project-independent fake of the part of ``GraphReader`` plan reads use."""

    _fixture: _ShopFixture
    _ontology: Ontology

    async def nodes(
        self,
        _project_id: str,
        labels: Sequence[str] | None = None,
        where: Mapping[str, Any] | None = None,
    ) -> list[GraphNode]:
        selected = self._labels(labels)
        filters = dict(where or {})
        found = [
            node
            for node in self._fixture.nodes
            if not selected or node.label in selected
            if all(node.attributes.get(key) == value for key, value in filters.items())
        ]
        return sorted(found, key=lambda node: node.spec_id)

    async def node(self, _project_id: str, spec_id: str) -> GraphNode | None:
        return next((node for node in self._fixture.nodes if node.spec_id == spec_id), None)

    async def nodes_by_spec_ids(self, _project_id: str, spec_ids: Sequence[str]) -> list[GraphNode]:
        wanted = set(spec_ids)
        found = [node for node in self._fixture.nodes if wanted & {node.spec_id, *node.spec_id_aliases}]
        return sorted(found, key=lambda node: node.spec_id)

    async def spec_aliases(self, _project_id: str) -> dict[str, str]:
        return spec_aliases(sorted(self._fixture.nodes, key=lambda node: node.spec_id))

    async def neighbours(
        self, _project_id: str, spec_id: str, relations: Sequence[str] | None = None
    ) -> list[GraphEdge]:
        selected = self._relations(relations)
        return sorted(
            (
                edge
                for edge in self._fixture.edges
                if spec_id in (edge.source, edge.target) and (not selected or edge.relation in selected)
            ),
            key=lambda edge: (edge.source, edge.relation, edge.target),
        )

    async def edges(self, _project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        return sorted(
            (edge for edge in self._fixture.edges if include_inferred or edge.inference_rule is None),
            key=lambda edge: (edge.source, edge.relation, edge.target),
        )

    async def documents(self, _project_id: str) -> list[GraphDocument]:
        return sorted(self._fixture.documents, key=lambda document: document.path or "")

    async def fragments(self, _project_id: str, document_id: str | None = None) -> list[GraphFragment]:
        return sorted(
            (
                fragment
                for fragment in self._fixture.fragments
                if document_id is None or fragment.document_id == document_id
            ),
            key=lambda fragment: (fragment.document_id, fragment.ordinal or 0, fragment.fragment_id),
        )

    def _labels(self, labels: Sequence[str] | None) -> frozenset[str]:
        if labels is None:
            return frozenset()
        return frozenset(labels)

    def _relations(self, relations: Sequence[str] | None) -> frozenset[str]:
        if relations is None:
            return frozenset()
        return frozenset(relations)


@dataclass(frozen=True)
class EmptyShopReader:
    """A fake reader with no spec nodes; used for the ``no_spec_graph`` error."""

    @classmethod
    def empty(cls) -> EmptyShopReader:
        return cls()

    async def nodes(
        self, _project_id: str, labels: Sequence[str] | None = None, where: Mapping[str, Any] | None = None
    ) -> list[GraphNode]:
        del labels, where
        return []

    async def edges(self, _project_id: str, *, include_inferred: bool = True) -> list[GraphEdge]:
        del include_inferred
        return []

    async def documents(self, _project_id: str) -> list[GraphDocument]:
        return []

    async def fragments(self, _project_id: str, document_id: str | None = None) -> list[GraphFragment]:
        del document_id
        return []


@dataclass(frozen=True)
class _MemoryPlanStore:
    """The minimal PlanStore Snapshot needs; tests pass prior state through it."""

    revisions: MeaningRevisions = _EMPTY_REVISIONS
    previous_result: Mapping[str, Any] | None = None
    result_exists: bool = False

    async def get_revisions(self, _project_id: str) -> MeaningRevisions:
        return self.revisions

    async def latest_run(
        self, _project_id: str, *, finished_only: bool = False, modes: Any = None
    ) -> _MemoryRun | None:
        del finished_only, modes
        return _MemoryRun() if self.result_exists else None

    async def get_result(self, _project_id: str, _run_id: str) -> Mapping[str, Any] | None:
        return self.previous_result


@dataclass(frozen=True)
class _MemoryRun:
    run_id: str = "run-1"


class SnapshotQueries:
    """Offline current-plan reads return no persisted plan by default."""

    def __init__(self, plan_rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.plan_rows = tuple(plan_rows)

    async def read(self, query: Any, _project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        if query.name == "plan_read_plan":
            return [dict(row) for row in self.plan_rows]
        return []


def ontology_labels(rules: PlanRules, key: str) -> tuple[str, ...]:
    """Resolve a rules type key or node group to its ontology labels."""

    return rules.labels_of(key)


async def write_spec_graph(
    project: GraphProject, project_id: str, fixture: ShopGraphReader, ontology: Ontology
) -> None:
    """Write the shop fixture to a live project for live plan tests (idempotent: a rewrite adds what is gone).

    Spec nodes and domain relationships go through ``OwnedWriter(owner="build")``; documents and
    fragments through its keyed upserts. Statements and provenance links are test data the owner
    writer refuses (structural relationships), so the test probe creates them; one link per type and
    pair of ends, as a merge would, and none that the project already holds.
    """

    writer = project.writer(project_id=project_id, ontology=ontology)
    by_spec_id = {node.spec_id: node for node in fixture._fixture.nodes}
    links: dict[tuple[str, str, str], EdgeSpec] = {}

    def link(relation: str, source: Match, target: Match, properties: Mapping[str, Any] | None = None) -> None:
        key = (relation, *(str(next(iter(end.props.values()))) for end in (source, target)))
        kept = {name: value for name, value in (properties or {}).items() if value is not None}
        links[key] = EdgeSpec(relation, source, target, kept)

    for document in fixture._fixture.documents:
        properties = document.model_dump(exclude={"document_id"}, exclude_none=True)
        await writer.upsert_node(DOCUMENT, document.document_id, properties)
    for fragment in fixture._fixture.fragments:
        properties = fragment.model_dump(exclude={"fragment_id"}, exclude_none=True)
        properties["heading_path"] = list(fragment.heading_path or ())
        await writer.upsert_node(FRAGMENT, fragment.fragment_id, properties)
        link(
            "CONTAINS",
            select(DOCUMENT, document_id=fragment.document_id),
            select(FRAGMENT, fragment_id=fragment.fragment_id),
        )
    for node in sorted(fixture._fixture.nodes, key=lambda item: item.spec_id):
        properties = {
            "spec_id": node.spec_id,
            "name": node.name,
            "last_build_id": node.last_build_id,
            **node.attributes,
        }
        await writer.upsert_node(node.label, f"{project_id}-{node.uuid}", properties)
        spec = select(SPEC_NODE, spec_id=node.spec_id)
        for statement in node.statements:
            link(
                "HAS_STATEMENT",
                spec,
                select(FRAGMENT, fragment_id=statement.fragment_id),
                {"text": statement.text, "revision_id": statement.revision_id, "version": statement.version},
            )
        for provenance in node.provenance:
            link(
                "REFERENCED_BY",
                spec,
                select(FRAGMENT, fragment_id=provenance.fragment_id),
                {"line_start": provenance.line_start, "line_end": provenance.line_end, "quote": provenance.quote},
            )
    held = {
        (edge.type, str(edge.source.ref), str(edge.target.ref)) for edge in await project.edges(project_id=project_id)
    }
    await project.create_edges([spec for key, spec in links.items() if key not in held], project_id=project_id)
    for edge in fixture._fixture.edges:
        source = by_spec_id[edge.source]
        target = by_spec_id[edge.target]
        properties = {
            "basis": edge.basis,
            "fact": edge.fact,
            "source_fragment_ids": list(edge.source_fragment_ids),
            "inference_rule": edge.inference_rule,
            "confidence": edge.confidence,
        }
        await writer.upsert_relationship(
            edge.relation,
            NodeRef(source.label, f"{project_id}-{source.uuid}"),
            NodeRef(target.label, f"{project_id}-{target.uuid}"),
            edge.edge_key or f"{edge.source}|{edge.relation}|{edge.target}",
            properties,
        )


def shop_graph(*mutations: str) -> ShopGraphReader:
    """The shop fixture as a fake reader; named mutations add test-only edges."""

    fixture = _load_shop_fixture()
    edges = list(fixture.edges)
    for mutation in mutations:
        if mutation == "empty":
            fixture = _ShopFixture("build-1", (), (), fixture.documents, fixture.fragments)
            return ShopGraphReader(fixture, plan_ontology())
        if mutation == "orphan_in_web_shop":
            edges.append(
                GraphEdge(
                    relation="PART_OF",
                    source="REQ::FR::ORPHAN",
                    target="MOD::WEB_SHOP",
                    edge_key="REQ_FR_ORPHAN|PART_OF|MOD_WEB_SHOP",
                    basis="inferred",
                    inference_rule="orphan_membership",
                    confidence=0.91,
                )
            )
        else:
            raise ValueError(f"unknown shop mutation: {mutation}")
    mutated = _ShopFixture(
        build_id=fixture.build_id,
        nodes=fixture.nodes,
        edges=tuple(edges),
        documents=fixture.documents,
        fragments=fixture.fragments,
    )
    return ShopGraphReader(mutated, plan_ontology())


async def shop_snapshot(
    *mutations: str,
    revisions: MeaningRevisions = _EMPTY_REVISIONS,
    previous_result: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    changes: tuple[BuildChangeReport, ...] = (),
    replan: bool = False,
    options: Any = None,
) -> Any:
    """The shop fixture snapshot, built through the production Snapshot function."""

    reader = shop_graph(*mutations)
    ontology = plan_ontology()
    context = PlanContext(
        project_id="shop",
        ontology=ontology,
        rules=plan_rules(ontology),
        driver=None,
        gateway=None,
        store=_MemoryPlanStore(
            revisions=revisions,
            previous_result=previous_result,
            result_exists=plan is not None,
        ),
        build_id="build-2",
        changes=changes,
        replan=replan,
        options=options or PlanOptions(),
    )
    plan_rows = () if plan is None else (plan,)
    return await snapshot(reader, SnapshotQueries(plan_rows), context)


async def shop_partition(*mutations: str) -> Any:
    """The shop fixture partition, built through production Snapshot and Partition."""

    artifact = await shop_snapshot(*mutations)
    ontology = plan_ontology()
    return partition(artifact, plan_rules(ontology), ontology)


async def shop_view(*mutations: str, **snapshot_options: Any) -> PlanView:
    """The shop fixture's Snapshot and Partition seen together, as the planning steps see them."""

    artifact = await shop_snapshot(*mutations, **snapshot_options)
    ontology = plan_ontology()
    rules = plan_rules(ontology)
    return PlanView(artifact, partition(artifact, rules, ontology), rules)


def planning_context(
    view: PlanView,
    store: PlanStore,
    gateway: Any,
    *,
    run_id: str | None = "run-1",
    options: PlanOptions | None = None,
    replan: bool = False,
) -> PlanContext:
    """A context bound to a run for the planning steps (no driver: they make no query)."""

    return PlanContext(
        project_id="shop",
        ontology=plan_ontology(),
        rules=view.rules,
        driver=None,
        gateway=gateway,
        store=store,
        build_id=view.snapshot.build_id,
        options=options or PlanOptions(),
        replan=replan,
        run_id=run_id,
    )


@dataclass(frozen=True)
class PlannedShop:
    """The shop planned with the scripted answers: the view and the planner artifacts."""

    view: PlanView
    describe: DescribeArtifact
    project: ProjectArtifact
    tasks: TasksArtifact


async def planned_shop(view: PlanView, store: PlanStore, gateway: ScriptedGateway | None = None) -> PlannedShop:
    """Run Describe, Project and Tasks over ``view`` with the scripted shop answers (or ``gateway``)."""

    gateway = gateway or script_plan(ScriptedGateway(), shop_answers(view))
    ctx = planning_context(view, store, gateway)
    mode = view.snapshot.mode
    described = await describe(ctx, view, mode=mode)
    projected = await project(ctx, view, described, mode=mode)
    return PlannedShop(view, described, projected, await plan_tasks(ctx, view, described, projected, mode=mode))


APPLIED_AT = "2026-01-01T12:00:00+00:00"


def applied_plan(
    view: PlanView,
    artifact: DiffArtifact,
    *,
    states: Mapping[str, Mapping[str, Any]] | None = None,
) -> CurrentPlan:
    """The current plan after ``artifact`` is applied, in memory (what Persist would write); ``states``
    overrides task properties by task id (status, commit, claim fields), as an agent's work would."""

    plan = PlanInfo.model_validate(
        {**artifact.plan_properties, "created_at": APPLIED_AT, "updated_at": APPLIED_AT, "applying": None}
    )
    current = view.snapshot.current
    stamps = {"created_at": APPLIED_AT, "updated_at": APPLIED_AT}
    areas = {area.area_id: area.model_dump() for area in current.areas}
    for op in artifact.areas:
        areas[op.id] = {**areas.get(op.id, {}), **op.properties, **stamps}
    stories = {story.story_id: story.model_dump() for story in current.stories}
    for op in artifact.stories:
        stories[op.id] = {**stories.get(op.id, {}), **op.properties, **stamps}
    tasks = {task.task_id: task.model_dump() for task in current.tasks}
    for op in artifact.tasks:
        tasks[op.id] = {**tasks.get(op.id, {}), **op.properties, **stamps}
    for task_id, overrides in (states or {}).items():
        tasks[task_id] = {**tasks[task_id], **overrides}
    uuid_to_id = {node.uuid: node.spec_id for node in view.snapshot.plannable}
    deleted = {(link.type, link.source, link.target, link.key) for link in artifact.links_delete}
    links = [link for link in current.links if (link.type, link.source, link.target, link.edge_key) not in deleted]
    links.extend(
        PlanLink(
            type=link.type,
            source=uuid_to_id.get(link.source, link.source),
            target=uuid_to_id.get(link.target, link.target),
            edge_key=link.key,
            properties=tuple(
                sorted(
                    (key, tuple(value) if isinstance(value, list) else value) for key, value in link.properties.items()
                )
            ),
        )
        for link in artifact.links_create
    )
    return CurrentPlan(
        plan=plan,
        areas=tuple(WorkAreaInfo.model_validate(row) for _, row in sorted(areas.items())),
        stories=tuple(StoryInfo.model_validate(row) for _, row in sorted(stories.items())),
        tasks=tuple(CurrentTask.model_validate(row) for _, row in sorted(tasks.items())),
        links=tuple(sorted(links, key=lambda link: link.edge_key)),
    )


def next_view(
    view: PlanView,
    current: CurrentPlan,
    *,
    build_id: str = "build-3",
    changed: Mapping[str, str] | None = None,
    mode: PlanMode = "update",
    snapshot_updates: Mapping[str, Any] | None = None,
) -> PlanView:
    """The view of the next run over ``current``: ``changed`` maps spec ids to meaning-change classes of
    build ``build_id`` (their revision becomes that build)."""

    changes = dict(changed or {})
    consumed = (*view.snapshot.revisions.consumed, build_id)
    revisions = MeaningRevisions(
        nodes={**view.snapshot.revisions.nodes, **dict.fromkeys(changes, build_id)}, consumed=consumed
    )
    snapshot = view.snapshot.model_copy(
        update={
            "mode": mode,
            "build_id": build_id,
            "current": current,
            "revisions": revisions,
            "change_set": ChangeSet(meaning=changes),
            **(snapshot_updates or {}),
        }
    )
    ontology = plan_ontology()
    return PlanView(snapshot, partition(snapshot, view.rules, ontology), view.rules)


def live_context(project: GraphProject, project_id: str, store: PlanStore, gateway: Any, **fields: Any) -> PlanContext:
    """A plan context over a live test project's store (the shop fixture's ontology and rules)."""

    ontology = plan_ontology()
    values: dict[str, Any] = {
        "project_id": project_id,
        "ontology": ontology,
        "rules": plan_rules(ontology),
        "driver": project.store,
        "gateway": gateway,
        "store": store,
        "build_id": "build-1",
        **fields,
    }
    return PlanContext(**values)


async def planned_live_shop(project: GraphProject, store_root: Path) -> tuple[str, PlanContext]:
    """A live project holding the shop fixture and its first plan (scripted planners)."""

    project_id = project.new_project()
    await write_spec_graph(project, project_id, shop_graph(), plan_ontology())
    gateway = script_plan(ScriptedGateway(), shop_answers(await shop_view()))
    context = live_context(project, project_id, open_plan_store(store_root), gateway)
    result = await run_with(context, StepFunctions())
    if result.checks is None or not result.checks.ok:
        raise AssertionError("the first shop plan does not pass its checks")
    return project_id, context


def planner_payload(request: ScriptedRequest) -> dict[str, Any]:
    """The JSON input of a planner request (text fields hold block ids)."""

    user = request.contents("user")[0]
    start = user.index("{")
    payload, _ = json.JSONDecoder().raw_decode(user[start:])
    return dict(payload)


def planner_key(request: ScriptedRequest) -> str:
    """The store key of a planner request: the area of Describe, the part of Tasks, else the planner name."""

    payload = planner_payload(request)
    if "part_id" in payload:
        return str(payload["part_id"])
    area = payload.get("area")
    if isinstance(area, dict) and request.prompt == gateway_prompt("describe"):
        return str(area["area_id"])
    return request.prompt.split(".")[1]


def script_plan(gateway: ScriptedGateway, answers: Mapping[tuple[str, str], Any]) -> ScriptedGateway:
    """Answer planner calls by ``(planner, key)`` (``planner_key``); a value may be an answer, a dict, an
    exception, or a list of those answered one per call (the last one repeats)."""

    remaining = {key: list(value) if isinstance(value, list) else [value] for key, value in answers.items()}
    for kind in PLANNER_KINDS:

        def handle(request: ScriptedRequest, kind: str = kind) -> Any:
            queue = remaining.get((kind, planner_key(request)))
            if not queue:
                return NO_ANSWER
            return queue.pop(0) if len(queue) > 1 else queue[0]

        gateway.handle(gateway_prompt(kind), handle)
    return gateway


def shop_answers(view: PlanView) -> dict[tuple[str, str], BaseModel]:
    """A valid answer for every planner call of the shop plan, derived from the partition."""

    answers: dict[tuple[str, str], BaseModel] = {}
    for area in view.active_areas:
        answers[("describe", area.area_id)] = DescribeAnswer(
            title=f"Area {area.area_id}",
            description="What the area builds.",
            interfaces=[
                InterfaceAnswer(
                    spec_id=item.spec_id or "", name=item.name, kind=item.kind, summary="Summary.", spec_ids=[]
                )
                for item in spec_interfaces(view, area.area_id)
            ],
            stories=[
                StorySummaryAnswer(story_id=story.story_id, summary="Delivers the story.")
                for story in view.area_stories(area.area_id)
                if story.kind not in ("setup", "closing")
            ],
        )
    answers[("project", "project")] = ProjectAnswer(
        profile=ProfileAnswer(
            languages=["python"],
            frameworks=[],
            layout="src",
            test_command="pytest",
            test_paths=["tests"],
            spec_ids=[],
            conventions=[ConventionAnswer(text="Every service runs in the EU region.", spec_ids=["CON::EU_REGION"])],
        ),
        setup_tasks=[
            _answer_task("WORKSPACE", "setup", [], []),
            _answer_task("TOOLCHAIN", "setup", list(setup_members(view)), ["WORKSPACE"]),
        ],
        closing_tasks=[_answer_task("END_TO_END", "acceptance", [], [])],
        setup_summary="Workspace and toolchain.",
        closing_summary="End-to-end acceptance.",
    )
    for part in view.partition.parts:
        tasks: list[TaskAnswer] = []
        for story_id in part.story_ids:
            story = view.stories[story_id]
            by_phase: dict[str, list[str]] = {}
            for spec_id in story.members:
                if spec_id in part.members and spec_id != "CON::EU_REGION":
                    by_phase.setdefault(view.phase_of(spec_id), []).append(spec_id)
            slug = story_id.rsplit("::", 1)[-1]
            earlier: list[str] = []
            for phase in ("contract", "data", "logic", "integration", "quality"):
                if phase not in by_phase:
                    continue
                action = f"{phase.upper()}_{slug}"[:40].rstrip("_")
                tasks.append(_answer_task(action, phase, by_phase[phase], list(earlier), story=story_id, view=view))
                earlier.append(action)
        answers[("tasks", part.part_id)] = TasksAnswer(tasks=tasks, not_buildable=[])
    answers[("review", "review")] = ReviewAnswer(issues=[])
    return answers


def _answer_task(
    action: str,
    phase: str,
    implements: Sequence[str],
    waits: Sequence[str],
    *,
    story: str = "",
    view: PlanView | None = None,
) -> TaskAnswer:
    done_when = [DoneWhenAnswer(text=f"{action} is done", source="planner")]
    if view is not None and any(view.rules.acceptance_of(view.nodes[spec_id].attributes) for spec_id in implements):
        done_when.append(DoneWhenAnswer(text="The acceptance criteria hold", source="spec"))
    return TaskAnswer(
        story=story,
        action=action,
        phase=phase,
        title=f"Build {action.lower()}",
        description="What to build.",
        implements=list(implements),
        waits_for=list(waits),
        done_when=done_when,
        complexity="medium",
    )


def change_reports(results: Iterable[BuildResult]) -> tuple[BuildChangeReport, ...]:
    """Convert shop-v2 build results, in chain order, to the plan-side report model."""

    reports: list[BuildChangeReport] = []
    previous_build_id: str | None = None
    for result in results:
        if result.change_report is None:
            continue
        reports.append(
            BuildChangeReport(
                build_id=result.build_id,
                previous_build_id=previous_build_id,
                nodes=tuple(
                    ChangedSpecNode(
                        spec_id=node.spec_id,
                        classes=node.classes,
                        previous_spec_ids=node.previous_spec_ids,
                    )
                    for node in result.change_report.nodes
                ),
                edges=tuple(_changed_edge(edge) for edge in result.change_report.edges),
            )
        )
        previous_build_id = result.build_id
    return tuple(reports)


def _changed_edge(edge: ChangedEdge) -> ChangedSpecEdge:
    parts = edge.ref.split("|")
    if len(parts) != 3:
        raise ValueError(f"invalid changed edge ref: {edge.ref}")
    source, relation, target = parts
    return ChangedSpecEdge(
        source=source,
        relation=relation,
        target=target,
        change=edge.change,
        basis=edge.basis,
    )


def _load_shop_fixture() -> _ShopFixture:
    with _SHOP_FIXTURE.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise TypeError("shop fixture must be a mapping")
    nodes = tuple(GraphNode.model_validate(item) for item in data.get("nodes", ()))
    edges = tuple(GraphEdge.model_validate(item) for item in data.get("edges", ()))
    documents = tuple(GraphDocument.model_validate(item) for item in data.get("documents", ()))
    fragments = tuple(GraphFragment.model_validate(item) for item in data.get("fragments", ()))
    _validate_shop_fixture(nodes, edges, documents, fragments)
    return _ShopFixture(
        build_id=str(data["build_id"]),
        nodes=nodes,
        edges=edges,
        documents=documents,
        fragments=fragments,
    )


def _validate_shop_fixture(
    nodes: tuple[GraphNode, ...],
    edges: tuple[GraphEdge, ...],
    documents: tuple[GraphDocument, ...],
    fragments: tuple[GraphFragment, ...],
) -> None:
    node_ids = [node.spec_id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise ValueError("shop fixture has duplicate node ids")
    known_nodes = frozenset(node_ids)
    for edge in edges:
        if edge.source not in known_nodes or edge.target not in known_nodes:
            raise ValueError(f"shop edge has an unknown endpoint: {edge.edge_key}")
    document_ids = frozenset(document.document_id for document in documents)
    if len(document_ids) != len(documents):
        raise ValueError("shop fixture has duplicate document ids")
    fragment_ids = [fragment.fragment_id for fragment in fragments]
    if len(fragment_ids) != len(set(fragment_ids)):
        raise ValueError("shop fixture has duplicate fragment ids")
    if any(fragment.document_id not in document_ids for fragment in fragments):
        raise ValueError("shop fragment names an unknown document")


def shop_fixture_data() -> dict[str, Any]:
    """The parsed shop fixture file, for tests that plan a variant of it."""

    with _SHOP_FIXTURE.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise TypeError("shop fixture must be a mapping")
    return data


def fixture_graph(data: Mapping[str, Any], ontology: Ontology) -> ShopGraphReader:
    """A fake reader over parsed fixture data in the shop fixture's format."""

    nodes = tuple(GraphNode.model_validate(item) for item in data.get("nodes", ()))
    edges = tuple(GraphEdge.model_validate(item) for item in data.get("edges", ()))
    documents = tuple(GraphDocument.model_validate(item) for item in data.get("documents", ()))
    fragments = tuple(GraphFragment.model_validate(item) for item in data.get("fragments", ()))
    _validate_shop_fixture(nodes, edges, documents, fragments)
    return ShopGraphReader(_ShopFixture(str(data["build_id"]), nodes, edges, documents, fragments), ontology)


async def fixture_view(reader: ShopGraphReader, ontology: Ontology, rules: PlanRules) -> PlanView:
    """Snapshot and Partition of ``reader`` under ``ontology`` and ``rules``, as ``shop_view`` builds them."""

    context = PlanContext(
        project_id="shop",
        ontology=ontology,
        rules=rules,
        driver=None,
        gateway=None,
        store=_MemoryPlanStore(),
        build_id="build-2",
        options=PlanOptions(),
    )
    artifact = await snapshot(reader, SnapshotQueries(), context)
    return PlanView(artifact, partition(artifact, rules, ontology), rules)


def generic_planners(rules: PlanRules) -> ScriptedGateway:
    """Planners that answer any project from the request itself: one task per home story and phase."""

    gateway = ScriptedGateway()

    def describe(request: ScriptedRequest) -> DescribeAnswer:
        payload = planner_payload(request)
        return DescribeAnswer(
            title=f"Area {payload['area']['area_id']}",
            description="What the area builds.",
            interfaces=[
                InterfaceAnswer(spec_id=item["spec_id"], name="Interface", kind=item["kind"], summary="", spec_ids=[])
                for item in payload["interfaces"]
            ],
            stories=[
                StorySummaryAnswer(story_id=item["story_id"], summary="Delivers it.") for item in payload["stories"]
            ],
        )

    def project(request: ScriptedRequest) -> ProjectAnswer:
        members = [item["spec_id"] for item in planner_payload(request)["members"]]
        return ProjectAnswer(
            profile=ProfileAnswer(
                languages=["python"],
                frameworks=[],
                layout="src",
                test_command="pytest",
                test_paths=[],
                spec_ids=[],
                conventions=[],
            ),
            setup_tasks=[
                _generic_task("WORKSPACE", "setup", [], []),
                _generic_task("TOOLCHAIN", "setup", members, ["WORKSPACE"]),
            ],
            closing_tasks=[_generic_task("END_TO_END", "acceptance", [], [])],
            setup_summary="Setup.",
            closing_summary="Closing.",
        )

    def tasks(request: ScriptedRequest) -> TasksAnswer:
        payload = planner_payload(request)
        groups: dict[tuple[str, str], list[str]] = {}
        for member in payload["members"]:
            phase = rules.rule_of(member["label"]).phase or "logic"
            groups.setdefault((member["home_story"], phase), []).append(member["spec_id"])
        return TasksAnswer(
            tasks=[
                _generic_task(_stable_action(phase, story), phase, implements, [], story=story)
                for (story, phase), implements in sorted(groups.items())
            ],
            not_buildable=[],
        )

    gateway.handle(gateway_prompt("describe"), describe)
    gateway.handle(gateway_prompt("project"), project)
    gateway.handle(gateway_prompt("tasks"), tasks)
    gateway.handle(gateway_prompt("review"), lambda _request: ReviewAnswer(issues=[]))
    return gateway


def _stable_action(phase: str, story_id: str) -> str:
    """An action that stays the same across runs for the same story and phase, as planners are asked to."""

    return f"{phase.upper()}_{story_id.rsplit('::', 1)[-1]}"[:40].rstrip("_")


def _generic_task(action: str, phase: str, implements: list[str], waits: list[str], *, story: str = "") -> TaskAnswer:
    return TaskAnswer(
        story=story,
        action=action,
        phase=phase,
        title=f"Build {action.lower()}",
        description="What to build.",
        implements=implements,
        waits_for=waits,
        done_when=[DoneWhenAnswer(text=f"{action} works", source="planner")],
        complexity="medium",
    )
