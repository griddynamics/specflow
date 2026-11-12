"""The Diff step: the writes that turn the current plan into the target plan.

Target tasks are matched to current tasks by task key, then by their ``implements`` set. A task whose spec
changed (an implemented node's meaning revision is newer than its ``spec_build_id``, or the node no longer
resolves) is redone: a completed one is superseded by a new revision, an open one is reset to
``pending``. In-progress tasks are never touched. Plan relationships are matched by their endpoints,
never by key, because build moves them to a merge survivor with their old keys. The target plan must
keep the plan invariants, else ``ConformanceError("plan_invariant")``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

from graph.core.errors import ConformanceError
from graph.plan.identity import revision_id
from graph.plan.invariants import PlanState, StoryState, TaskState, failing, problems
from graph.plan.models import (
    CurrentTask,
    DescribeArtifact,
    DiffArtifact,
    LinkOp,
    NodeOp,
    NodeOpKind,
    PlanChangesSummary,
    PlannedTask,
    ProjectArtifact,
    StitchArtifact,
    SupersededTask,
    TasksArtifact,
)
from graph.plan.names import PlanRelation
from graph.plan.planned import base_key
from graph.plan.view import PlanView

PLAN_ID: Final = "PLAN::MAIN"
_KEPT_INVARIANTS: Final = ("membership", "task_area", "coverage", "dependencies", "task_state", "stories")
_EDITABLE: Final = frozenset({"pending", "failed", "blocked"})
_PLANNER: Final = "planner"
IN_AREA: Final[PlanRelation] = "IN_AREA"
IN_STORY: Final[PlanRelation] = "IN_STORY"
IMPLEMENTS: Final[PlanRelation] = "IMPLEMENTS"
WAITS_FOR: Final[PlanRelation] = "WAITS_FOR"
SUPERSEDES: Final[PlanRelation] = "SUPERSEDES"


@dataclass(frozen=True)
class DiffInputs:
    """What Diff needs besides the artifacts: versions, fingerprint and whether reports were given."""

    ontology_version: str
    plan_ontology_version: str
    rules_version: str
    replan_without_reports: bool = False


@dataclass
class _TaskPlan:
    """The decision for one target or current task."""

    final_id: str
    target: PlannedTask | None
    current: CurrentTask | None
    op: NodeOpKind | Literal["untouched"]
    cause: str | None = None
    successor_of: str | None = None


@dataclass
class _State:
    decisions: dict[str, _TaskPlan] = field(default_factory=dict)  # by final id
    retired: dict[str, CurrentTask] = field(default_factory=dict)
    superseded: dict[str, tuple[str, str]] = field(default_factory=dict)  # current id -> (successor, cause)
    in_progress_changed: set[str] = field(default_factory=set)
    unverified: set[str] = field(default_factory=set)


def link_key(source: str, relation: str, target: str) -> str:
    """The key of a plan relationship written now: ``<source id>|<RELATION>|<target id>``."""

    return f"{source}|{relation}|{target}"


def diff(
    view: PlanView,
    *,
    describe: DescribeArtifact,
    project: ProjectArtifact,
    tasks: TasksArtifact,
    stitch: StitchArtifact,
    inputs: DiffInputs,
) -> DiffArtifact:
    """The writes from the current plan to the target plan."""

    targets = sorted((*project.setup, *project.closing, *tasks.tasks), key=lambda task: task.task_key)
    state = _decide(view, targets, inputs)
    key_to_id = {decision.target.task_key: final for final, decision in state.decisions.items() if decision.target}
    task_ops = _task_ops(view, state)
    story_ops, active_stories = _story_ops(view, describe, project)
    area_ops = _area_ops(view, describe, tasks)
    deletes, creates = _link_ops(view, state, key_to_id, stitch, active_stories)
    summary = _summary(state, area_ops, story_ops, task_ops)
    artifact = DiffArtifact(
        areas=tuple(area_ops),
        stories=tuple(story_ops),
        tasks=tuple(task_ops),
        links_delete=tuple(deletes),
        links_create=tuple(creates),
        plan_properties={
            "plan_id": PLAN_ID,
            "profile": json.dumps(project.profile.model_dump(mode="json"), sort_keys=True),
            "last_build_id": view.snapshot.build_id,
            "consumed_build_ids": list(view.snapshot.revisions.consumed),
            "graph_fingerprint": view.snapshot.fingerprint,
            "ontology_version": inputs.ontology_version,
            "plan_ontology_version": inputs.plan_ontology_version,
            "rules_version": inputs.rules_version,
        },
        summary=summary,
        stale_commits=_stale(view, state),
        unverified_completed=tuple(sorted(state.unverified)),
        in_progress_changed=tuple(sorted(state.in_progress_changed)),
        affected_dependants=_affected(view, state),
        revisions=view.snapshot.revisions,
    )
    _check(view, artifact, tasks, project)
    return artifact


def _latest(current: Iterable[CurrentTask]) -> dict[str, CurrentTask]:
    latest: dict[str, CurrentTask] = {}
    for task in current:
        key = base_key(task.task_id)
        if key not in latest or task.revision > latest[key].revision:
            latest[key] = task
    return latest


def _match(view: PlanView, targets: Sequence[PlannedTask], latest: Mapping[str, CurrentTask]) -> dict[str, CurrentTask]:
    matched = {task.task_key: latest[task.task_key] for task in targets if task.task_key in latest}
    used = {task.task_id for task in matched.values()}
    by_members: dict[frozenset[str], list[CurrentTask]] = {}
    for task in sorted(latest.values(), key=lambda item: item.task_id):
        if task.task_id not in used and task.implements and task.status != "superseded":
            by_members.setdefault(frozenset(view.resolve(spec_id) for spec_id in task.implements), []).append(task)
    for target in targets:
        if target.task_key in matched or not target.implements:
            continue
        candidates = by_members.get(frozenset(target.implements))
        if candidates:
            matched[target.task_key] = candidates.pop(0)
    return matched


def _decide(view: PlanView, targets: Sequence[PlannedTask], inputs: DiffInputs) -> _State:
    current = view.snapshot.current
    latest = _latest(current.tasks)
    matched = _match(view, targets, latest)
    revisions = _next_revisions(current.tasks)
    state = _State()
    matched_ids = {task.task_id for task in matched.values()}
    for target in targets:
        found = matched.get(target.task_key)
        if found is None:
            state.decisions[target.task_key] = _TaskPlan(target.task_key, target, None, "create")
            continue
        cause = _cause(view, found, inputs)
        if _unverified(view, found, cause, inputs):
            state.unverified.add(found.task_id)
        _decide_matched(state, target, found, cause, revisions)
    for task in latest.values():
        if task.task_id in matched_ids:
            continue
        if task.status == "in_progress":
            state.in_progress_changed.add(task.task_id)
        elif task.status in _EDITABLE or task.status == "completed":
            state.retired[task.task_id] = task
    _cascade(view, state, revisions)
    _flag_in_progress(view, state)
    return state


def _unverified(view: PlanView, task: CurrentTask, cause: str | None, inputs: DiffInputs) -> bool:
    """A completed task a report-less replan keeps although an implemented node was written after it."""

    return (
        inputs.replan_without_reports
        and task.status == "completed"
        and cause is None
        and view.spec_change(task.implements, task.spec_build_id) is not None
    )


def _cause(view: PlanView, task: CurrentTask, inputs: DiffInputs) -> str | None:
    cause = view.spec_change(task.implements, task.spec_build_id)
    if inputs.replan_without_reports and task.status == "completed":
        return cause if cause == "unresolved" else None
    return cause


def _decide_matched(
    state: _State, target: PlannedTask, found: CurrentTask, cause: str | None, revisions: dict[str, int]
) -> None:
    status = found.status
    if status == "in_progress":
        state.decisions[found.task_id] = _TaskPlan(found.task_id, target, found, "untouched")
        if cause:
            state.in_progress_changed.add(found.task_id)
    elif status == "completed" and cause:
        _new_revision(state, target, found, cause, revisions)
    elif status == "completed":
        state.decisions[found.task_id] = _TaskPlan(found.task_id, target, found, "keep")
    elif status in _EDITABLE:
        op: NodeOpKind = "reset" if cause and status != "pending" else "update"
        state.decisions[found.task_id] = _TaskPlan(found.task_id, target, found, op, cause)
    elif status == "retired" and not found.commit:
        state.decisions[found.task_id] = _TaskPlan(found.task_id, target, found, "revive")
    else:
        # Retired work with a commit (or a superseded latest revision) returns as a new revision; the old
        # task keeps its status.
        _new_revision(state, target, found, cause or "returned", revisions, supersede=False)


def _new_revision(
    state: _State,
    target: PlannedTask,
    found: CurrentTask,
    cause: str,
    revisions: dict[str, int],
    *,
    supersede: bool = True,
) -> None:
    base = base_key(found.task_id)
    revisions[base] = revisions.get(base, 1) + 1
    successor = revision_id(base, revisions[base])
    state.decisions[successor] = _TaskPlan(successor, target, None, "create", cause, successor_of=found.task_id)
    if supersede:
        state.superseded[found.task_id] = (successor, cause)


def _next_revisions(tasks: Iterable[CurrentTask]) -> dict[str, int]:
    found: dict[str, int] = {}
    for task in tasks:
        key = base_key(task.task_id)
        found[key] = max(found.get(key, 1), task.revision)
    return found


def _planner_waits(view: PlanView) -> dict[str, set[str]]:
    by_id = {task.task_id: task for task in view.snapshot.current.tasks}
    waits: dict[str, set[str]] = {}
    for link in view.snapshot.current.links:
        if link.type == WAITS_FOR and dict(link.properties).get("source") == _PLANNER:
            source, target = by_id.get(link.source), by_id.get(link.target)
            if source is not None and target is not None and source.area_id == target.area_id:
                waits.setdefault(link.source, set()).add(link.target)
    return waits


def _cascade(view: PlanView, state: _State, revisions: dict[str, int]) -> None:
    """A kept completed task waiting (planner wait, inside its area) on redone work is redone too."""

    waits = _planner_waits(view)
    changed = True
    while changed:
        changed = False
        lost = set(state.superseded) | set(state.retired)
        for task_id, decision in sorted(state.decisions.items()):
            if decision.op != "keep" or decision.target is None or decision.current is None:
                continue
            if waits.get(task_id, set()) & lost:
                del state.decisions[task_id]
                _new_revision(state, decision.target, decision.current, "cascade", revisions)
                changed = True


def _flag_in_progress(view: PlanView, state: _State) -> None:
    lost = set(state.superseded) | set(state.retired)
    for link in view.snapshot.current.links:
        if link.type == WAITS_FOR and link.target in lost:
            decision = state.decisions.get(link.source)
            if decision is not None and decision.op == "untouched":
                state.in_progress_changed.add(link.source)


def _task_properties(target: PlannedTask, task_id: str, build_id: str) -> dict[str, Any]:
    action = base_key(task_id).rsplit("::", 1)[-1]
    revision = int(task_id.rsplit("_R", 1)[1]) if task_id != base_key(task_id) else 1
    return {
        "task_id": task_id,
        "area_id": target.area_id,
        "action": action,
        "revision": revision,
        "story_id": target.story_id,
        "phase": target.phase,
        "title": target.title,
        "description": target.description,
        "done_when": json.dumps([item.model_dump(mode="json") for item in target.done_when]),
        "complexity": target.complexity,
        "implements": list(target.implements),
        "release": list(target.release),
        "has_open_questions": target.has_open_questions,
        "planned_by": target.planned_by,
        "prompt_ref": target.prompt_ref,
        "spec_build_id": build_id,
    }


def _changed(task: CurrentTask, properties: Mapping[str, Any]) -> bool:
    """Whether writing ``properties`` would change the stored task (an unchanged task is not written). A new
    ``spec_build_id`` alone is no change: an older one is still in the consumed chain, so it stays correct."""

    stored = {
        **task.model_dump(mode="json"),
        "done_when": json.dumps([item.model_dump(mode="json") for item in task.done_when]),
    }
    return any(
        _plain(stored.get(name)) != _plain(value) for name, value in properties.items() if name != "spec_build_id"
    )


def _task_ops(view: PlanView, state: _State) -> list[NodeOp]:
    build_id = view.snapshot.build_id
    ops: list[NodeOp] = []
    for final_id, decision in sorted(state.decisions.items()):
        target, found = decision.target, decision.current
        if decision.op == "untouched" or target is None:
            continue
        properties = _task_properties(target, final_id, build_id)
        if decision.op == "create":
            ops.append(NodeOp(op="create", label="Task", id=final_id, properties={**properties, "status": "pending"}))
        elif found is not None and decision.op == "keep":
            content = {
                name: properties[name]
                for name in ("area_id", "title", "description", "done_when", "story_id", "release", "spec_build_id")
            }
            if _changed(found, content):
                ops.append(
                    NodeOp(op="keep", label="Task", id=final_id, expected_status=found.status, properties=content)
                )
        elif found is not None and decision.op in ("reset", "revive"):
            ops.append(
                NodeOp(
                    op=cast(NodeOpKind, decision.op),
                    label="Task",
                    id=final_id,
                    expected_status=found.status,
                    properties={**properties, "status": "pending", "block_reason": None, "blocked_by": None},
                )
            )
        elif found is not None and (decision.cause is not None or _changed(found, properties)):
            ops.append(
                NodeOp(op="update", label="Task", id=final_id, expected_status=found.status, properties=properties)
            )
    current = {task.task_id: task for task in view.snapshot.current.tasks}
    ops.extend(
        NodeOp(
            op="supersede",
            label="Task",
            id=task_id,
            expected_status=current[task_id].status,
            properties={"status": "superseded"},
        )
        for task_id in sorted(state.superseded)
    )
    ops.extend(
        NodeOp(op="retire", label="Task", id=task_id, expected_status=task.status, properties={"status": "retired"})
        for task_id, task in sorted(state.retired.items())
    )
    return ops


def _story_ops(
    view: PlanView, describe: DescribeArtifact, project: ProjectArtifact
) -> tuple[list[NodeOp], dict[str, Any]]:
    current = {story.story_id: story for story in view.snapshot.current.stories}
    busy = {task.story_id for task in view.snapshot.current.tasks if task.status == "in_progress"}
    summaries = {**describe.story_summaries, **project.summaries}
    refs = _summary_refs(view, describe, project)
    ops: list[NodeOp] = []
    active: dict[str, Any] = {}
    for story in sorted(view.partition.stories, key=lambda item: item.story_id):
        existing = current.get(story.story_id)
        if story.status == "retired":
            if existing is not None and existing.status == "active" and story.story_id not in busy:
                ops.append(NodeOp(op="retire", label="Story", id=story.story_id, properties={"status": "retired"}))
            elif existing is not None and existing.status == "active":
                active[story.story_id] = existing
            continue
        summary = summaries.get(story.story_id) or (existing.summary if existing is not None else None)
        properties = {
            "story_id": story.story_id,
            "area_id": story.area_id,
            "kind": story.kind,
            "anchor": story.anchor,
            "chapter": json.dumps(story.chapter.model_dump()) if story.chapter is not None else None,
            "title": story.title,
            "summary": summary,
            "prompt_ref": refs.get(story.story_id),
            "release": story.release,
            "status": "active",
        }
        op: NodeOpKind = "create" if existing is None else ("revive" if existing.status == "retired" else "update")
        ops.append(NodeOp(op=op, label="Story", id=story.story_id, properties=properties))
        active[story.story_id] = story
    return ops, active


def _summary_refs(view: PlanView, describe: DescribeArtifact, project: ProjectArtifact) -> dict[str, str]:
    """The prompt revision that wrote each story summary (a summary a fallback kept records none)."""

    refs: dict[str, str] = {}
    for story_id in describe.story_summaries:
        story = view.stories.get(story_id)
        planned = describe.areas.get(story.area_id) if story is not None else None
        if planned is not None and planned.planned_by == "model" and planned.prompt_ref is not None:
            refs[story_id] = planned.prompt_ref
    if not project.outcome.fallback and (project.setup or project.closing):
        ref = next((task.prompt_ref for task in project.setup if task.prompt_ref), None)
        refs.update(dict.fromkeys(project.summaries, ref) if ref else {})
    return refs


def _area_ops(view: PlanView, describe: DescribeArtifact, tasks: TasksArtifact) -> list[NodeOp]:
    current = {area.area_id: area for area in view.snapshot.current.areas}
    busy = {task.area_id for task in view.snapshot.current.tasks if task.status == "in_progress"}
    ops: list[NodeOp] = []
    for area in sorted(view.partition.areas, key=lambda item: item.area_id):
        existing = current.get(area.area_id)
        if area.status == "retired":
            # An area keeps its status while it holds claimed work; a later run retires it.
            if existing is not None and existing.status == "active" and area.area_id not in busy:
                ops.append(NodeOp(op="retire", label="WorkArea", id=area.area_id, properties={"status": "retired"}))
            continue
        planned = describe.areas.get(area.area_id)
        properties = {
            "area_id": area.area_id,
            "kind": area.kind,
            "anchor": area.anchor,
            "title": planned.title if planned is not None else (existing.title if existing else None),
            "description": planned.description if planned is not None else (existing.description if existing else None),
            "interfaces": json.dumps(
                [item.model_dump(mode="json") for item in planned.interfaces] if planned is not None else []
            ),
            "not_buildable": json.dumps(
                [entry.model_dump(mode="json") for entry in tasks.not_buildable.get(area.area_id, ())]
            ),
            "planned_by": planned.planned_by if planned is not None else "rules",
            "prompt_ref": planned.prompt_ref if planned is not None else None,
            "status": "active",
        }
        op: NodeOpKind = "create" if existing is None else ("revive" if existing.status == "retired" else "update")
        ops.append(NodeOp(op=op, label="WorkArea", id=area.area_id, properties=properties))
    return ops


def _desired_links(
    view: PlanView,
    state: _State,
    key_to_id: Mapping[str, str],
    stitch: StitchArtifact,
    active_stories: Mapping[str, Any],
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], set[tuple[str, str]]]:
    """The wanted links by ``(type, source id, target id)``, and the ``(type, source)`` pairs they replace."""

    wanted: dict[tuple[str, str, str], dict[str, Any]] = {}
    managed: set[tuple[str, str]] = set()
    _area_links(view, wanted, managed)
    _task_links(view, state, key_to_id=key_to_id, stitch=stitch, wanted=wanted, managed=managed)
    for story_id, story in active_stories.items():
        managed.add((IMPLEMENTS, story_id))
        if story.anchor is not None and view.resolve(story.anchor) in view.nodes:
            wanted[(IMPLEMENTS, story_id, view.resolve(story.anchor))] = {}
    _retarget_kept_waits(view, state, wanted, managed)
    return wanted, managed


def _area_links(
    view: PlanView, wanted: dict[tuple[str, str, str], dict[str, Any]], managed: set[tuple[str, str]]
) -> None:
    active_areas = {area.area_id for area in view.active_areas}
    for spec_id, placement in view.partition.placements.items():
        if placement.area_id in active_areas:
            properties = {"rule": placement.rule, "via": list(placement.via)}
            if spec_id in view.partition.home_story:
                properties["story"] = view.partition.home_story[spec_id]
            wanted[(IN_AREA, spec_id, placement.area_id)] = properties
        managed.add((IN_AREA, spec_id))
    managed.update((IN_AREA, link.source) for link in view.snapshot.current.links if link.type == IN_AREA)


def _task_links(
    view: PlanView,
    state: _State,
    *,
    key_to_id: Mapping[str, str],
    stitch: StitchArtifact,
    wanted: dict[tuple[str, str, str], dict[str, Any]],
    managed: set[tuple[str, str]],
) -> None:
    waits: dict[str, list[tuple[str, str]]] = {}
    for wait in stitch.waits:
        waits.setdefault(wait.source, []).append((wait.target, wait.kind))
    for final_id, decision in state.decisions.items():
        target = decision.target
        if target is None or decision.op == "untouched":
            continue
        managed.add((IN_STORY, final_id))
        wanted[(IN_STORY, final_id, target.story_id)] = {}
        if decision.op == "keep":
            # Kept work keeps what it implemented; after a merge, one relationship per surviving node.
            managed.add((IMPLEMENTS, final_id))
            implemented = decision.current.implements if decision.current is not None else ()
            for survivor in {view.resolve(spec_id) for spec_id in implemented} & set(view.nodes):
                wanted[(IMPLEMENTS, final_id, survivor)] = {}
            continue
        managed.update({(IMPLEMENTS, final_id), (WAITS_FOR, final_id)})
        for survivor in {view.resolve(spec_id) for spec_id in target.implements} & set(view.nodes):
            wanted[(IMPLEMENTS, final_id, survivor)] = {}
        for prerequisite, kind in waits.get(target.task_key, ()):
            if prerequisite in key_to_id:
                wanted[(WAITS_FOR, final_id, key_to_id[prerequisite])] = {"source": kind}
        if decision.successor_of is not None:
            wanted[(SUPERSEDES, final_id, decision.successor_of)] = {}


def _retarget_kept_waits(
    view: PlanView,
    state: _State,
    wanted: dict[tuple[str, str, str], dict[str, Any]],
    managed: set[tuple[str, str]],
) -> None:
    """A kept completed task keeps its waits; one on redone work points to the successor, one on retired
    work is dropped, so no wait of a live task ends at a closed task."""

    kept = {final_id for final_id, decision in state.decisions.items() if decision.op == "keep"}
    for link in view.snapshot.current.links:
        if link.type != WAITS_FOR or link.source not in kept:
            continue
        managed.add((WAITS_FOR, link.source))
        properties = dict(link.properties)
        if link.target in state.superseded:
            wanted[(WAITS_FOR, link.source, state.superseded[link.target][0])] = properties
        elif link.target not in state.retired:
            wanted[(WAITS_FOR, link.source, link.target)] = properties


def _link_ops(
    view: PlanView,
    state: _State,
    key_to_id: Mapping[str, str],
    stitch: StitchArtifact,
    active_stories: Mapping[str, Any],
) -> tuple[list[LinkOp], list[LinkOp]]:
    wanted, managed = _desired_links(view, state, key_to_id, stitch, active_stories)
    deletes: list[LinkOp] = []
    present: set[tuple[str, str, str]] = set()
    for link in view.snapshot.current.links:
        endpoints = (link.type, link.source, link.target)
        if (link.type, link.source) not in managed:
            continue
        properties = dict(link.properties)
        if endpoints in wanted and endpoints not in present and _same(properties, wanted[endpoints]):
            present.add(endpoints)
            continue
        deletes.append(
            LinkOp(type=cast(PlanRelation, link.type), source=link.source, target=link.target, key=link.edge_key)
        )
    creates = [
        LinkOp(
            type=cast(PlanRelation, relation),
            source=_endpoint(view, source),
            target=_endpoint(view, target),
            key=link_key(source, relation, target),
            properties=properties,
        )
        for (relation, source, target), properties in sorted(wanted.items())
        if (relation, source, target) not in present
    ]
    return deletes, creates


def _same(stored: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    return {key: _plain(value) for key, value in stored.items()} == {
        key: _plain(value) for key, value in wanted.items()
    }


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _endpoint(view: PlanView, node_id: str) -> str:
    """A spec node by uuid (writes address spec nodes by uuid), a plan node by its id."""

    node = view.nodes.get(node_id)
    return node.uuid if node is not None else node_id


def _summary(
    state: _State, areas: Sequence[NodeOp], stories: Sequence[NodeOp], tasks: Sequence[NodeOp]
) -> PlanChangesSummary:
    def ids(ops: Sequence[NodeOp], op: str) -> tuple[str, ...]:
        return tuple(sorted(item.id for item in ops if item.op == op))

    decisions = state.decisions.values()
    return PlanChangesSummary(
        tasks_added=tuple(sorted(item.final_id for item in decisions if item.op == "create")),
        tasks_updated=ids(tasks, "update"),
        tasks_reopened=tuple(sorted(item.final_id for item in decisions if item.op in ("reset", "revive"))),
        tasks_superseded=tuple(
            SupersededTask(task_id=task_id, successor=successor, cause=cause)
            for task_id, (successor, cause) in sorted(state.superseded.items())
        ),
        tasks_retired=tuple(sorted(state.retired)),
        tasks_kept=tuple(sorted(item.final_id for item in decisions if item.op == "keep")),
        areas_added=ids(areas, "create"),
        areas_retired=ids(areas, "retire"),
        areas_reactivated=ids(areas, "revive"),
        stories_added=ids(stories, "create"),
        stories_retired=ids(stories, "retire"),
        stories_reactivated=ids(stories, "revive"),
    )


def _stale(view: PlanView, state: _State) -> dict[str, str]:
    """Commits of the tasks this run supersedes or retires, by task id."""

    current = {task.task_id: task for task in view.snapshot.current.tasks}
    closed = [current[task_id] for task_id in state.superseded] + list(state.retired.values())
    return {task.task_id: task.commit for task in sorted(closed, key=lambda item: item.task_id) if task.commit}


def _affected(view: PlanView, state: _State) -> tuple[str, ...]:
    kept = {final_id for final_id, decision in state.decisions.items() if decision.op == "keep"}
    found = {
        link.source
        for link in view.snapshot.current.links
        if link.type == WAITS_FOR
        and link.source in kept
        and link.target in state.superseded
        and dict(link.properties).get("source") != _PLANNER
    }
    return tuple(sorted(found))


def _check(view: PlanView, artifact: DiffArtifact, tasks: TasksArtifact, project: ProjectArtifact) -> None:
    """Refuse a target plan that breaks a plan invariant (membership, areas, coverage, waits, state, stories)."""

    state = target_state(view, artifact, tasks, project)
    found = problems(state, view.resolve)
    broken = [check for check in failing(found) if check in _KEPT_INVARIANTS]
    if broken:
        raise ConformanceError("plan_invariant", check=broken[0], problems=sum(len(found[name]) for name in broken))


def target_state(view: PlanView, artifact: DiffArtifact, tasks: TasksArtifact, project: ProjectArtifact) -> PlanState:
    """The plan state after the diff is applied (in-progress tasks as they are)."""

    current_tasks = {task.task_id: task for task in view.snapshot.current.tasks}
    task_state = {
        task_id: TaskState(
            area_id=task.area_id,
            story_id=task.story_id,
            status=task.status,
            implements=task.implements,
            commit=task.commit,
            claimed=task.claimed_by is not None,
        )
        for task_id, task in current_tasks.items()
    }
    for op in artifact.tasks:
        before = task_state.get(op.id)
        properties = op.properties
        task_state[op.id] = TaskState(
            area_id=properties.get("area_id", before.area_id if before else ""),
            story_id=properties.get("story_id", before.story_id if before else ""),
            status=properties.get("status", before.status if before else "pending"),
            implements=tuple(properties.get("implements", before.implements if before else ())),
            commit=before.commit if before else None,
            claimed=before.claimed if before and op.op not in ("reset", "revive") else False,
        )
    areas = {area.area_id: area.status for area in view.snapshot.current.areas}
    areas.update({op.id: op.properties.get("status", "active") for op in artifact.areas})
    stories = {
        story.story_id: StoryState(area_id=story.area_id, status=story.status, kind=story.kind, anchor=story.anchor)
        for story in view.snapshot.current.stories
    }
    for op in artifact.stories:
        story = stories.get(op.id)
        properties = op.properties
        anchor = properties.get("anchor", story.anchor if story else None)
        stories[op.id] = StoryState(
            area_id=properties.get("area_id", story.area_id if story else ""),
            status=properties.get("status", story.status if story else "active"),
            kind=properties.get("kind", story.kind if story else "core"),
            anchor=view.resolve(anchor) if anchor else None,
        )
    # A delete names one stored relationship by its key and ends; another with the same ends stays.
    deleted = {(link.type, link.source, link.target, link.key) for link in artifact.links_delete}
    uuid_to_id = {node.uuid: node.spec_id for node in view.snapshot.plannable}
    links = [
        (link.type, link.source, link.target)
        for link in view.snapshot.current.links
        if (link.type, link.source, link.target, link.edge_key) not in deleted
    ]
    links.extend(
        (link.type, uuid_to_id.get(link.source, link.source), uuid_to_id.get(link.target, link.target))
        for link in artifact.links_create
    )
    return PlanState(
        plannable=frozenset(view.nodes),
        required=frozenset(spec_id for spec_id in view.nodes if view.required(spec_id)),
        areas=areas,
        stories=stories,
        tasks=task_state,
        links=tuple(links),
        not_buildable=frozenset(entry.spec_id for entries in tasks.not_buildable.values() for entry in entries),
        conventions=frozenset(spec_id for item in project.profile.conventions for spec_id in item.spec_ids),
    )
