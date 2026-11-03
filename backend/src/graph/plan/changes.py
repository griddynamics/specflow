"""Report-chain checks, merged changes, revisions and aliases."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from graph.core.errors import ConfigurationError
from graph.plan.models import BuildChangeReport, ChangeClass, MeaningRevisions
from graph.plan.names import PlanMode

__all__ = ["ChangeSet", "change_set", "check_chain", "meaning_revisions", "resolve_spec_id"]

_MEANING: Final = frozenset({"added", "removed", "merged", "carried_over", "rekeyed", "changed_meaning"})
_LINK_NODE: Final = "changed_links"
_IGNORED: Final = "changed_evidence"


class _MissingReportsError(ConfigurationError):
    """A gap in the consumed report chain; ``missing`` lists the boundary ids."""

    def __init__(self, missing: Sequence[str], first_missing: str | None) -> None:
        self.missing: tuple[str, ...] = tuple(missing)
        self.first_missing = first_missing
        super().__init__(
            "change_reports_missing",
            missing=",".join(self.missing),
            first_missing=first_missing,
        )


@dataclass(frozen=True)
class ChangeSet:
    """Every meaning and link change of the reports, in build order; evidence changes are ignored."""

    meaning: tuple[tuple[str, ChangeClass], ...] = ()
    links: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        """Whether nothing of the plan depends on these reports."""

        return not self.meaning and not self.links


def check_chain(
    reports: Iterable[BuildChangeReport],
    last_build_id: str | None,
    build_id: str,
    mode: PlanMode,
) -> tuple[BuildChangeReport, ...]:
    """Validate the report chain, in build order; ``first`` accepts no changes.

    Build order is the passed chain itself, never the ordering of build ids. An ``update`` requires
    the chain to start at ``last_build_id``, continue without a gap and end at ``build_id``.
    """

    ordered = tuple(reports)
    if mode in ("first", "replan") and not ordered:
        return ()
    if mode == "first":
        raise _MissingReportsError((), None)

    missing: list[str] = []
    previous: str | None = last_build_id
    for report in ordered:
        if report.previous_build_id != previous:
            missing.append(report.previous_build_id or "")
        previous = report.build_id
    if ordered:
        if previous != build_id:
            missing.append(build_id)
    else:
        missing.append(last_build_id or "")
        missing.append(build_id)
    if missing:
        first = next((item for item in missing if item), None)
        raise _MissingReportsError(missing, first)
    return ordered


def change_set(reports: Iterable[BuildChangeReport]) -> ChangeSet:
    """Merge report changes in build order; the last meaning class of a node wins.

    A ``changed_links`` node and both ends of every changed edge become link changes. ``changed_evidence``
    is ignored.
    """

    meaning: dict[str, ChangeClass] = {}
    links: set[str] = set()
    for report in tuple(reports):
        for node in report.nodes:
            classes = tuple(item for item in node.classes if item != _IGNORED)
            for item in classes:
                if item in _MEANING:
                    meaning[node.spec_id] = item
                elif item == _LINK_NODE:
                    links.add(node.spec_id)
        for edge in report.edges:
            links.add(edge.source)
            links.add(edge.target)
    return ChangeSet(
        meaning=tuple(sorted(meaning.items())),
        links=frozenset(links),
    )


def resolve_spec_id(spec_id: str, aliases: Mapping[str, str]) -> str:
    """Resolve a merged or rekeyed id to its survivor; unresolved ids stay unchanged.

    Alias chains are resolved in input order, without cycles. An id that is both current and an alias
    resolves to itself, so survivor nodes never change even when their old ids are still listed.
    """

    current = spec_id
    visited = {spec_id}
    while (survivor := aliases.get(current)) is not None:
        if survivor in visited:
            break
        visited.add(survivor)
        current = survivor
    return current


def meaning_revisions(
    reports: Iterable[BuildChangeReport],
    node_build_ids: Mapping[str, str],
    stored: MeaningRevisions | Mapping[str, str],
    build_id: str,
    *,
    aliases: Mapping[str, str] | None = None,
    consumed: Iterable[str] = (),
    replan_without_reports: bool = False,
) -> MeaningRevisions:
    """Derive the run's meaning revisions from reports, stored revisions and the graph.

    A current node without a stored revision gets the build the run consumes. On a report-less
    ``replan``, every graph node newer than the consumed chain is also set to the consumed build.
    Merge and rekey changes resolve to their survivor; obsolete alias keys are dropped. Nothing is
    persisted here.
    """

    ordered = tuple(reports)
    resolved_aliases = aliases or {}
    stored_nodes = stored.nodes if isinstance(stored, MeaningRevisions) else stored
    consumed_chain = tuple(consumed)
    current_nodes = frozenset(node_build_ids)
    revisions = {spec_id: stored_nodes[spec_id] for spec_id in sorted(current_nodes) if spec_id in stored_nodes}

    if not ordered and replan_without_reports:
        for spec_id, last_build_id in sorted(node_build_ids.items()):
            if last_build_id not in consumed_chain:
                revisions[spec_id] = build_id
        for spec_id in sorted(current_nodes):
            revisions.setdefault(spec_id, build_id)
        return MeaningRevisions(nodes=revisions, consumed=consumed_chain)

    reported: set[str] = set()
    for report in ordered:
        for node in report.nodes:
            survivor = resolve_spec_id(node.spec_id, resolved_aliases)
            reported.add(survivor)
            if any(item in _MEANING for item in node.classes):
                revisions[survivor] = report.build_id

    for spec_id in sorted(node_build_ids):
        survivor = resolve_spec_id(spec_id, resolved_aliases)
        if survivor in reported:
            continue
        revisions.setdefault(survivor, build_id)

    return MeaningRevisions(nodes=revisions, consumed=consumed_chain)
