"""Directed trace over the spec graph by the ontology's impact roles (trace.md "Walk", "Library").

``GraphReader.trace`` checks the arguments and hands the walk to ``TraceWalk``. Roles come from the compiled
ontology (``Ontology.impact_role``); no relation name is written here (invariant I4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict

from graph.core.ontology import Ontology
from graph.core.ontology.compiled import COUPLED, RELATED, USED_BY, USES
from graph.core.queries import READ_NODE_UUIDS_BY_SPEC_ID, READ_NODES_BY_UUID, READ_TRACE_HOP
from graph.core.store import Executor

Direction = Literal["based_on", "affects"]
Basis = Literal["trusted", "any"]
Step = Literal["forward", "backward", "scope"]

BASED_ON: Final = "based_on"
AFFECTS: Final = "affects"
DIRECTIONS: Final = (BASED_ON, AFFECTS)
TRUSTED: Final = "trusted"
BASES: Final = (TRUSTED, "any")
# Edge ``basis`` values of the data model; an edge without one reads as stated.
STATED: Final = "stated"
IMPLIED: Final = "implied"
INFERRED: Final = "inferred"
TRUSTED_BASES: Final = frozenset({STATED, IMPLIED})
MAX_SPEC_IDS: Final = 50
MAX_TRACE_LIMIT: Final = 500
HUB_ROLES: Final = frozenset({USES, USED_BY})
"""The only roles walked from a spine root (rule 6)."""
_RETIRED: Final = "retired"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class TraceOwner(_Frozen):
    """An owner of a seed up to the first spine root (the trace's context)."""

    spec_id: str
    label: str
    name: str


class TraceNode(_Frozen):
    """A node the trace reached, with the edge from its ``parent`` that reached it first."""

    spec_id: str
    label: str
    name: str
    hops: int
    parent: str
    relation: str
    step: Step
    role: str
    basis: str
    confidence: float | None = None
    rule: str | None = None
    weak: bool = False
    via_owner: bool = False
    spine_root: bool = False


class TraceResult(_Frozen):
    """Seeds (``sources``), ids that resolve to no node (``unknown``), the seeds' owners (``context``) and the
    reached ``nodes`` ordered by hop then ``spec_id``; ``truncated`` when more were reachable than returned."""

    direction: Direction
    sources: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    context: tuple[TraceOwner, ...] = ()
    nodes: tuple[TraceNode, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Visit:
    """A node on the walk: a start (hop 0) or a reached node, with what its path carries on."""

    uuid: str
    spec_id: str
    label: str
    via_owner: bool
    weak: bool = False


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One edge from a frontier node to an unvisited node; the lowest ``key`` is its node's parent."""

    node: TraceNode
    uuid: str

    @property
    def key(self) -> tuple[bool, str, str, str]:
        return (self.node.via_owner, self.node.parent, self.node.relation, self.node.step)


@dataclass(frozen=True, slots=True)
class TraceWalk:
    """One trace of one project; the arguments are checked by ``GraphReader.trace``."""

    executor: Executor
    ontology: Ontology
    project_id: str
    labels: tuple[str, ...]
    """The spec labels (build-owned): the only ends a trace reads."""
    domain_relations: tuple[str, ...]
    relations: tuple[str, ...]
    """The relations steps 4 and 5 may walk."""
    direction: Direction
    depth: int
    any_basis: bool
    include_owner: bool
    limit: int

    async def run(self, spec_ids: Sequence[str]) -> TraceResult:
        seeds, unknown = await self._seeds(spec_ids)
        context = await self._context(seeds)
        roots = self.ontology.spine.roots
        owner_starts = [
            Visit(owner.uuid, owner.spec_id, owner.label, via_owner=True)
            for owner, _ in context
            if self.include_owner and owner.label in roots
        ]
        visited = {visit.uuid for visit in (*seeds, *owner_starts)}
        frontier = [*seeds, *owner_starts]
        found: list[TraceNode] = []
        for hop in range(1, self.depth + 1):
            if not frontier or len(found) > self.limit:
                break
            reached = await self._hop(frontier, hop, visited)
            found.extend(node for _, node in reached)
            frontier = [Visit(uuid, node.spec_id, node.label, node.via_owner, node.weak) for uuid, node in reached]
        return TraceResult(
            direction=self.direction,
            sources=tuple(seed.spec_id for seed in seeds),
            unknown=unknown,
            context=tuple(TraceOwner(spec_id=o.spec_id, label=o.label, name=name) for o, name in context),
            nodes=tuple(found[: self.limit]),
            truncated=len(found) > self.limit,
        )

    # Steps 1 to 3: seeds, context, starts

    async def _seeds(self, spec_ids: Sequence[str]) -> tuple[list[Visit], tuple[str, ...]]:
        """The nodes ``spec_ids`` name (own id or alias), by ``spec_id``, and the ids naming none."""

        wanted = list(dict.fromkeys(spec_ids))
        rows = await self.executor.read(
            READ_NODE_UUIDS_BY_SPEC_ID, self.project_id, spec_ids=wanted, labels=list(self.labels)
        )
        uuids = [row["uuid"] for row in rows]
        nodes = (
            await self.executor.read(READ_NODES_BY_UUID, self.project_id, uuids=uuids, retired=_RETIRED)
            if uuids
            else []
        )
        seeds: list[Visit] = []
        named: set[str] = set()
        for row in sorted(nodes, key=lambda item: str(item["props"]["spec_id"])):
            label = self._label(row["labels"])
            if label is None:
                continue
            props = row["props"]
            seeds.append(Visit(props["uuid"], props["spec_id"], label, via_owner=False))
            named |= {props["spec_id"], *(props.get("spec_id_aliases") or ())}
        return seeds, tuple(spec_id for spec_id in wanted if spec_id not in named)

    async def _context(self, seeds: Sequence[Visit]) -> list[tuple[Visit, str]]:
        """The owners of the seeds up to the first spine root, one read of the owner pairs per level; each
        with its name, by level then ``spec_id``. Seeds are never context."""

        roots = self.ontology.spine.roots
        relations = sorted(
            name for name in self.domain_relations if any(pair.owner for pair in self.ontology.pairs(name))
        )
        levels = max((path.max_hops for path in self.ontology.spine.paths), default=1)
        seen = {seed.uuid for seed in seeds}
        frontier = {seed.uuid: seed for seed in seeds if seed.label not in roots}
        context: list[tuple[Visit, str]] = []
        for _ in range(levels):
            if not frontier or not relations:
                break
            rows = await self._read_hop(list(frontier), relations)
            found: dict[str, tuple[Visit, str]] = {}
            for row in rows:
                start = frontier[row["frontier"]]
                label = self._label(row["labels"])
                if not row["outgoing"] or label is None or row["uuid"] in seen or not self._trusted(row["basis"]):
                    continue
                pair = self.ontology.pair_rule(row["relation"], start.label, label)
                if pair is not None and pair.owner:
                    found[row["uuid"]] = (Visit(row["uuid"], row["spec_id"], label, via_owner=True), row["name"] or "")
            level = sorted(found.values(), key=lambda item: item[0].spec_id)
            context.extend(level)
            seen |= set(found)
            frontier = {owner.uuid: owner for owner, _ in level if owner.label not in roots}
        return context

    # Steps 4 to 7: one hop

    async def _hop(self, frontier: Sequence[Visit], hop: int, visited: set[str]) -> list[tuple[str, TraceNode]]:
        """The nodes first reached at ``hop`` from ``frontier``, by ``spec_id``; marks them visited."""

        by_uuid = {visit.uuid: visit for visit in frontier}
        rows = await self._frontier_rows(frontier)
        best: dict[str, _Candidate] = {}
        for row in rows:
            candidate = self._candidate(by_uuid[row["frontier"]], row, hop, visited)
            if candidate is not None and (candidate.uuid not in best or candidate.key < best[candidate.uuid].key):
                best[candidate.uuid] = candidate
        reached = sorted(((uuid, item.node) for uuid, item in best.items()), key=lambda pair: pair[1].spec_id)
        visited |= set(best)
        return reached

    async def _frontier_rows(self, frontier: Sequence[Visit]) -> list[Mapping[str, Any]]:
        """The incident edges of ``frontier``: a spine root's of the hub relations only (rule 6)."""

        roots = self.ontology.spine.roots
        hub_relations = [name for name in self.relations if self._has_hub_role(name)]
        rows: list[Mapping[str, Any]] = []
        for at_root, relations in ((True, hub_relations), (False, list(self.relations))):
            uuids = [visit.uuid for visit in frontier if (visit.label in roots) == at_root]
            if uuids and relations:
                rows.extend(await self._read_hop(uuids, relations))
        return rows

    def _candidate(self, start: Visit, row: Mapping[str, Any], hop: int, visited: set[str]) -> _Candidate | None:
        """The step over ``row``'s edge from ``start`` the direction allows, else None."""

        label = self._label(row["labels"])
        if label is None or row["uuid"] in visited or not self._trusted(row["basis"]):
            return None
        outgoing = bool(row["outgoing"])
        source, target = (start.label, label) if outgoing else (label, start.label)
        role = self.ontology.impact_role(row["relation"], source, target)
        at_root = start.label in self.ontology.spine.roots
        step: Step
        if self._walks(role, outgoing=outgoing) and (not at_root or role in HUB_ROLES):
            step = "forward" if outgoing else "backward"
        elif not at_root and not outgoing and self._is_member(row["relation"], source, target):
            step = "scope"
        else:
            return None
        basis = row["basis"] or STATED
        inferred = basis == INFERRED
        node = TraceNode(
            spec_id=row["spec_id"],
            label=label,
            name=row["name"] or "",
            hops=hop,
            parent=start.spec_id,
            relation=row["relation"],
            step=step,
            role=role,
            basis=basis,
            confidence=row["confidence"] if inferred else None,
            rule=row["inference_rule"] if inferred else None,
            weak=start.weak or role == RELATED,
            via_owner=start.via_owner,
            spine_root=label in self.ontology.spine.roots,
        )
        return _Candidate(node, row["uuid"])

    def _walks(self, role: str, *, outgoing: bool) -> bool:
        """Whether an edge of ``role``, met in its stored direction (``outgoing``) or against it, is walked."""

        if role in (COUPLED, RELATED):
            return True
        if role == USES:
            return outgoing == (self.direction == BASED_ON)
        if role == USED_BY:
            return outgoing == (self.direction == AFFECTS)
        return False

    def _is_member(self, relation: str, source: str, target: str) -> bool:
        """Whether an edge of ``relation`` from ``source`` to ``target`` is an owner pair (scope step)."""

        pair = self.ontology.pair_rule(relation, source, target)
        return pair is not None and pair.owner

    def _has_hub_role(self, relation: str) -> bool:
        return any(pair.impact in HUB_ROLES for pair in self.ontology.pairs(relation))

    # Reads and rows

    async def _read_hop(self, frontier: Sequence[str], relations: Sequence[str]) -> list[dict[str, Any]]:
        return await self.executor.read(
            READ_TRACE_HOP,
            self.project_id,
            frontier=list(frontier),
            relations=list(relations),
            labels=list(self.labels),
        )

    def _trusted(self, basis: str | None) -> bool:
        return self.any_basis or (basis or STATED) in TRUSTED_BASES

    def _label(self, labels: Sequence[str]) -> str | None:
        """The one spec label of a node, else None."""

        found = [label for label in labels if label in self.labels]
        return found[0] if len(found) == 1 else None
