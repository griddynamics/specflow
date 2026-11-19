"""Step ``scope``: the change reports of the builds since the last gap run (docs/graph/gap/detection.md "Run modes and
scope", lifecycle.md "Rekeys and merges"; FR-GRUN-02).

The reports (``GapChangeReport``, in build order) give:

- the **alias map**: every ``merged`` and ``rekeyed`` old id mapped to its surviving id, chains followed to the last
  build (``a -> b`` then ``b -> c`` gives ``a -> c``);
- the **touched ids**: every id the reports name as added, removed, changed in meaning, links or evidence, and the
  surviving ids of merges and rekeys, mapped through the alias map.

The reports decide what is new for the investigation, never what the detectors look at: detectors always run over
the whole graph, and a site whose inputs did not change keeps its cached verdict (detection.md "Sites").

The step also moves the ``ABOUT`` and ``IMPACTS`` relations of existing gaps to the surviving key. A build moves every
relation of a merged node to the survivor and keeps a rekeyed node's relations, so after a merge or rekey a gap
relation's edge key still names the old id: the step deletes that relation and writes it again under the edge key of
the current id (or only deletes it when the gap already has that relation). The gap's signature is not touched: it
keeps the old ids, and the alias map resolves them. Nothing else of a gap changes here; the close check decides about
gaps whose nodes were removed (lifecycle.md "Close check").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.core.ontology import Ontology
from graph.core.writer import NodeRef, OwnedWriter
from graph.gap.gaps import StoredGap, StoredLink, gap_uuid, link_key
from graph.gap.models import GapChangeReport
from graph.gap.names import GAP, OWNER
from logger.logger import INFO, get_logger, log_event

logger = get_logger(__name__)

MAX_ALIAS_HOPS: Final = 64
"""A guard against a cyclic alias chain in malformed reports."""


@dataclass(frozen=True)
class ScopeOutcome:
    """The alias map, the touched ids and the number of gap relations moved to a surviving key."""

    aliases: Mapping[str, str]
    touched: tuple[str, ...]
    moved: int = 0
    builds: int = 0

    def payload(self) -> dict[str, Any]:
        return {
            "aliases": dict(sorted(self.aliases.items())),
            "touched": list(self.touched),
            "moved": self.moved,
            "builds": self.builds,
        }


def alias_map(changes: Sequence[GapChangeReport]) -> dict[str, str]:
    """Every old id of a merge or rekey to its id after the last report (chains followed, cycles cut)."""

    step = {
        old: new for report in changes for old, new in (*report.merged.items(), *report.rekeyed.items()) if old != new
    }
    resolved: dict[str, str] = {}
    for old, first in step.items():
        current, hops = first, 0
        while current in step and hops < MAX_ALIAS_HOPS and step[current] != old:
            current, hops = step[current], hops + 1
        if current != old:
            resolved[old] = current
    return resolved


def touched_ids(changes: Sequence[GapChangeReport], aliases: Mapping[str, str]) -> tuple[str, ...]:
    """The ids the reports touch, mapped through ``aliases`` (removed ids stay as they are), sorted."""

    found: set[str] = set()
    for report in changes:
        found.update(report.added, report.removed, report.changed_meaning, report.changed_links)
        found.update(report.changed_evidence, report.merged.values(), report.rekeyed.values())
    return tuple(sorted({aliases.get(spec_id, spec_id) for spec_id in found}))


def stale_links(gap: StoredGap) -> list[StoredLink]:
    """The gap's ``ABOUT`` and ``IMPACTS`` relations whose edge key does not name the node's current id."""

    return [
        link
        for link in (*gap.about, *gap.impacts)
        if link.edge_key != link_key(gap.gap_id, link.relation, link.spec_id)
    ]


async def move_relations(
    source: Any, ontology: Ontology, project_id: str, gaps: Sequence[StoredGap], *, database: str | None = None
) -> int:
    """Rewrite every stale ``ABOUT``/``IMPACTS`` relation of ``gaps`` under its current key, one transaction per gap
    (owner ``gap``); returns the number of relations moved."""

    moved = 0
    for gap in gaps:
        stale = stale_links(gap)
        if not stale:
            continue
        current = {link.edge_key for link in (*gap.about, *gap.impacts)}
        writer = OwnedWriter(source, ontology, OWNER, project_id, database=database)
        async with writer.transaction() as tx:
            for link in stale:
                await tx.delete_relationship(link.relation, link.edge_key)
                key = link_key(gap.gap_id, link.relation, link.spec_id)
                if key not in current:
                    await tx.upsert_relationship(
                        link.relation,
                        NodeRef(GAP, gap_uuid(project_id, gap.gap_id)),
                        NodeRef(link.label, link.uuid),
                        key,
                        {} if link.added_by_run is None else {"added_by_run": link.added_by_run},
                    )
                    current.add(key)
                moved += 1
    return moved


async def scope(
    source: Any,
    ontology: Ontology,
    project_id: str,
    changes: Sequence[GapChangeReport],
    gaps: Sequence[StoredGap],
    *,
    persist: bool = True,
    run_id: str = "",
) -> ScopeOutcome:
    """The alias map and touched ids of ``changes``; with ``persist``, the gap relations moved to surviving keys."""

    aliases = alias_map(changes)
    touched = touched_ids(changes, aliases)
    moved = await move_relations(source, ontology, project_id, gaps) if persist else 0
    log_event(
        logger,
        INFO,
        "graph.gap.scope.done",
        project_id=project_id,
        run_id=run_id,
        builds=len(changes),
        aliases=len(aliases),
        touched=len(touched),
        moved=moved,
    )
    return ScopeOutcome(aliases=aliases, touched=touched, moved=moved, builds=len(changes))
