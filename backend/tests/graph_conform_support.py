"""Hand-built Conform inputs for the  Conform tests (``test_graph_conform_precedence``,
``test_graph_answered``): a plan of one unit per document and canonical nodes keyed as Resolve keys them."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from graph.build.models import CanonicalNode, ChangeSet, DocumentRec, FragmentRec, PlanArtifact, SourceQuote, Unit
from graph.build.resolve import hub_surface
from graph.core.identity import normalise_surface


def _fragments(document_id: str, texts: Sequence[str]) -> list[FragmentRec]:
    """One fragment per text, a blank source line between two; the content hash follows the text."""

    found: list[FragmentRec] = []
    line = 1
    for index, text in enumerate(texts):
        last = line + text.count("\n")
        found.append(
            FragmentRec(
                fragment_id=f"{document_id}#s{index}",
                document_id=document_id,
                heading_path=(f"Section {index}",),
                ordinal=index,
                line_start=line,
                line_end=last,
                text=text,
                content_hash=f"hash-{text}",
                tokens=max(1, len(text) // 4),
            )
        )
        line = last + 2
    return found


def conform_plan(*documents: tuple[str, Sequence[str]]) -> PlanArtifact:
    """A ``first`` plan: per ``(document id, texts)`` one document and one unit ``<document id>@u0``."""

    fragments: list[FragmentRec] = []
    records: list[DocumentRec] = []
    units: list[Unit] = []
    for document_id, texts in documents:
        own = _fragments(document_id, texts)
        fragments.extend(own)
        records.append(
            DocumentRec(
                document_id=document_id,
                revision_id=f"{document_id}-r1",
                version=1,
                path=f"{document_id}.md",
                title=document_id.title(),
            )
        )
        units.append(
            Unit(
                unit_id=f"{document_id}@u0",
                document_id=document_id,
                fragment_ids=tuple(fragment.fragment_id for fragment in own),
                tokens=10,
                content_hash=f"unit-{document_id}",
                windows=((0, len(own)),),
            )
        )
    return PlanArtifact(
        mode="first",
        change_set=ChangeSet(new=tuple(record.document_id for record in records)),
        documents=tuple(records),
        fragments=tuple(fragments),
        dirty_ids=tuple(fragment.fragment_id for fragment in fragments),
        units=tuple(units),
    )


def canonical(
    cid: str,
    name: str,
    label: str,
    fragments: Sequence[str],
    *,
    quote: str | None = None,
    hubs: Collection[str] = (),
) -> CanonicalNode:
    """A canonical node over ``fragments``: a hub (``label`` in ``hubs``) keyed by its surface, any other
    node by a provisional span key; ``quote``: its one source quote over all its fragments."""

    hub = label in hubs
    return CanonicalNode(
        cid=cid,
        identity_key=f"{label}|surface|{hub_surface(name)}" if hub else f"requirement|span|{normalise_surface(name)}",
        provisional=not hub,
        name=name,
        label=label,
        member_ids=(f"raw-{cid}",),
        fragment_ids=tuple(fragments),
        name_embedding_text=f"{label}: {name}",
        quotes=(SourceQuote(quote=quote, fragment_ids=tuple(fragments)),) if quote else (),
    )
