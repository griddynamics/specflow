"""The node and edge extraction calls of the build."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from graph.build.answers import EDGES_LIST, NodeList, edge_answer_model, node_answer_model
from graph.build.models import Window
from graph.build.prompts import (
    EDGE_WINDOW_PROMPT,
    EDGE_WINDOW_WARM_UP_PROMPT,
    NODE_PROMPTS,
    NODE_WARM_UP_PROMPT,
    KnownTerm,
    ReferencedEntity,
    edge_window_messages,
    edge_window_warm_up_messages,
    node_messages,
    node_warm_up_messages,
)
from graph.core.llm import Gateway
from graph.core.ontology import Ontology

__all__ = [
    "EdgeCall",
    "ExtractedEdge",
    "ExtractedNode",
    "NodeCall",
    "extract_edge_window_call",
    "extract_nodes_call",
    "warm_up_edge_windows",
    "warm_up_nodes",
]

EXTRACTION_CLASS: Final = "extraction"


@dataclass(frozen=True)
class ExtractedNode:
    """One node of a node call: ``indices`` are the 0-based unit fragment indices within the window the model gave
    (empty when it gave none there); ``quote`` is ``None`` when the model gave none; ``aliases``: the other names a hub
    item gave (whitespace folded, its own name and repeats left out).
    """

    name: str
    label: str
    quote: str | None
    indices: tuple[int, ...]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class NodeCall:
    """Result of one node call: its nodes in answer order, the names given two or more labels and the
    items the answer put in a list the pass did not ask for (left out)."""

    nodes: list[ExtractedNode]
    label_collapses: int = 0
    stray_items: int = 0


@dataclass(frozen=True)
class ExtractedEdge:
    """One fact of an edge call between two catalog names; ``indices`` as for ``ExtractedNode``;
    ``outside``: the fact named fragment numbers, none of them inside the window."""

    relation: str
    source_name: str
    target_name: str
    fact: str
    indices: tuple[int, ...]
    outside: bool = False


@dataclass(frozen=True)
class EdgeCall:
    """Result of one edge call: the kept facts and the counts of dropped ones (a name outside the
    catalog, a relation outside the extracted relations, an entity linked to itself)."""

    edges: list[ExtractedEdge]
    unknown_names: int = 0
    unknown_relations: int = 0
    self_loops: int = 0


def _indices(numbers: Iterable[Any], window: Window) -> tuple[int, ...]:
    """The fragment ``numbers`` (from 1) as 0-based unit indices inside ``window``, sorted and unique."""

    start, end = window
    return tuple(sorted(index for index in _numbers(numbers) if start <= index < end))


def _numbers(numbers: Iterable[Any]) -> set[int]:
    """The fragment ``numbers`` (from 1) as 0-based indices, whatever their window."""

    return {number - 1 for number in numbers if isinstance(number, int) and not isinstance(number, bool)}


def _name_key(name: str) -> str:
    return " ".join(name.split()).lower()


def _aliases(name: str, values: Iterable[Any]) -> tuple[str, ...]:
    """The other names of an item: whitespace folded, empty ones, its own name and repeats left out."""

    seen = {_name_key(name)}
    found: list[str] = []
    for value in values:
        alias = " ".join(str(value).split())
        if alias and _name_key(alias) not in seen:
            seen.add(_name_key(alias))
            found.append(alias)
    return tuple(found)


async def warm_up_nodes(gateway: Gateway, ontology: Ontology, fragments: Sequence[str]) -> None:
    """The prompt-cache warm-up of a unit's node calls (either pass: they share the prefix and the
    answer schema)."""

    await gateway.warm_up(
        EXTRACTION_CLASS,
        None,
        node_warm_up_messages(ontology, fragments),
        node_answer_model(ontology),
        prompt=NODE_WARM_UP_PROMPT,
    )


async def warm_up_edge_windows(
    gateway: Gateway,
    ontology: Ontology,
    fragments: Sequence[str],
    catalog: Sequence[tuple[str, str]],
    *,
    sections: Mapping[str, str] | None = None,
    referenced: Mapping[str, ReferencedEntity] | None = None,
) -> None:
    """The prompt-cache warm-up of a unit's edge-window calls: system, unit and catalog messages."""

    await gateway.warm_up(
        EXTRACTION_CLASS,
        None,
        edge_window_warm_up_messages(ontology, fragments, catalog, sections=sections, referenced=referenced),
        edge_answer_model(ontology),
        prompt=EDGE_WINDOW_WARM_UP_PROMPT,
    )


async def extract_nodes_call(
    gateway: Gateway,
    ontology: Ontology,
    fragments: Sequence[str],
    known_terms: Sequence[KnownTerm],
    window: Window,
    *,
    node_list: NodeList,
) -> NodeCall:
    """One node call of the ``node_list`` pass over ``window`` of a unit (``fragments``: ``prompts.fragment_text`` of
    each).
    """

    answer = await gateway.structured(
        EXTRACTION_CLASS,
        None,
        node_messages(ontology, fragments, known_terms, window, node_list=node_list),
        node_answer_model(ontology),
        prompt=NODE_PROMPTS[node_list],
    )
    data = answer.model_dump(mode="json")
    stray = sum(len(data.get(name) or ()) for name in data if name != node_list)
    nodes: list[ExtractedNode] = []
    labels_by_name: dict[str, set[str]] = {}
    for item in data.get(node_list) or ():
        name = str(item["name"]).strip()
        if not name:
            continue
        quote = str(item["quote"]).strip() or None
        aliases = _aliases(name, item.get("aliases") or ())
        nodes.append(ExtractedNode(name, item["label"], quote, _indices(item["fragments"], window), aliases))
        labels_by_name.setdefault(_name_key(name), set()).add(item["label"])
    collapses = sum(1 for labels in labels_by_name.values() if len(labels) > 1)
    return NodeCall(nodes, collapses, stray)


async def extract_edge_window_call(
    gateway: Gateway,
    ontology: Ontology,
    fragments: Sequence[str],
    catalog: Sequence[tuple[str, str]],
    window: Window,
    *,
    sections: Mapping[str, str] | None = None,
    referenced: Mapping[str, ReferencedEntity] | None = None,
) -> EdgeCall:
    """One edge-window call over ``window`` of a unit (``prompts.edge_window_messages``) with a catalog of ``(name,
    label)`` entities (names unique and safe for a prompt; ``edges.catalog_names``); ``sections``: section hints by
    catalog name; ``referenced``: the referenced part's statements and referring sentences by catalog name.
    """

    answer = await gateway.structured(
        EXTRACTION_CLASS,
        None,
        edge_window_messages(ontology, fragments, catalog, window, sections=sections, referenced=referenced),
        edge_answer_model(ontology),
        prompt=EDGE_WINDOW_PROMPT,
    )
    return _edge_call(answer.model_dump(mode="json"), ontology, catalog, window)


def _edge_call(
    data: Mapping[str, Any],
    ontology: Ontology,
    catalog: Sequence[tuple[str, str]],
    window: Window,
) -> EdgeCall:
    """The kept facts of an edge answer and the counts of dropped ones (``EdgeCall``)."""

    known = {name for name, _ in catalog}
    relations = set(ontology.extracted_relations)
    edges: list[ExtractedEdge] = []
    unknown_names = unknown_relations = self_loops = 0
    for item in data.get(EDGES_LIST) or ():
        source, target, relation = item["source"], item["target"], item["relation"]
        if source not in known or target not in known:
            unknown_names += 1
        elif relation not in relations:
            unknown_relations += 1
        elif source == target:
            self_loops += 1
        else:
            fact = str(item["fact"]).strip()
            indices = _indices(item["fragments"], window)
            outside = not indices and bool(_numbers(item["fragments"]))
            edges.append(ExtractedEdge(relation, source, target, fact, indices, outside))
    return EdgeCall(edges, unknown_names, unknown_relations, self_loops)
