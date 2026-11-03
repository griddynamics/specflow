"""A read-only view over the Snapshot and Partition artifacts that the planning steps share.

It answers, from the spec graph and the partition only, which area a node is in, what an area provides,
consumes and integrates with, a node's meaning revision, release and open questions, and the domain
edges that touch a set of nodes. Relation names come from the plan rules' roles.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import cast, get_args

from graph.plan.changes import resolve_spec_id
from graph.plan.identity import PROJECT_AREA_ID
from graph.plan.models import AreaPlan, PartitionArtifact, SnapshotArtifact, SpecEdgeView, SpecNodeView, StoryPlan
from graph.plan.names import Phase
from graph.plan.rules import PlanRules


@dataclass(frozen=True)
class PlanView:
    """Snapshot and Partition seen together; every list it returns is sorted."""

    snapshot: SnapshotArtifact
    partition: PartitionArtifact
    rules: PlanRules

    @cached_property
    def nodes(self) -> Mapping[str, SpecNodeView]:
        """Plannable nodes by ``spec_id``."""

        return {node.spec_id: node for node in self.snapshot.plannable}

    @cached_property
    def context(self) -> Mapping[str, SpecNodeView]:
        """Context nodes (external modules, open questions) by ``spec_id``."""

        return {node.spec_id: node for node in self.snapshot.context}

    @cached_property
    def active_areas(self) -> tuple[AreaPlan, ...]:
        return tuple(area for area in self.partition.areas if area.status == "active")

    @cached_property
    def stories(self) -> Mapping[str, StoryPlan]:
        return {story.story_id: story for story in self.partition.stories if story.status == "active"}

    def area_of(self, spec_id: str) -> str | None:
        """The area a plannable node is placed in; None for a context or unknown node."""

        placement = self.partition.placements.get(spec_id)
        return placement.area_id if placement is not None else None

    def area(self, area_id: str) -> AreaPlan:
        return next(area for area in self.partition.areas if area.area_id == area_id)

    def area_stories(self, area_id: str) -> tuple[StoryPlan, ...]:
        return tuple(story for story in self.stories.values() if story.area_id == area_id)

    def revision(self, spec_id: str) -> str:
        """The meaning revision of a node (the consumed build when none is known)."""

        return self.snapshot.revisions.nodes.get(spec_id, self.snapshot.build_id)

    def resolve(self, spec_id: str) -> str:
        """A merged or rekeyed id resolved to its survivor."""

        return resolve_spec_id(spec_id, self.snapshot.aliases)

    def newer(self, revision: str, spec_build_id: str | None) -> bool:
        """Whether a meaning revision is newer than a task's ``spec_build_id``: not one of the builds the plan
        consumed up to it. Build ids are ordered by the consumed chain only, never by value."""

        if spec_build_id is None:
            return True
        consumed = self.snapshot.revisions.consumed
        if spec_build_id not in consumed:
            return revision != spec_build_id
        return revision not in consumed[: consumed.index(spec_build_id) + 1]

    def spec_change(self, implements: Iterable[str], spec_build_id: str | None) -> str | None:
        """Why a task's spec changed, or None: ``unresolved`` when an implemented node no longer resolves,
        else the latest meaning-change class of a node whose revision is newer than ``spec_build_id``."""

        cause: str | None = None
        for spec_id in implements:
            survivor = self.resolve(spec_id)
            if survivor not in self.nodes:
                return "unresolved"
            if self.newer(self.revision(survivor), spec_build_id):
                meaning = self.snapshot.change_set.meaning
                cause = meaning.get(survivor) or meaning.get(spec_id) or "changed_meaning"
        return cause

    def anchors(self, spec_id: str, kind: str) -> bool:
        """Whether a node's type anchors stories of ``kind``: ``interface``, ``entity`` or ``feature``."""

        node = self.nodes.get(spec_id) or self.context.get(spec_id)
        return node is not None and self.rules.story_kind(node.label) == kind

    def phase_of(self, spec_id: str) -> Phase:
        """The default phase of a member's type (``logic`` when its type names none)."""

        phase = self.rules.rule_of(self.nodes[spec_id].label).phase
        return cast(Phase, phase) if phase in get_args(Phase) else "logic"

    def required(self, spec_id: str) -> bool:
        return self.rules.rule_of(self.nodes[spec_id].label).required

    def edges_of(self, relation_role: str) -> tuple[SpecEdgeView, ...]:
        relation = self.rules.relation(relation_role)
        return tuple(edge for edge in self.snapshot.edges if edge.relation == relation)

    def touching(self, spec_ids: Iterable[str]) -> tuple[SpecEdgeView, ...]:
        """The domain edges with an end in ``spec_ids``, in key order."""

        ids = frozenset(spec_ids)
        return tuple(
            sorted(
                (edge for edge in self.snapshot.edges if edge.source in ids or edge.target in ids),
                key=lambda edge: edge.edge_key,
            )
        )

    def provided(self, area_id: str) -> tuple[str, ...]:
        """The spec Interfaces placed in an area (rule 3 places an interface with its provider)."""

        area = self.area(area_id)
        return tuple(spec_id for spec_id in area.members if self.anchors(spec_id, "interface"))

    def provider_area(self, interface_id: str) -> str | None:
        return self.area_of(interface_id) if self.anchors(interface_id, "interface") else None

    def consumed(self, area_id: str) -> tuple[tuple[str, str], ...]:
        """``(interface, provider area)`` of every interface a module of the area consumes from another area."""

        found = {
            (edge.source, provider)
            for edge in self.edges_of("consumption")
            if self.area_of(edge.target) == area_id
            and (provider := self.provider_area(edge.source)) is not None
            and provider != area_id
        }
        return tuple(sorted(found))

    def consumers(self, area_id: str) -> tuple[str, ...]:
        """The other areas whose modules consume an interface this area provides."""

        provided = frozenset(self.provided(area_id))
        found: set[str] = set()
        for edge in self.edges_of("consumption"):
            consumer = self.area_of(edge.target)
            if edge.source in provided and consumer is not None and consumer != area_id:
                found.add(consumer)
        return tuple(sorted(found))

    def integrations(self, area_id: str) -> tuple[str, ...]:
        """The other areas linked to this one by an integration edge, in either direction."""

        found: set[str] = set()
        for edge in self.edges_of("integration"):
            ends = (self.area_of(edge.source), self.area_of(edge.target))
            if area_id in ends:
                found.update(end for end in ends if end is not None and end != area_id)
        return tuple(sorted(found))

    def related_areas(self, area_id: str) -> tuple[str, ...]:
        """Areas this area provides to, consumes from or integrates with."""

        consumed = {provider for _, provider in self.consumed(area_id)}
        return tuple(sorted(consumed | set(self.consumers(area_id)) | set(self.integrations(area_id))))

    def open_questions(self, spec_id: str) -> tuple[str, ...]:
        """Context nodes of role ``context`` linked to a node by the reference relation."""

        reference = self.rules.relation("reference")
        found = {
            other
            for edge in self.snapshot.edges
            if edge.relation == reference and spec_id in (edge.source, edge.target)
            for other in (edge.source, edge.target)
            if other != spec_id and other in self.context and self._is_context_role(other)
        }
        return tuple(sorted(found))

    def release_of(self, spec_id: str) -> str | None:
        """A node's release: its own value, else the nearest owner's by containment."""

        containment = self.rules.relation("containment")
        seen = {spec_id}
        current = spec_id
        while (node := self.nodes.get(current)) is not None:
            value = node.attributes.get(self.rules.release)
            if value is not None:
                return str(value)
            owner = next(
                (
                    edge.target
                    for edge in sorted(self.snapshot.edges, key=lambda item: item.edge_key)
                    if edge.relation == containment and edge.source == current and edge.target not in seen
                ),
                None,
            )
            if owner is None:
                return None
            seen.add(owner)
            current = owner
        return None

    def releases(self, spec_ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted({release for spec_id in spec_ids if (release := self.release_of(spec_id)) is not None}))

    def has_open_questions(self, spec_ids: Iterable[str]) -> bool:
        return any(self.open_questions(spec_id) for spec_id in spec_ids)

    def statement(self, spec_id: str) -> str:
        node = self.nodes.get(spec_id) or self.context[spec_id]
        return " ".join(str(item.get("text", "")).strip() for item in node.statements if item.get("text"))

    def quote(self, spec_id: str) -> str:
        node = self.nodes.get(spec_id) or self.context[spec_id]
        return " ".join(item.quote.strip() for item in node.provenance if item.quote)

    def internal_modules(self, area_id: str | None = None) -> tuple[str, ...]:
        """Area anchors (internal modules), optionally only the one of an area."""

        anchors = (area.anchor for area in self.active_areas if area.anchor is not None)
        return tuple(sorted(anchor for anchor in anchors if area_id is None or self.area_of(anchor) == area_id))

    def project_area(self) -> AreaPlan:
        return self.area(PROJECT_AREA_ID)

    def _is_context_role(self, spec_id: str) -> bool:
        return self.rules.rule_of(self.context[spec_id].label).role == "context"
