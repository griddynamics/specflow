"""Deterministic placement of plannable nodes into work areas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from graph.core.ontology import Ontology
from graph.plan.identity import PROJECT_AREA_ID, area_id, chapter_slug, story_id
from graph.plan.models import (
    AreaPlan,
    ChapterRef,
    PartitionArtifact,
    Placement,
    PlanningPart,
    PlanOptions,
    SnapshotArtifact,
    SpecEdgeView,
    SpecNodeView,
    StoryPlan,
)
from graph.plan.names import StoryKind
from graph.plan.rules import PlanRules


@dataclass(frozen=True)
class _WalkCandidate:
    target: str
    edges: tuple[SpecEdgeView, ...]


_DEFAULT_PLANNER_MEMBERS = PlanOptions().planner_members
_ANCHOR_RULE = 1
_PROJECT_PLANNED_KINDS = frozenset({"setup", "closing"})


def partition(
    snapshot: SnapshotArtifact,
    rules: PlanRules,
    ontology: Ontology,
    *,
    planner_members: int = _DEFAULT_PLANNER_MEMBERS,
) -> PartitionArtifact:
    """Build the module and project areas, place every plannable node by the partition rules 1-7, then
    create the stories and the planning parts of at most ``planner_members`` members.

    The result is pure and deterministic.
    """

    del ontology
    nodes = {node.spec_id: node for node in snapshot.plannable}
    edges = tuple(snapshot.edges)
    internal_modules = _internal_modules(tuple(nodes.values()), rules)
    anchors = sorted(internal_modules)
    anchor_areas = _anchor_areas(anchors, snapshot)
    areas: dict[str, set[str]] = {PROJECT_AREA_ID: set()}
    for area_id_value in anchor_areas.values():
        areas[area_id_value] = set()

    placements: dict[str, Placement] = {}
    for anchor in anchors:
        areas[anchor_areas[anchor]].add(anchor)
        placements[anchor] = Placement(area_id=anchor_areas[anchor], rule=_ANCHOR_RULE)
    counts = {"cross_cutting": 0, "ambiguous_membership": 0, "unplaced": 0}
    _place_interfaces(
        nodes,
        edges,
        rules=rules,
        internal_modules=internal_modules,
        areas=areas,
        anchor_areas=anchor_areas,
        placements=placements,
        counts=counts,
    )
    _place_entities(
        nodes,
        edges,
        rules=rules,
        internal_modules=internal_modules,
        areas=areas,
        anchor_areas=anchor_areas,
        placements=placements,
        counts=counts,
    )
    _place_capabilities(
        nodes,
        edges,
        rules=rules,
        internal_modules=internal_modules,
        areas=areas,
        anchor_areas=anchor_areas,
        placements=placements,
        counts=counts,
    )
    _place_members(
        nodes,
        edges,
        rules=rules,
        internal_modules=internal_modules,
        areas=areas,
        anchor_areas=anchor_areas,
        placements=placements,
        counts=counts,
    )
    stories, home_story = _stories(
        nodes, edges=edges, rules=rules, snapshot=snapshot, areas=areas, placements=placements
    )

    module_areas = tuple(
        AreaPlan(
            area_id=anchor_areas[anchor],
            kind="module",
            anchor=anchor,
            status="active",
            members=tuple(sorted(areas[anchor_areas[anchor]])),
        )
        for anchor in anchors
    )
    retired_areas = tuple(
        AreaPlan(
            area_id=area.area_id,
            kind=area.kind,
            anchor=area.anchor,
            status="retired",
            members=(),
        )
        for area in snapshot.current.areas
        if area.anchor is not None and area.area_id not in anchor_areas.values()
    )
    project_area = AreaPlan(
        area_id=PROJECT_AREA_ID,
        kind="project",
        anchor=None,
        status="active",
        members=tuple(sorted(areas[PROJECT_AREA_ID])),
    )
    return PartitionArtifact(
        areas=(project_area, *module_areas, *retired_areas),
        stories=stories,
        placements=dict(sorted(placements.items())),
        home_story=dict(sorted(home_story.items())),
        parts=planning_parts(stories, planner_members),
        counts=dict(sorted(counts.items())),
    )


def planning_parts(stories: Sequence[StoryPlan], planner_members: int) -> tuple[PlanningPart, ...]:
    """The Tasks calls of every area: whole stories in story id order, packed up to ``planner_members``
    members; a larger story is split into chunks in ``spec_id`` order. Setup and closing stories are
    planned by the Project step and belong to no part."""

    by_area: dict[str, list[StoryPlan]] = {}
    for story in stories:
        if story.status == "active" and story.members and story.kind not in _PROJECT_PLANNED_KINDS:
            by_area.setdefault(story.area_id, []).append(story)
    parts: list[PlanningPart] = []
    for area, area_stories in sorted(by_area.items()):
        groups: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
        story_ids: list[str] = []
        members: list[str] = []
        for story in sorted(area_stories, key=lambda item: item.story_id):
            story_members = tuple(sorted(story.members))
            if len(story_members) > planner_members or len(members) + len(story_members) > planner_members:
                if story_ids:
                    groups.append((tuple(story_ids), tuple(members)))
                story_ids, members = [], []
            if len(story_members) > planner_members:
                groups.extend(
                    ((story.story_id,), story_members[start : start + planner_members])
                    for start in range(0, len(story_members), planner_members)
                )
                continue
            story_ids.append(story.story_id)
            members.extend(story_members)
        if story_ids:
            groups.append((tuple(story_ids), tuple(members)))
        parts.extend(
            PlanningPart(
                part_id=f"{area}#{number}",
                area_id=area,
                number=number,
                story_ids=group_story_ids,
                members=tuple(sorted(group_members)),
            )
            for number, (group_story_ids, group_members) in enumerate(groups, start=1)
        )
    return tuple(parts)


def _place_members(
    nodes: Mapping[str, SpecNodeView],
    edges: Sequence[SpecEdgeView],
    *,
    rules: PlanRules,
    internal_modules: frozenset[str],
    areas: dict[str, set[str]],
    anchor_areas: Mapping[str, str],
    placements: dict[str, Placement],
    counts: dict[str, int],
) -> None:
    grouping = rules.relation("grouping")
    reference = rules.relation("reference")
    for spec_id, node in sorted(nodes.items()):
        if spec_id in placements or rules.rule_of(node.label).role == "area_anchor":
            continue
        if (
            rules.rule_of(node.label).role == "shared"
            or _is_type(node, "interface", rules)
            or _is_type(node, "data_entity", rules)
        ):
            continue
        selection = _owner_walk(
            spec_id,
            nodes,
            edges,
            rules=rules,
            internal_modules=internal_modules,
            anchor_areas=anchor_areas,
            placements=placements,
        )
        if selection is None:
            selection = _grouped_area(
                spec_id,
                edges,
                grouping=grouping,
                placements=placements,
                areas=areas,
            )
        if selection is None:
            selection = _referenced_area(
                spec_id,
                edges,
                reference=reference,
                internal_modules=internal_modules,
                anchor_areas=anchor_areas,
            )
        if selection is None:
            _place_project(spec_id, areas, placements, rule=6)
            counts["unplaced"] += 1
            continue
        area_id_value, via, count_key = selection
        if count_key:
            counts[count_key] += 1
        areas[area_id_value].add(spec_id)
        placements[spec_id] = Placement(area_id=area_id_value, rule=6, via=via)


def _anchor_areas(anchors: Sequence[str], snapshot: SnapshotArtifact) -> dict[str, str]:
    persisted = {area.anchor: area.area_id for area in snapshot.current.areas if area.anchor is not None}
    assigned: dict[str, str] = {}
    taken = {PROJECT_AREA_ID, *(area.area_id for area in snapshot.current.areas)}
    for anchor in anchors:
        resolved = _resolve_anchor(anchor, snapshot.aliases)
        persisted_area = next(
            (
                area_id_value
                for persisted_anchor, area_id_value in persisted.items()
                if _resolve_anchor(persisted_anchor, snapshot.aliases) == resolved
            ),
            None,
        )
        assigned[anchor] = persisted_area or area_id(anchor, taken=tuple(taken))
        taken.add(assigned[anchor])
    return assigned


def _resolve_anchor(anchor: str, aliases: Mapping[str, str]) -> str:
    return aliases.get(anchor, anchor)


def _stories(
    nodes: Mapping[str, SpecNodeView],
    *,
    edges: Sequence[SpecEdgeView],
    rules: PlanRules,
    snapshot: SnapshotArtifact,
    areas: Mapping[str, set[str]],
    placements: Mapping[str, Placement],
) -> tuple[tuple[StoryPlan, ...], dict[str, str]]:
    anchors = {
        spec_id: node for spec_id, node in nodes.items() if rules.rule_of(node.label).story and spec_id in placements
    }
    target_stories = _anchor_stories(
        anchors,
        nodes=nodes,
        edges=edges,
        rules=rules,
        snapshot=snapshot,
        placements=placements,
    )
    anchor_ids = {story.anchor: story.story_id for story in target_stories if story.anchor is not None}
    # An area anchor is its area: it is a member of the area, but of no story.
    story_members = {spec_id: placement for spec_id, placement in placements.items() if placement.rule != _ANCHOR_RULE}
    home_story = _home_stories(story_members, edges=edges, rules=rules, anchors=anchors, anchor_ids=anchor_ids)
    _set_anchor_members(target_stories, home_story, anchor_ids)
    _add_core_stories(target_stories, home_story=home_story, snapshot=snapshot, areas=areas, anchors=anchors)
    _add_chapter_stories(
        target_stories, home_story=home_story, snapshot=snapshot, areas=areas, anchors=anchors, nodes=nodes
    )
    _add_project_stories(target_stories)
    _set_project_fallback_homes(target_stories, home_story, areas)
    target_stories = _with_ordinals(target_stories)
    retired_stories = _retired_stories(target_stories, snapshot)
    return (*target_stories, *retired_stories), home_story


def _anchor_stories(
    anchors: Mapping[str, SpecNodeView],
    *,
    nodes: Mapping[str, SpecNodeView],
    edges: Sequence[SpecEdgeView],
    rules: PlanRules,
    snapshot: SnapshotArtifact,
    placements: Mapping[str, Placement],
) -> list[StoryPlan]:
    containment = rules.relation("containment")
    target_stories: list[StoryPlan] = []
    for spec_id, node in sorted(anchors.items()):
        target_stories.append(
            StoryPlan(
                story_id=_story_id_for_anchor(spec_id, snapshot, placements[spec_id].area_id, target_stories),
                area_id=placements[spec_id].area_id,
                kind=_story_kind(node, rules),
                anchor=spec_id,
                title=node.name,
                release=_release(node, nodes, edges, containment, rules),
                members=(),
                ordinal=1,
                status="active",
            )
        )

    return target_stories


def _set_anchor_members(
    target_stories: list[StoryPlan], home_story: Mapping[str, str], anchor_ids: Mapping[str, str]
) -> None:
    members: dict[str, set[str]] = {anchor: set() for anchor in anchor_ids}
    for spec_id, story in home_story.items():
        if story in anchor_ids.values():
            members[next(anchor for anchor, story_id in anchor_ids.items() if story_id == story)].add(spec_id)
    for index, anchor_story in enumerate(target_stories):
        if anchor_story.anchor is not None and anchor_story.story_id in anchor_ids.values():
            target_stories[index] = anchor_story.model_copy(
                update={"members": tuple(sorted(members[anchor_story.anchor]))}
            )


def _add_core_stories(
    target_stories: list[StoryPlan],
    *,
    home_story: dict[str, str],
    snapshot: SnapshotArtifact,
    areas: Mapping[str, set[str]],
    anchors: Mapping[str, SpecNodeView],
) -> None:
    target_ids = {story.story_id for story in target_stories}
    for area_id_value, members in sorted(
        {
            area_id_value: {
                member for member in members if member not in anchors and home_story.get(member) == area_id_value
            }
            for area_id_value, members in areas.items()
            if area_id_value != PROJECT_AREA_ID
        }.items()
    ):
        if not members:
            continue
        existing = next(
            (
                story.story_id
                for story in snapshot.current.stories
                if story.area_id == area_id_value and story.kind == "core"
            ),
            None,
        )
        target_stories.append(
            StoryPlan(
                story_id=existing or story_id(area_id_value, "CORE"),
                area_id=area_id_value,
                kind="core",
                anchor=None,
                title="Core",
                release=None,
                members=tuple(sorted(members)),
                ordinal=1,
                status="active",
            )
        )
        target_ids.add(target_stories[-1].story_id)
        for member in members:
            if home_story[member] == area_id_value:
                home_story[member] = target_stories[-1].story_id


def _add_chapter_stories(
    target_stories: list[StoryPlan],
    *,
    home_story: dict[str, str],
    snapshot: SnapshotArtifact,
    areas: Mapping[str, set[str]],
    anchors: Mapping[str, SpecNodeView],
    nodes: Mapping[str, SpecNodeView],
) -> None:
    target_ids = {story.story_id for story in target_stories}

    chapter_members: dict[str, set[str]] = {}
    chapter_refs: dict[str, ChapterRef] = {}
    for spec_id in sorted(areas[PROJECT_AREA_ID]):
        node = nodes[spec_id]
        if spec_id in anchors or node.chapter is None:
            continue
        chapter = node.chapter
        chapter_key = chapter_slug(chapter.document, chapter.heading)
        chapter_members.setdefault(chapter_key, set()).add(spec_id)
        chapter_refs.setdefault(chapter_key, chapter)
    for chapter_key, members in sorted(chapter_members.items()):
        chapter = chapter_refs[chapter_key]
        existing = next(
            (
                story.story_id
                for story in snapshot.current.stories
                if story.area_id == PROJECT_AREA_ID and story.kind == "chapter" and story.chapter == chapter
            ),
            None,
        )
        target_stories.append(
            StoryPlan(
                story_id=existing or story_id(PROJECT_AREA_ID, chapter_key[:56], taken=tuple(target_ids)),
                area_id=PROJECT_AREA_ID,
                kind="chapter",
                anchor=None,
                chapter=chapter,
                title=chapter.heading,
                release=None,
                members=tuple(sorted(members)),
                ordinal=1,
                status="active",
            )
        )
        target_ids.add(target_stories[-1].story_id)
        chapter_story = target_stories[-1]
        for member in members:
            home_story[member] = chapter_story.story_id


def _add_project_stories(target_stories: list[StoryPlan]) -> None:
    target_stories.extend(
        StoryPlan(
            story_id=story_id(PROJECT_AREA_ID, kind.upper()),
            area_id=PROJECT_AREA_ID,
            kind=kind,
            anchor=None,
            title=kind.title(),
            release=None,
            ordinal=1,
            status="active",
        )
        for kind in ("setup", "closing")
    )


def _set_project_fallback_homes(
    target_stories: Sequence[StoryPlan], home_story: dict[str, str], areas: Mapping[str, set[str]]
) -> None:
    setup_story_id = next(story.story_id for story in target_stories if story.kind == "setup")
    for member in sorted(areas[PROJECT_AREA_ID]):
        if home_story[member] == PROJECT_AREA_ID:
            home_story[member] = setup_story_id


def _with_ordinals(stories: Sequence[StoryPlan]) -> list[StoryPlan]:
    target_stories = sorted(stories, key=lambda story: story.story_id)
    ordinals: dict[str, int] = {}
    for story in target_stories:
        ordinals[story.area_id] = ordinals.get(story.area_id, 0) + 1
        target_stories[target_stories.index(story)] = story.model_copy(update={"ordinal": ordinals[story.area_id]})

    return target_stories


def _retired_stories(target_stories: Sequence[StoryPlan], snapshot: SnapshotArtifact) -> tuple[StoryPlan, ...]:
    active_story_ids = {story.story_id for story in target_stories}
    persisted_stories = sorted(snapshot.current.stories, key=lambda story: story.story_id)
    active_anchor_stories = _active_anchor_stories(target_stories)
    active_chapter_stories = _active_chapter_stories(target_stories)
    return tuple(
        StoryPlan(
            story_id=story.story_id,
            area_id=story.area_id,
            kind=story.kind,
            anchor=story.anchor,
            chapter=story.chapter,
            title=story.title,
            release=story.release,
            members=(),
            ordinal=1,
            status="retired",
        )
        for story in persisted_stories
        if story.story_id not in active_story_ids
        and (
            story.anchor is None
            or not any(
                active_story.anchor == _resolve_anchor(story.anchor, snapshot.aliases)
                and active_story.area_id == story.area_id
                for active_story in active_anchor_stories
            )
        )
        and (
            story.chapter is None
            or not any(
                active_story.chapter == story.chapter
                and active_story.area_id == story.area_id
                and active_story.kind == "chapter"
                for active_story in active_chapter_stories
            )
        )
    )


def _active_anchor_stories(stories: Sequence[StoryPlan]) -> tuple[StoryPlan, ...]:
    return tuple(story for story in stories if story.anchor is not None)


def _active_chapter_stories(stories: Sequence[StoryPlan]) -> tuple[StoryPlan, ...]:
    return tuple(story for story in stories if story.kind == "chapter")


def _story_id_for_anchor(
    anchor: str,
    snapshot: SnapshotArtifact,
    area_id: str,
    target_stories: Sequence[StoryPlan],
) -> str:
    existing = next(
        (
            story.story_id
            for story in snapshot.current.stories
            if story.anchor is not None and _resolve_anchor(story.anchor, snapshot.aliases) == anchor
        ),
        None,
    )
    generated = story_id(
        area_id,
        anchor.split("::", 1)[-1],
        taken=(
            *(story.story_id for story in snapshot.current.stories),
            *(story.story_id for story in target_stories),
        ),
    )
    return existing if existing is not None else generated


def _story_kind(node: SpecNodeView, rules: PlanRules) -> StoryKind:
    if _is_type(node, "feature", rules):
        return "feature"
    if _is_type(node, "interface", rules):
        return "interface"
    return "entity"


def _home_stories(
    placements: Mapping[str, Placement],
    *,
    edges: Sequence[SpecEdgeView],
    rules: PlanRules,
    anchors: Mapping[str, SpecNodeView],
    anchor_ids: Mapping[str, str],
) -> dict[str, str]:
    containment = rules.relation("containment")
    home: dict[str, str] = {}
    for spec_id in sorted(placements):
        area = placements[spec_id].area_id
        if spec_id in anchors:
            home[spec_id] = anchor_ids[spec_id]
            continue
        frontier = {spec_id}
        visited = {spec_id}
        found: str | None = None
        for _ in range(rules.hop_limit):
            outgoing = tuple(
                edge
                for edge in edges
                if edge.relation == containment and edge.source in frontier and edge.target not in visited
            )
            candidates = tuple(sorted({edge.target for edge in outgoing if edge.target in anchor_ids}))
            same_area = tuple(anchor for anchor in candidates if placements[anchor].area_id == area)
            if same_area:
                found = anchor_ids[same_area[0]]
                break
            frontier = {edge.target for edge in outgoing}
            visited.update(frontier)
        home[spec_id] = found or area
    return home


def _release(
    node: SpecNodeView,
    nodes: Mapping[str, SpecNodeView],
    edges: Sequence[SpecEdgeView],
    containment: str,
    rules: PlanRules,
) -> str | None:
    release = node.attributes.get(rules.release)
    if release is not None:
        return str(release)
    for edge in edges:
        if edge.relation != containment or edge.source != node.spec_id or edge.target not in nodes:
            continue
        inherited = _release(nodes[edge.target], nodes, edges, containment, rules)
        if inherited is not None:
            return inherited
    return None


def _internal_modules(nodes: Sequence[SpecNodeView], rules: PlanRules) -> frozenset[str]:
    return frozenset(
        node.spec_id
        for node in nodes
        if rules.rule_of(node.label).role == "area_anchor"
        and all(
            node.attributes.get(attribute) not in values
            for attribute, values in rules.rule_of(node.label).exclude.items()
        )
    )


def _place_interfaces(
    nodes: Mapping[str, SpecNodeView],
    edges: Sequence[SpecEdgeView],
    *,
    rules: PlanRules,
    internal_modules: frozenset[str],
    areas: dict[str, set[str]],
    anchor_areas: Mapping[str, str],
    placements: dict[str, Placement],
    counts: dict[str, int],
) -> None:
    provision = rules.relation("provision")
    consumption = rules.relation("consumption")
    integration = rules.relation("integration")
    for spec_id, node in sorted(nodes.items()):
        if rules.rule_of(node.label).role != "member" or not _is_type(node, "interface", rules):
            continue
        provider = next(
            ((edge.target, edge.edge_key) for edge in edges if edge.relation == provision and edge.source == spec_id),
            None,
        )
        consumers = tuple(
            (edge.target, edge.edge_key)
            for edge in edges
            if edge.relation == consumption and edge.source == spec_id and edge.target in internal_modules
        )
        integrators = (
            ()
            if provider is None
            else tuple(
                (edge.target, edge.edge_key)
                for edge in edges
                if edge.relation == integration
                and edge.target in internal_modules
                and (edge.source == provider[0] or edge.target == provider[0])
            )
        )
        selection = _single_provider(provider, internal_modules, consumers, integrators)
        if selection is None:
            counts["cross_cutting"] += 1
            _place_project(spec_id, areas, placements, rule=3)
            continue
        _assign(
            spec_id,
            selection,
            internal_modules=internal_modules,
            areas=areas,
            anchor_areas=anchor_areas,
            placements=placements,
            rule=3,
        )


def _place_entities(
    nodes: Mapping[str, SpecNodeView],
    edges: Sequence[SpecEdgeView],
    *,
    rules: PlanRules,
    internal_modules: frozenset[str],
    areas: dict[str, set[str]],
    anchor_areas: Mapping[str, str],
    placements: dict[str, Placement],
    counts: dict[str, int],
) -> None:
    storage = rules.relation("storage")
    for spec_id, node in sorted(nodes.items()):
        if not _is_type(node, "data_entity", rules):
            continue
        stores = tuple(
            (edge.target, edge.edge_key)
            for edge in edges
            if edge.relation == storage and edge.source == spec_id and edge.target in internal_modules
        )
        selection = stores[0] if len(stores) == 1 else None
        if selection is None:
            counts["cross_cutting"] += 1
            _place_project(spec_id, areas, placements, rule=4)
            continue
        _assign(
            spec_id,
            selection,
            internal_modules=internal_modules,
            areas=areas,
            anchor_areas=anchor_areas,
            placements=placements,
            rule=4,
        )


def _place_capabilities(
    nodes: Mapping[str, SpecNodeView],
    edges: Sequence[SpecEdgeView],
    *,
    rules: PlanRules,
    internal_modules: frozenset[str],
    areas: dict[str, set[str]],
    anchor_areas: Mapping[str, str],
    placements: dict[str, Placement],
    counts: dict[str, int],
) -> None:
    realisation = rules.relation("realisation")
    for spec_id, node in sorted(nodes.items()):
        if not _is_type(node, "capability", rules):
            continue
        modules = tuple(
            (edge.target, edge.edge_key)
            for edge in edges
            if edge.relation == realisation and edge.source == spec_id and edge.target in internal_modules
        )
        selection = modules[0] if len(modules) == 1 else None
        if selection is None:
            counts["unplaced" if not modules else "cross_cutting"] += 1
            _place_project(spec_id, areas, placements, rule=5)
            continue
        _assign(
            spec_id,
            selection,
            internal_modules=internal_modules,
            areas=areas,
            anchor_areas=anchor_areas,
            placements=placements,
            rule=5,
        )


def _is_type(node: SpecNodeView, type_key: str, rules: PlanRules) -> bool:
    return node.label in rules.labels_of(type_key)


def _single_provider(
    provider: tuple[str, str] | None,
    internal_modules: frozenset[str],
    consumers: Sequence[tuple[str, str]],
    integrators: Sequence[tuple[str, str]],
) -> tuple[str, str] | None:
    if provider is not None and provider[0] in internal_modules:
        return provider
    if len(consumers) == 1:
        return consumers[0]
    if not consumers and len(integrators) == 1:
        return integrators[0]
    return None


def _assign(
    spec_id: str,
    selection: tuple[str, str] | None,
    *,
    internal_modules: frozenset[str],
    areas: dict[str, set[str]],
    anchor_areas: Mapping[str, str],
    placements: dict[str, Placement],
    rule: int,
) -> None:
    selected_area = PROJECT_AREA_ID
    via: tuple[str, ...] = ()
    if selection is not None and selection[0] in internal_modules:
        selected_area = anchor_areas[selection[0]]
        via = (selection[1],)
    areas[selected_area].add(spec_id)
    placements[spec_id] = Placement(area_id=selected_area, rule=rule, via=via)


def _place_project(
    spec_id: str,
    areas: dict[str, set[str]],
    placements: dict[str, Placement],
    *,
    rule: int,
) -> None:
    areas[PROJECT_AREA_ID].add(spec_id)
    placements[spec_id] = Placement(area_id=PROJECT_AREA_ID, rule=rule)


def _owner_walk(
    spec_id: str,
    nodes: Mapping[str, SpecNodeView],
    edges: Sequence[SpecEdgeView],
    *,
    rules: PlanRules,
    internal_modules: frozenset[str],
    anchor_areas: Mapping[str, str],
    placements: Mapping[str, Placement],
) -> tuple[str, tuple[str, ...], str | None] | None:
    containment = rules.relation("containment")
    frontier = {spec_id}
    visited = {spec_id}
    via: tuple[str, ...] = ()
    for _ in range(rules.hop_limit):
        outgoing = tuple(
            edge
            for edge in edges
            if edge.relation == containment and edge.source in frontier and edge.target not in visited
        )
        outgoing_by_target: dict[str, list[SpecEdgeView]] = {}
        for edge in outgoing:
            outgoing_by_target.setdefault(edge.target, []).append(edge)
        anchors = tuple(
            _WalkCandidate(target, tuple(outgoing_by_target[target])) for target in sorted(outgoing_by_target)
        )
        internal = tuple(item for item in anchors if item.target in internal_modules)
        if internal:
            candidate = internal[0]
            count_key = "ambiguous_membership" if len(internal) > 1 else None
            edge_keys = tuple(sorted(edge.edge_key for edge in candidate.edges))
            return anchor_areas[candidate.target], (*via, *edge_keys), count_key
        hubs = tuple(item for item in anchors if item.target in placements and item.target in nodes)
        if hubs:
            candidate = hubs[0]
            hub_areas = {placements[item.target].area_id for item in hubs}
            count_key = "ambiguous_membership" if len(hub_areas) > 1 else None
            hub_placement = placements[candidate.target]
            edge_keys = tuple(sorted(edge.edge_key for edge in candidate.edges))
            return hub_placement.area_id, (*via, *edge_keys, *hub_placement.via), count_key
        external = tuple(item for item in anchors if item.target not in nodes)
        if external:
            candidate = external[0]
            external_area = _external_owner_area(
                candidate.target,
                edges,
                internal_modules=internal_modules,
                anchor_areas=anchor_areas,
                roles=rules.relations,
            )
            edge_keys = tuple(sorted(edge.edge_key for edge in candidate.edges))
            if external_area is not None:
                return external_area[0], (*via, *edge_keys, *external_area[1]), None
            return PROJECT_AREA_ID, (*via, *edge_keys), "unplaced"
        frontier = {edge.target for edge in outgoing}
        visited.update(frontier)
        via = (*via, *tuple(sorted(edge.edge_key for edge in outgoing)))
    return None


def _external_owner_area(
    owner: str,
    edges: Sequence[SpecEdgeView],
    *,
    internal_modules: frozenset[str],
    anchor_areas: Mapping[str, str],
    roles: Mapping[str, str],
) -> tuple[str, tuple[str, ...]] | None:
    provision, consumption, integration = roles["provision"], roles["consumption"], roles["integration"]
    interfaces = {edge.source for edge in edges if edge.relation == provision and edge.target == owner}
    consumers = tuple(
        (edge.target, edge.edge_key)
        for edge in edges
        if edge.relation == consumption and edge.source in interfaces and edge.target in internal_modules
    )
    if len(consumers) == 1:
        return anchor_areas[consumers[0][0]], (consumers[0][1],)
    integrators = tuple(
        (edge.target, edge.edge_key)
        for edge in edges
        if edge.relation == integration and edge.target in internal_modules and edge.source == owner
    )
    if not consumers and len(integrators) == 1:
        return anchor_areas[integrators[0][0]], (integrators[0][1],)
    return None


def _grouped_area(
    spec_id: str,
    edges: Sequence[SpecEdgeView],
    *,
    grouping: str,
    placements: Mapping[str, Placement],
    areas: Mapping[str, set[str]],
) -> tuple[str, tuple[str, ...], str | None] | None:
    groups = tuple(
        (edge.target, edge.edge_key)
        for edge in edges
        if edge.relation == grouping and edge.source == spec_id and edge.target in placements
    )
    for capability, edge_key in sorted(groups):
        placement = placements[capability]
        if placement.area_id != PROJECT_AREA_ID or capability in areas[PROJECT_AREA_ID]:
            return placement.area_id, (edge_key, *placement.via), None
    return None


def _referenced_area(
    spec_id: str,
    edges: Sequence[SpecEdgeView],
    *,
    reference: str,
    internal_modules: frozenset[str],
    anchor_areas: Mapping[str, str],
) -> tuple[str, tuple[str, ...], str | None] | None:
    references = tuple(
        (edge.target if edge.source == spec_id else edge.source, edge.edge_key)
        for edge in edges
        if edge.relation == reference and spec_id in (edge.source, edge.target)
    )
    modules = tuple(sorted({target for target, _ in references if target in internal_modules}))
    if len(modules) == 1:
        edge_keys = tuple(sorted(edge_key for target, edge_key in references if target == modules[0]))
        return anchor_areas[modules[0]], edge_keys, None
    if len(modules) > 1:
        return PROJECT_AREA_ID, (), "cross_cutting"
    return None
