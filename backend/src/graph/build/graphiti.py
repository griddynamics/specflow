"""Graphiti adapters and call wrappers of the build (docs/graph/api.md "LLM gateway"; tech-specs 5.5).

The only module of the library that imports ``graphiti_core`` (architecture.md I2). The build calls
Graphiti's ``extract_nodes`` and ``extract_edges`` only; Graphiti gets adapters, never its own clients:

- ``GatewayLLMClient`` overrides the public ``generate_response``: exactly one ``Gateway.structured``
  call per Graphiti request (call class ``extraction``, prompt name = Graphiti's ``prompt_name``), so
  every request passes the gateway's limiter, retries, error classes, usage and untrusted-text notice.
  Graphiti's tenacity retry, response cache, tracer and error logging are never reached; only
  ``GraphError`` types leave it.
- ``GatewayEmbedder`` implements ``EmbedderClient`` on ``Gateway.embed``.
- ``OfflineDriver`` and ``NoCrossEncoder`` fail if Graphiti ever calls them (it must not).

The wrappers take and return plain values (episode texts, names, labels, indices), so no Graphiti
type leaves this module. ``render_nodes_request`` and ``render_edges_request`` return the messages a
call would send without making it (``estimate_build``): Graphiti builds them as usual, and a rendering
LLM client stops at its first request. Attribution is recomputed from the model's raw answer: an entity or fact
whose ``episode_indices`` name no valid episode gets an empty index list (Graphiti itself would
substitute every episode, which hides the missing attribution).
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, NoReturn, override

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.driver import GraphDriver, GraphDriverSession, GraphProvider
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.llm_client.config import LLMConfig as GraphitiLLMConfig
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.prompts.models import Message
from graphiti_core.tracer import NoOpTracer
from graphiti_core.utils.maintenance.dedup_helpers import _normalize_string_exact
from graphiti_core.utils.maintenance.edge_operations import extract_edges as graphiti_extract_edges
from graphiti_core.utils.maintenance.node_operations import extract_nodes as graphiti_extract_nodes
from pydantic import BaseModel

from graph.core.errors import ConformanceError, GraphError
from graph.core.llm import Gateway

__all__ = [
    "EDGES_PROMPT",
    "EPISODE_GROUP",
    "EPISODE_SOURCE",
    "EPISODE_TIME",
    "FALLBACK_PROMPT",
    "GENERIC_ENTITY_TYPE",
    "NODES_PROMPT",
    "EdgeCall",
    "ExtractedEdge",
    "ExtractedNode",
    "GatewayEmbedder",
    "GatewayLLMClient",
    "GraphitiClients",
    "NoCrossEncoder",
    "NodeCall",
    "OfflineDriver",
    "extract_edges_call",
    "extract_nodes_call",
    "graphiti_clients",
    "render_edges_request",
    "render_nodes_request",
]

GENERIC_ENTITY_TYPE: Final = "Entity"
"""Graphiti's built-in generic entity type (type id 0), never an ontology name; always excluded."""
NODES_PROMPT: Final = "extract_nodes.extract_text"
"""Graphiti's prompt name of a node-extraction request over text episodes (graphiti-core 0.30)."""
EDGES_PROMPT: Final = "extract_edges.edge"
"""Graphiti's prompt name of an edge-extraction request (graphiti-core 0.30)."""
FALLBACK_PROMPT: Final = "graphiti.request"
"""Prompt name for a Graphiti request that names none (Graphiti 0.30 always names its prompts)."""
EPISODE_GROUP: Final = "build"
EPISODE_SOURCE: Final = "fragment"
EPISODE_TIME: Final = datetime(2000, 1, 1, tzinfo=UTC)
"""Fixed episode timestamp: Graphiti prints it in episode headers, so it must not vary between runs."""


# Per-call scope: the raw answers of one wrapper call and an optional answer filter.


@dataclass
class _CallScope:
    answers: list[dict[str, Any]] = field(default_factory=list)
    sanitize: Callable[[dict[str, Any]], dict[str, Any]] | None = None


_SCOPE: contextvars.ContextVar[_CallScope | None] = contextvars.ContextVar("graph_build_graphiti_scope", default=None)


def _fail(code: str, **fields: str | int) -> NoReturn:
    raise ConformanceError(code, **fields)


@contextlib.contextmanager
def _conformance_boundary(code: str) -> Iterator[None]:
    """Let a ``GraphError`` of the block pass; turn any other failure (a bug) into
    ``ConformanceError(code, error_type=...)`` that keeps only the type name, never its text."""

    try:
        yield
    except Exception as error:  # the boundary contains every failure; a GraphError is already typed
        if isinstance(error, GraphError):
            raise
        raise ConformanceError(code, error_type=type(error).__name__) from None


# Adapters


class _NoClient:
    """Stands in for the ``AsyncOpenAI`` client Graphiti's base class would create; fails if used."""

    def __getattr__(self, name: str) -> NoReturn:
        _fail("graphiti_openai_client_used", attribute=name)


class _ChatClient(OpenAIGenericClient):
    """Graphiti LLM client base that renders the messages of a request the way Graphiti would send them."""

    def chat_messages(self, messages: list[Message], *, attribute_extraction: bool) -> list[dict[str, str]]:
        """Graphiti's messages as sent: attribute preamble applied, input cleaned the way Graphiti does."""

        self._apply_attribute_extraction_preamble(messages, attribute_extraction)
        return [{"role": message.role, "content": self._clean_input(message.content)} for message in messages]


class GatewayLLMClient(_ChatClient):
    """Graphiti LLM client whose every request is one ``Gateway.structured`` call (tech-specs 5.5)."""

    def __init__(self, gateway: Gateway) -> None:
        super().__init__(config=GraphitiLLMConfig(api_key="unused", model="gateway"), client=_NoClient())
        self.gateway = gateway

    @override
    async def generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
        *,
        attribute_extraction: bool = False,
    ) -> dict[str, Any]:
        if response_model is None:
            _fail("graphiti_unstructured_request", prompt=prompt_name or FALLBACK_PROMPT)
        chat = self.chat_messages(messages, attribute_extraction=attribute_extraction)
        with _conformance_boundary("graphiti_gateway_failed"):  # a gateway bug; its text never reaches Graphiti's logs
            answer = await self.gateway.structured(
                "extraction", None, chat, response_model, prompt=prompt_name or FALLBACK_PROMPT
            )
        result = answer.model_dump(mode="json")
        scope = _SCOPE.get()
        if scope is not None:
            scope.answers.append(result)
            if scope.sanitize is not None:
                result = scope.sanitize(result)
        return result

    @override
    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 0,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        _fail("graphiti_private_request_path")


class _RenderedError(Exception):
    """Raised by ``_RenderingLLMClient`` with the request Graphiti would send."""

    def __init__(self, messages: list[dict[str, str]]) -> None:
        super().__init__("rendered")
        self.messages = messages


class _RenderingLLMClient(_ChatClient):
    """Graphiti LLM client that records the first request and stops there (``render_*_request``):
    the messages are exactly those ``GatewayLLMClient`` would pass to the gateway, but nothing is sent."""

    def __init__(self) -> None:
        super().__init__(config=GraphitiLLMConfig(api_key="unused", model="render"), client=_NoClient())

    @override
    async def generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
        group_id: str | None = None,
        prompt_name: str | None = None,
        *,
        attribute_extraction: bool = False,
    ) -> dict[str, Any]:
        raise _RenderedError(self.chat_messages(messages, attribute_extraction=attribute_extraction))

    @override
    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 0,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        _fail("graphiti_private_request_path")


class _NoEmbedder(EmbedderClient):
    """Graphiti embedder that fails if called (rendering a request never embeds)."""

    @override
    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        _fail("graphiti_embedder_called")

    @override
    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        _fail("graphiti_embedder_called")


class GatewayEmbedder(EmbedderClient):
    """Graphiti embedder on ``Gateway.embed`` (Graphiti's node and edge extraction never embeds)."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    @override
    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        if isinstance(input_data, str):
            texts = [input_data]
        elif isinstance(input_data, list) and len(input_data) == 1 and isinstance(input_data[0], str):
            texts = [input_data[0]]
        else:
            _fail("graphiti_embedder_input_unsupported")
        return (await self.gateway.embed(texts))[0]

    @override
    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return await self.gateway.embed(input_data_list)


class NoCrossEncoder(CrossEncoderClient):
    """Graphiti cross-encoder that fails if called: the build never reranks through Graphiti."""

    @override
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        _fail("graphiti_cross_encoder_called")


class OfflineDriver(GraphDriver):
    """Graphiti graph driver that fails if called: Graphiti never touches Neo4j in a build."""

    provider = GraphProvider.NEO4J

    @override
    def execute_query(self, cypher_query_: str, **kwargs: Any) -> NoReturn:
        _fail("graphiti_driver_called", method="execute_query")

    @override
    def session(self, database: str | None = None) -> GraphDriverSession:
        _fail("graphiti_driver_called", method="session")

    @override
    def close(self) -> NoReturn:
        _fail("graphiti_driver_called", method="close")

    @override
    def delete_all_indexes(self) -> NoReturn:
        _fail("graphiti_driver_called", method="delete_all_indexes")

    @override
    async def build_indices_and_constraints(self, delete_existing: bool = False) -> NoReturn:
        _fail("graphiti_driver_called", method="build_indices_and_constraints")


def graphiti_clients(gateway: Gateway) -> GraphitiClients:
    """Graphiti's client bundle on the gateway: LLM and embedder adapters, failing driver and cross-encoder."""

    return GraphitiClients(
        driver=OfflineDriver(),
        llm_client=GatewayLLMClient(gateway),
        embedder=GatewayEmbedder(gateway),
        cross_encoder=NoCrossEncoder(),
        tracer=NoOpTracer(),
    )


def _rendering_clients() -> GraphitiClients:
    return GraphitiClients(
        driver=OfflineDriver(),
        llm_client=_RenderingLLMClient(),
        embedder=_NoEmbedder(),
        cross_encoder=NoCrossEncoder(),
        tracer=NoOpTracer(),
    )


# Call wrappers


@dataclass(frozen=True)
class ExtractedNode:
    """One node of a Graphiti ``extract_nodes`` call: ``uuid`` keys the index map, ``label`` its type name."""

    uuid: str
    name: str
    label: str


@dataclass(frozen=True)
class NodeCall:
    """Result of one ``extract_nodes`` request: typed nodes, episode indices per ``uuid`` and the
    number of names the model gave two or more labels (Graphiti keeps one node per name)."""

    nodes: list[ExtractedNode]
    index_map: dict[str, list[int]]
    label_collapses: int = 0


@dataclass(frozen=True)
class ExtractedEdge:
    """One fact of a Graphiti ``extract_edges`` call between two named catalog nodes.

    ``episode_indices`` are the valid episodes the model attributed the fact to; empty when it named none.
    """

    relation: str
    source_name: str
    target_name: str
    fact: str
    episode_indices: tuple[int, ...]


@dataclass(frozen=True)
class EdgeCall:
    """Result of one ``extract_edges`` request: the facts and the counts of facts dropped before
    Graphiti saw them (a name outside the catalog, a relation outside the edge types)."""

    edges: list[ExtractedEdge]
    unknown_names: int = 0
    unknown_relations: int = 0


def _episodes(contents: Sequence[str]) -> list[EpisodicNode]:
    if not contents:
        _fail("graphiti_no_episodes")
    return [
        EpisodicNode(
            name=f"episode {index}",
            group_id=EPISODE_GROUP,
            source=EpisodeType.text,
            source_description=EPISODE_SOURCE,
            content=content,
            valid_at=EPISODE_TIME,
            created_at=EPISODE_TIME,
        )
        for index, content in enumerate(contents)
    ]


def _valid_indices(raw: Any, episodes: int) -> list[int]:
    if not isinstance(raw, list):
        return []
    return sorted({index for index in raw if isinstance(index, int) and 0 <= index < episodes})


async def _in_scope(scope: _CallScope, call: Any) -> Any:
    token = _SCOPE.set(scope)
    try:
        with _conformance_boundary("graphiti_call_failed"):  # Graphiti failed on an answer the gateway validated
            return await call
    finally:
        _SCOPE.reset(token)


async def extract_nodes_call(
    clients: GraphitiClients,
    episodes: Sequence[str],
    entity_types: Mapping[str, type[BaseModel]],
    instructions: str,
) -> NodeCall:
    """One Graphiti ``extract_nodes`` request over ``episodes`` (texts, one episode each, in order).

    Returns the typed nodes (the generic type is excluded) and, per node ``uuid``, the sorted valid
    episode indices the model gave; an empty list means the model named no valid episode. Graphiti
    collapses equal names within the call; their indices are joined, and a name given two or more
    labels counts as one label collapse.
    """

    scope = _CallScope()
    nodes, _ = await _in_scope(
        scope,
        graphiti_extract_nodes(
            clients,
            _episodes(episodes),
            [],
            entity_types=dict(entity_types),
            excluded_entity_types=[GENERIC_ENTITY_TYPE],
            custom_extraction_instructions=instructions,
        ),
    )
    type_names = [GENERIC_ENTITY_TYPE, *entity_types]
    indices_by_name: dict[str, set[int]] = {}
    types_by_name: dict[str, set[int]] = {}
    for answer in scope.answers:
        for entity in answer.get("extracted_entities") or ():
            type_id = entity.get("entity_type_id")
            name = entity.get("name")
            if not isinstance(name, str) or not name.strip() or not isinstance(type_id, int):
                continue
            if not 0 < type_id < len(type_names):
                continue
            types_by_name.setdefault(_normalize_string_exact(name), set()).add(type_id)
            indices_by_name.setdefault(_normalize_string_exact(name), set()).update(
                _valid_indices(entity.get("episode_indices"), len(episodes))
            )
    result: list[ExtractedNode] = []
    index_map: dict[str, list[int]] = {}
    for node in nodes:
        label = next((label for label in node.labels if label != GENERIC_ENTITY_TYPE), None)
        if label is None or label not in entity_types:
            continue
        result.append(ExtractedNode(uuid=node.uuid, name=node.name, label=label))
        index_map[node.uuid] = sorted(indices_by_name.get(_normalize_string_exact(node.name), ()))
    collapses = sum(1 for type_ids in types_by_name.values() if len(type_ids) > 1)
    return NodeCall(result, index_map, collapses)


async def extract_edges_call(
    clients: GraphitiClients,
    episodes: Sequence[str],
    nodes: Sequence[tuple[str, str]],
    edge_types: Mapping[str, type[BaseModel]],
    edge_type_map: Mapping[tuple[str, str], list[str]],
    *,
    instructions: str,
) -> EdgeCall:
    """One Graphiti ``extract_edges`` request over ``episodes`` with a catalog of ``(name, label)`` nodes.

    Names must be unique (Graphiti keys nodes by exact name) and already safe for a prompt
    (``prompt_name``): Graphiti shows them in its ENTITIES list. Facts naming a node outside the
    catalog, or a relation outside ``edge_types``, are dropped (and counted) before Graphiti sees
    them, as are the model's date fields (unused), so no model output reaches Graphiti's log lines.
    """

    names = [name for name, _ in nodes]
    if len(set(names)) != len(names):
        _fail("graphiti_catalog_names_not_unique")
    known = set(names)

    def sanitize(answer: dict[str, Any]) -> dict[str, Any]:
        return {**answer, "edges": _known_edges(answer, known, edge_types)[0]}

    scope = _CallScope(sanitize=sanitize)
    catalog = [
        EntityNode(name=name, group_id=EPISODE_GROUP, labels=[GENERIC_ENTITY_TYPE, label], created_at=EPISODE_TIME)
        for name, label in nodes
    ]
    edges: list[EntityEdge] = await _in_scope(
        scope,
        graphiti_extract_edges(
            clients,
            _episodes(episodes),
            catalog,
            [],
            {key: list(value) for key, value in edge_type_map.items()},
            group_id=EPISODE_GROUP,
            edge_types=dict(edge_types),
            custom_extraction_instructions=instructions,
        ),
    )
    name_of = {node.uuid: node.name for node in catalog}
    raw_indices: dict[tuple[str, str, str, str], set[int]] = {}
    unknown_names = unknown_relations = 0
    for answer in scope.answers:
        kept, names_dropped, relations_dropped = _known_edges(answer, known, edge_types)
        unknown_names += names_dropped
        unknown_relations += relations_dropped
        for fact in kept:
            key = (fact["relation_type"], fact["source_entity_name"], fact["target_entity_name"], fact.get("fact", ""))
            raw_indices.setdefault(key, set()).update(_valid_indices(fact.get("episode_indices"), len(episodes)))
    result = []
    for edge in edges:
        source, target = name_of[edge.source_node_uuid], name_of[edge.target_node_uuid]
        indices = raw_indices.get((edge.name, source, target, edge.fact), set())
        result.append(
            ExtractedEdge(
                relation=edge.name,
                source_name=source,
                target_name=target,
                fact=edge.fact,
                episode_indices=tuple(sorted(indices)),
            )
        )
    return EdgeCall(result, unknown_names, unknown_relations)


def _known_edges(
    answer: Mapping[str, Any], known: set[str], edge_types: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], int, int]:
    """The answer's facts between catalog names with a known relation (dates cleared), and the
    numbers dropped for an unknown name and for an unknown relation."""

    kept: list[dict[str, Any]] = []
    unknown_names = unknown_relations = 0
    for edge in answer.get("edges") or ():
        if not isinstance(edge, dict):
            continue
        if edge.get("source_entity_name") not in known or edge.get("target_entity_name") not in known:
            unknown_names += 1
        elif edge.get("relation_type") not in edge_types:
            unknown_relations += 1
        else:
            kept.append({**edge, "valid_at": None, "invalid_at": None})
    return kept, unknown_names, unknown_relations


# Request rendering (estimate_build: the messages of a call without making it)


async def _rendered(call: Any, prompt: str) -> list[dict[str, str]]:
    with _conformance_boundary("graphiti_render_failed"):  # Graphiti failed before its request
        try:
            await call
        except _RenderedError as rendered:
            return rendered.messages
    _fail("graphiti_request_not_rendered", prompt=prompt)


async def render_nodes_request(
    episodes: Sequence[str], entity_types: Mapping[str, type[BaseModel]], instructions: str
) -> list[dict[str, str]]:
    """The messages ``extract_nodes_call`` would send for the same arguments, without any call."""

    return await _rendered(
        graphiti_extract_nodes(
            _rendering_clients(),
            _episodes(episodes),
            [],
            entity_types=dict(entity_types),
            excluded_entity_types=[GENERIC_ENTITY_TYPE],
            custom_extraction_instructions=instructions,
        ),
        NODES_PROMPT,
    )


async def render_edges_request(
    episodes: Sequence[str],
    nodes: Sequence[tuple[str, str]],
    edge_types: Mapping[str, type[BaseModel]],
    edge_type_map: Mapping[tuple[str, str], list[str]],
    instructions: str,
) -> list[dict[str, str]]:
    """The messages ``extract_edges_call`` would send for the same arguments, without any call
    (``nodes``: the catalog as ``(name, label)``, names unique)."""

    catalog = [
        EntityNode(name=name, group_id=EPISODE_GROUP, labels=[GENERIC_ENTITY_TYPE, label], created_at=EPISODE_TIME)
        for name, label in nodes
    ]
    return await _rendered(
        graphiti_extract_edges(
            _rendering_clients(),
            _episodes(episodes),
            catalog,
            [],
            {key: list(value) for key, value in edge_type_map.items()},
            group_id=EPISODE_GROUP,
            edge_types=dict(edge_types),
            custom_extraction_instructions=instructions,
        ),
        EDGES_PROMPT,
    )
