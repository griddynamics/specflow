"""Gap nodes as graph.gap reads them back: one query (``READ_GAPS``) for the run engine and the service.

``read_gaps`` returns each stored gap with its ``ABOUT`` and ``IMPACTS`` nodes (current spec id, storage key, domain
label, name, the documents of their evidence, the relation's edge key), its ``CITES`` witnesses, its follow-up chain
and the clarifications that answer it (docs/graph/gap/data-model.md "Graph model"). The keys of gap nodes and
relations are deterministic (``gap_uuid``, ``link_key``, ``cite_key``), so a write repeated by a resumed run is the
same write. Every read binds and filters the project id (FR-GOWN-02).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from graph.core.identity import NAMESPACE
from graph.core.ontology import Ontology
from graph.core.store import Executor
from graph.gap.identity import normalise_quote
from graph.gap.models import FollowUp
from graph.gap.names import ABOUT, CITES, GRAPH_STATUSES, IMPACTS
from graph.gap.queries import READ_GAPS

QUOTE_KEY_HEX: Final = 16


def gap_uuid(project_id: str, gap: str) -> str:
    """The storage key (``uuid``) of a gap node: uuid5 of the project and the gap id."""

    return str(uuid.uuid5(NAMESPACE, f"{project_id}|gap|{gap}"))


def clarification_uuid(project_id: str, clr: str) -> str:
    """The storage key (``uuid``) of a clarification node: uuid5 of the project and the clarification id."""

    return str(uuid.uuid5(NAMESPACE, f"{project_id}|clarification|{clr}"))


def answer_key(clr: str, relation: str, end: str) -> str:
    """The edge key of a clarification's relation (``ANSWERS`` to its gap, ``GROUNDED_IN`` to a fragment)."""

    return f"{clr}|{relation}|{end}"


def link_key(gap: str, relation: str, spec_id: str) -> str:
    """The edge key of an ``ABOUT`` or ``IMPACTS`` relation: the gap, the relation and the current spec id."""

    return f"{gap}|{relation}|{spec_id}"


def quote_hash(quote: str) -> str:
    """SHA-256 (hex) of the normalised quote (the ``quote_hash`` of a ``CITES`` relation)."""

    return hashlib.sha256(normalise_quote(quote).encode("utf-8")).hexdigest()


def cite_key(gap: str, fragment_id: str, digest: str) -> str:
    """The edge key of a ``CITES`` relation: the gap, the fragment and the start of the quote hash."""

    return f"{gap}|{CITES}|{fragment_id}|{digest[:QUOTE_KEY_HEX]}"


@dataclass(frozen=True)
class StoredLink:
    """An ``ABOUT`` or ``IMPACTS`` relation of a stored gap and the spec node it ends at."""

    relation: str
    spec_id: str
    uuid: str
    label: str
    name: str
    edge_key: str
    documents: tuple[str, ...] = ()
    added_by_run: str | None = None


@dataclass(frozen=True)
class StoredCite:
    """A ``CITES`` relation: the fragment, its document, the quote and its lines."""

    fragment_id: str
    document_id: str | None
    quote: str
    edge_key: str
    line_start: int | None = None
    line_end: int | None = None
    quote_hash: str | None = None


@dataclass(frozen=True)
class StoredGap:
    """A gap node of the graph with its relations; ``props`` are the stored properties as read."""

    gap_id: str
    props: Mapping[str, Any]
    about: tuple[StoredLink, ...] = ()
    impacts: tuple[StoredLink, ...] = ()
    cites: tuple[StoredCite, ...] = ()
    follow_ups: tuple[FollowUp, ...] = ()
    clarifications: tuple[Mapping[str, Any], ...] = field(default=())

    @property
    def signature(self) -> str:
        return str(self.props.get("signature") or "")

    @property
    def status(self) -> str:
        return str(self.props.get("status") or "")

    @property
    def origin(self) -> str:
        return str(self.props.get("origin") or "")

    @property
    def about_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(link.spec_id for link in self.about))

    @property
    def impact_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(link.spec_id for link in self.impacts))

    @property
    def documents(self) -> frozenset[str]:
        """The documents the gap cites a fragment of, or holds evidence of a node it is ``ABOUT``."""

        cited = {cite.document_id for cite in self.cites if cite.document_id}
        return frozenset(cited | {document for link in self.about for document in link.documents})


def _label(labels: Sequence[str], known: Collection[str]) -> str:
    return next((label for label in labels if label in known), "")


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _links(rows: Sequence[Mapping[str, Any]], relation: str, known: Collection[str]) -> tuple[StoredLink, ...]:
    found = [
        StoredLink(
            relation=relation,
            spec_id=str(row["spec_id"]),
            uuid=str(row.get("uuid") or ""),
            label=_label(row.get("labels") or (), known),
            name=str(row.get("name") or ""),
            edge_key=str(row.get("edge_key") or ""),
            documents=tuple(sorted({str(item) for item in row.get("documents") or () if item})),
            added_by_run=row.get("added_by_run"),
        )
        for row in rows
        if row.get("relation") == relation and row.get("spec_id")
    ]
    return tuple(sorted(found, key=lambda link: (link.spec_id, link.edge_key)))


def _cites(rows: Sequence[Mapping[str, Any]]) -> tuple[StoredCite, ...]:
    found = [
        StoredCite(
            fragment_id=str(row["fragment_id"]),
            document_id=row.get("document_id"),
            quote=str(row.get("quote") or ""),
            edge_key=str(row.get("edge_key") or ""),
            line_start=_int(row.get("line_start")),
            line_end=_int(row.get("line_end")),
            quote_hash=row.get("quote_hash"),
        )
        for row in rows
        if row.get("fragment_id")
    ]
    return tuple(sorted(found, key=lambda cite: (cite.fragment_id, cite.line_start or 0, cite.edge_key)))


def _follow_ups(rows: Sequence[Mapping[str, Any]]) -> tuple[FollowUp, ...]:
    found = [
        FollowUp(
            gap_id=str(row["gap_id"]),
            reason=row["reason"],
            direction="continues" if row.get("continues") else "continued_by",
        )
        for row in rows
        if row.get("gap_id") and row.get("reason")
    ]
    return tuple(sorted(found, key=lambda item: (item.direction, item.gap_id)))


def stored_gap(row: Mapping[str, Any], known_labels: Collection[str]) -> StoredGap:
    """One ``READ_GAPS`` row as a ``StoredGap``; ``known_labels`` are the ontology's labels."""

    props: Mapping[str, Any] = row["gap"]
    nodes = list(row.get("nodes") or ())
    return StoredGap(
        gap_id=str(props["gap_id"]),
        props=dict(props),
        about=_links(nodes, ABOUT, known_labels),
        impacts=_links(nodes, IMPACTS, known_labels),
        cites=_cites(list(row.get("cites") or ())),
        follow_ups=_follow_ups(list(row.get("follow_ups") or ())),
        clarifications=tuple(dict(item) for item in row.get("clarifications") or () if item),
    )


async def read_gaps(
    executor: Executor,
    project_id: str,
    ontology: Ontology,
    *,
    statuses: Sequence[str] = GRAPH_STATUSES,
    gap_ids: Sequence[str] = (),
) -> tuple[StoredGap, ...]:
    """The project's gaps of ``statuses`` (and of ``gap_ids`` when given), by gap id."""

    rows = await executor.read(READ_GAPS, project_id, statuses=list(statuses), gap_ids=list(gap_ids))
    known = frozenset(ontology.labels)
    return tuple(stored_gap(row, known) for row in rows)
