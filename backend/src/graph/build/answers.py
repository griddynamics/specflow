"""Answer models of the build's node and edge calls."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, create_model

from graph.core.errors import ConformanceError
from graph.core.ontology import Ontology

__all__ = [
    "EDGES_LIST",
    "HUBS_LIST",
    "NODE_LISTS",
    "STATEMENTS_LIST",
    "NodeList",
    "edge_answer_model",
    "node_answer_model",
    "node_lists",
]

NodeList = Literal["hubs", "statements"]
HUBS_LIST: Final[NodeList] = "hubs"
"""Answer list of the named things (the ontology's hub labels)."""
STATEMENTS_LIST: Final[NodeList] = "statements"
"""Answer list of every other extracted label (requirements, questions: statements of the text)."""
NODE_LISTS: Final[tuple[NodeList, ...]] = (HUBS_LIST, STATEMENTS_LIST)
EDGES_LIST: Final = "edges"

_ITEM_MODELS: Final[dict[NodeList, str]] = {HUBS_LIST: "HubItem", STATEMENTS_LIST: "StatementItem"}
ANSWER_MODELS: Final = 32
"""Cached answer models (a few per ontology a process loads)."""


class _AnswerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _choice(values: Sequence[str], description: str) -> Any:
    """A string field limited to ``values``; always an ``enum`` in the schema, also for one value."""

    choices: Any = Literal[tuple(values)]
    return (
        Annotated[choices, WithJsonSchema({"type": "string", "enum": list(values), "description": description})],
        ...,
    )


def _fragments(subject: str) -> Any:
    return (
        tuple[int, ...],
        Field(description=f"Numbers of the fragments within the window that {subject}."),
    )


def node_lists(ontology: Ontology, lists: Sequence[NodeList] = NODE_LISTS) -> dict[NodeList, tuple[str, ...]]:
    """Labels of each answer list a node call asks for, in ``lists`` order; a list without labels is
    left out. Hubs take the extracted hub labels, statements the other extracted labels (sorted)."""

    extracted = ontology.extracted_labels
    hubs = tuple(label for label in extracted if label in ontology.hub_labels)
    labels: dict[NodeList, tuple[str, ...]] = {
        HUBS_LIST: hubs,
        STATEMENTS_LIST: tuple(label for label in extracted if label not in hubs),
    }
    return {name: labels[name] for name in dict.fromkeys(lists) if labels[name]}


@lru_cache(maxsize=ANSWER_MODELS)
def _node_answer_model(ontology: Ontology, lists: tuple[NodeList, ...]) -> type[BaseModel]:
    chosen = node_lists(ontology, lists)
    if not chosen:
        raise ConformanceError("answers_no_node_labels", lists=",".join(lists))
    fields: dict[str, Any] = {}
    for name, labels in chosen.items():
        extra: dict[str, Any] = {}
        if name == HUBS_LIST:
            extra["aliases"] = (
                tuple[str, ...],
                Field(description="Other names the text uses for the same thing, as written; empty if none."),
            )
        item = create_model(
            _ITEM_MODELS[name],
            __base__=_AnswerModel,
            __doc__=f"One entity of the {name} list.",
            name=(str, Field(description="The entity's name, by its type's naming rule.")),
            label=_choice(labels, "The entity's type."),
            **extra,
            quote=(str, Field(description="Source text stating, naming or describing it, verbatim; empty if none.")),
            fragments=_fragments("describe or state it"),
        )
        fields[name] = (tuple[item, ...], Field(description=f"Entities of the {name} types."))
    model: type[BaseModel] = create_model(
        "NodeAnswer", __base__=_AnswerModel, __doc__="Entities extracted from the window.", **fields
    )
    return model


def node_answer_model(ontology: Ontology, lists: Sequence[NodeList] = NODE_LISTS) -> type[BaseModel]:
    """Answer model of a node call asking for ``lists`` (cached per ontology and list set)."""

    return _node_answer_model(ontology, tuple(dict.fromkeys(lists)))


@lru_cache(maxsize=ANSWER_MODELS)
def edge_answer_model(ontology: Ontology) -> type[BaseModel]:
    """Answer model of an edge call (cached per ontology); a ``ConformanceError`` when the ontology
    extracts no relation (no edge call is made then)."""

    relations = tuple(ontology.edge_models())
    if not relations:
        raise ConformanceError("answers_no_relations")
    item = create_model(
        "EdgeItem",
        __base__=_AnswerModel,
        __doc__="One fact between two listed entities.",
        source=(str, Field(description="Name of the source entity, exactly as listed.")),
        relation=_choice(relations, "The relation type."),
        target=(str, Field(description="Name of the target entity, exactly as listed.")),
        fact=(str, Field(description="The source text that states the fact, verbatim.")),
        fragments=_fragments("state it"),
    )
    fields: dict[str, Any] = {EDGES_LIST: (tuple[item, ...], Field(description="Facts stated in the window."))}
    model: type[BaseModel] = create_model(
        "EdgeAnswer", __base__=_AnswerModel, __doc__="Facts extracted from the window.", **fields
    )
    return model
