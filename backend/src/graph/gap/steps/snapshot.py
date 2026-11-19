"""The graph a detection reads, held in memory: spec nodes, domain edges, documents, active fragments and sections.

Detection reads the persisted graph once through the core reader (``nodes``, ``edges``, ``documents``,
``fragments``) and answers every detector, the relevance filter and the site grouping from this snapshot, so the
detection time grows with the graph size and not with the number of detector questions (NFR-GPERF-01). A
**section** is a fragment's heading path within its document lineage (docs/graph/gap/detection.md "Relevance
filter"); fragments of one heading path form one section. Every spec-side name comes from the ontology or the rules
file (FR-GONT-04): spine roots, spine paths and the spine ``exclude`` are the ontology's.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from graph.core import Ontology
from graph.core.ontology.compiled import NodeView
from graph.core.reader import GraphDocument, GraphEdge, GraphFragment, GraphNode
from graph.gap.models import SectionKey, Witness

ACTIVE: Final = "active"
HEADING_MARK: Final = " \u203a "


@dataclass(frozen=True, slots=True)
class Line:
    """A line (or the quoted part of one) of an active fragment, with its line range in the document."""

    fragment_id: str
    text: str
    line_start: int | None = None
    line_end: int | None = None

    def witness(self) -> Witness:
        return Witness(
            fragment_id=self.fragment_id,
            quote=self.text,
            line_start=self.line_start,
            line_end=self.line_end,
        )


@dataclass(frozen=True)
class Section:
    """One section: a heading path of a document lineage, its fragments and their joined text."""

    lineage: str
    heading_path: tuple[str, ...]
    document_title: str
    document_path: str
    fragment_ids: tuple[str, ...]
    text: str
    text_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "text_hash", hashlib.sha256(self.text.encode("utf-8")).hexdigest())

    @property
    def key(self) -> SectionKey:
        return SectionKey(lineage=self.lineage, heading_path=self.heading_path, text_hash=self.text_hash)

    @property
    def ident(self) -> tuple[str, tuple[str, ...]]:
        """The section's place, without its text: lineage and heading path."""

        return (self.lineage, self.heading_path)

    @property
    def label(self) -> str:
        """``<document path> > <heading> > ...`` (heading mark ``HEADING_MARK``): how a report names the section."""

        return HEADING_MARK.join((self.document_path or self.lineage, *self.heading_path))


def heading_path_of(fragment: GraphFragment) -> tuple[str, ...]:
    """A fragment's heading path as a tuple (a store may hand it over as a JSON list in a string)."""

    value = fragment.heading_path
    if value is None:
        return ()
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except ValueError:
            return (value,)
        return tuple(str(item) for item in loaded) if isinstance(loaded, list) else (value,)
    return tuple(value)


class GraphSnapshot:
    """The spec graph of one project at detection time (read-only)."""

    def __init__(
        self,
        ontology: Ontology,
        nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
        documents: Iterable[GraphDocument],
        fragments: Iterable[GraphFragment],
    ) -> None:
        self.ontology = ontology
        self.nodes: dict[str, GraphNode] = {node.spec_id: node for node in sorted(nodes, key=lambda n: n.spec_id)}
        self.edges: tuple[GraphEdge, ...] = tuple(
            edge for edge in edges if edge.source in self.nodes and edge.target in self.nodes
        )
        outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        incoming: dict[str, list[GraphEdge]] = defaultdict(list)
        for edge in self.edges:
            outgoing[edge.source].append(edge)
            incoming[edge.target].append(edge)
        self.outgoing: Mapping[str, Sequence[GraphEdge]] = outgoing
        self.incoming: Mapping[str, Sequence[GraphEdge]] = incoming
        self.documents: dict[str, GraphDocument] = {
            document.document_id: document for document in documents if (document.status or ACTIVE) == ACTIVE
        }
        self.fragments: dict[str, GraphFragment] = {
            fragment.fragment_id: fragment
            for fragment in sorted(fragments, key=lambda f: (f.document_id, f.ordinal or 0, f.fragment_id))
            if (fragment.status or ACTIVE) == ACTIVE and fragment.document_id in self.documents
        }
        self.sections, self.section_of_fragment = self._sections()
        self.node_sections: dict[str, tuple[Section, ...]] = {
            spec_id: self._sections_of(node) for spec_id, node in self.nodes.items()
        }
        self._owners: dict[tuple[Any, ...], frozenset[str]] = {}

    # Sections and lines

    def _sections(self) -> tuple[tuple[Section, ...], dict[str, Section]]:
        grouped: dict[tuple[str, tuple[str, ...]], list[GraphFragment]] = defaultdict(list)
        for fragment in self.fragments.values():
            grouped[(fragment.document_id, heading_path_of(fragment))].append(fragment)
        sections: list[Section] = []
        by_fragment: dict[str, Section] = {}
        for (lineage, path), members in grouped.items():
            document = self.documents[lineage]
            section = Section(
                lineage=lineage,
                heading_path=path,
                document_title=document.title or "",
                document_path=document.path or "",
                fragment_ids=tuple(member.fragment_id for member in members),
                text="\n".join(member.text for member in members),
            )
            sections.append(section)
            by_fragment.update(dict.fromkeys(section.fragment_ids, section))
        return tuple(sections), by_fragment

    def _sections_of(self, node: GraphNode) -> tuple[Section, ...]:
        found = {
            self.section_of_fragment[item.fragment_id].ident: self.section_of_fragment[item.fragment_id]
            for item in node.provenance
            if item.fragment_id in self.section_of_fragment
        }
        return tuple(found.values())

    def lines(self, node: GraphNode) -> tuple[Line, ...]:
        """The node's evidence lines in provenance order: each provenance quote in an active fragment, or, when a store
        keeps only the line range (no quote), the text of those lines of the fragment."""

        found: list[Line] = []
        for item in node.provenance:
            if item.fragment_id not in self.fragments:
                continue
            if item.quote:
                found.append(Line(item.fragment_id, item.quote, item.line_start, item.line_end))
                continue
            ranged = self._range(item.fragment_id, item.line_start, item.line_end)
            if ranged is not None:
                found.append(ranged)
        return tuple(found)

    def _range(self, fragment_id: str, start: int | None, end: int | None) -> Line | None:
        if start is None:
            return None
        last = start if end is None else end
        text = " ".join(
            line.text.strip()
            for line in self.fragment_lines(fragment_id)
            if line.line_start is not None and start <= line.line_start <= last
        )
        return Line(fragment_id, text, start, last) if text else None

    def text(self, node: GraphNode) -> str:
        """What the text says about the node: its evidence quotes and statements (never the generated name)."""

        parts = [line.text for line in self.lines(node)]
        parts += [statement.text for statement in node.statements if statement.text]
        return "\n".join(dict.fromkeys(parts))

    def fragment_lines(self, fragment_id: str) -> tuple[Line, ...]:
        """Every line of an active fragment with its line number (from the fragment's ``line_start``)."""

        fragment = self.fragments.get(fragment_id)
        if fragment is None:
            return ()
        start = fragment.line_start
        return tuple(
            Line(
                fragment_id, text, None if start is None else start + offset, None if start is None else start + offset
            )
            for offset, text in enumerate(fragment.text.splitlines())
        )

    def section_of_line(self, line: Line) -> Section | None:
        return self.section_of_fragment.get(line.fragment_id)

    # Attributes and edges

    def value(self, node: GraphNode, attribute: str) -> Any:
        """The node's value of ``attribute``, its label's declared default when the node has none."""

        value = node.attributes.get(attribute)
        if value is None or value == "":
            return self.ontology.attribute_default(node.label, attribute)
        return value

    def view(self, node: GraphNode) -> NodeView:
        return NodeView(node.label, node.attributes)

    def matches(self, node: GraphNode, condition: Mapping[str, Sequence[str]]) -> bool:
        """True when ``condition`` (``{attribute: values}``) holds for the node (a missing value reads as the
        default); an empty condition never holds."""

        return bool(condition) and self.ontology.condition_matches(
            {name: frozenset(values) for name, values in condition.items()}, self.view(node)
        )

    def out_of_spine(self, node: GraphNode) -> bool:
        """True when the ontology's spine ``exclude`` holds for the node (an out-of-scope row)."""

        return self.ontology.condition_matches(self.ontology.spine_exclude(), self.view(node))

    def related(self, spec_id: str) -> set[str]:
        """The nodes one domain edge away, either direction."""

        return {edge.target for edge in self.outgoing.get(spec_id, ())} | {
            edge.source for edge in self.incoming.get(spec_id, ())
        }

    def step(self, spec_id: str, relation: str) -> set[str]:
        """The nodes one ``relation`` edge away in its stored direction (either way for a symmetric relation)."""

        found = {edge.target for edge in self.outgoing.get(spec_id, ()) if edge.relation == relation}
        if self.ontology.is_symmetric(relation):
            found |= {edge.source for edge in self.incoming.get(spec_id, ()) if edge.relation == relation}
        return found

    def spine_owners(
        self,
        spec_id: str,
        exclude: Mapping[str, Sequence[str]] | None = None,
        *,
        without: Collection[str] = (),
    ) -> frozenset[str]:
        """The spine roots the node reaches along the ontology's spine paths (inferred edges included), never through
        another root; a root matching the spine ``exclude`` or ``exclude`` is no owner; paths using a relation of
        ``without`` are left out. A root is its own owner."""

        condition = tuple(sorted((name, tuple(values)) for name, values in (exclude or {}).items()))
        key = (spec_id, condition, tuple(sorted(without)))
        cached = self._owners.get(key)
        if cached is not None:
            return cached
        roots = self.ontology.spine.roots
        node = self.nodes.get(spec_id)
        reached: set[str] = set()
        if node is not None and node.label in roots:
            reached.add(spec_id)
        elif node is not None:
            for path in self.ontology.spine.paths:
                if set(path.relations) & set(without):
                    continue
                reached |= self._sequence(spec_id, path.relations) if path.sequence else self._via(spec_id, path)
        owners = frozenset(
            item
            for item in reached
            if self.nodes[item].label in roots
            and not self.out_of_spine(self.nodes[item])
            and not self.matches(self.nodes[item], dict(condition))
        )
        self._owners[key] = owners
        return owners

    def _sequence(self, spec_id: str, relations: Sequence[str]) -> set[str]:
        roots = self.ontology.spine.roots
        frontier = {spec_id}
        for index, relation in enumerate(relations):
            frontier = {target for item in frontier for target in self.step(item, relation)}
            if index < len(relations) - 1:
                frontier = {item for item in frontier if self.nodes[item].label not in roots}
        return frontier

    def _via(self, spec_id: str, path: Any) -> set[str]:
        roots = self.ontology.spine.roots
        seen = {spec_id}
        frontier = {spec_id}
        found: set[str] = set()
        for hop in range(1, path.max_hops + 1):
            reached = {
                target
                for item in frontier
                for relation in path.relations
                for target in self.step(item, relation)
                if target not in seen
            }
            seen |= reached
            if hop >= path.min_hops:
                found |= {item for item in reached if self.nodes[item].label in roots}
            frontier = {item for item in reached if self.nodes[item].label not in roots}
            if not frontier:
                break
        return found


async def read_snapshot(reader: Any, project_id: str, ontology: Ontology) -> GraphSnapshot:
    """Read the project's spec nodes, domain edges (inferred ones included), documents and fragments once."""

    nodes = await reader.nodes(project_id)
    edges = await reader.edges(project_id, include_inferred=True)
    documents = await reader.documents(project_id)
    fragments = await reader.fragments(project_id)
    return GraphSnapshot(ontology, nodes, edges, documents, fragments)
