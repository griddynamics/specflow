"""Shared support for the graph library tests."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Hashable, Iterable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from functools import cache, lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Final, TypeVar

import httpx
import numpy as np
import pytest
import pytest_asyncio
import yaml
from pydantic import BaseModel, SecretStr, ValidationError

from graph.build import (
    BuildContext,
    BuildOptions,
    BuildResult,
    BuildSettings,
    FileCorpusSource,
    FileStores,
    open_file_stores,
    run_build,
)
from graph.build import edges as edge_extraction
from graph.build import run as build_run
from graph.build.answers import HUBS_LIST, node_lists
from graph.build.conform import DEDUP_PROMPT
from graph.build.consolidate import HUBS_PROMPT
from graph.build.hubjudge import TWINS_PROMPT
from graph.build.models import PlanArtifact
from graph.build.prompts import (
    ATTRIBUTES_PROMPT,
    EDGE_WINDOW_PROMPT,
    ENTITIES_LABEL,
    FRAGMENT_LABEL,
    KNOWN_TERMS_LABEL,
    NODE_HUBS_PROMPT,
    NODE_PROMPTS,
    NODE_STATEMENTS_PROMPT,
    NODES_LABEL,
    breadcrumb,
    window_fragments,
)
from graph.core import (
    ConfigurationError,
    Executor,
    FileSpecIdRegistry,
    Gateway,
    GraphReader,
    LLMConfig,
    Neo4jConfig,
    Ontology,
    OwnedWriter,
    UsageSnapshot,
    connect,
    ensure_schema,
    load_ontology,
)
from graph.core.config import CallClass
from graph.core.errors import LLMError
from graph.core.llm import strict_json_schema, untrusted
from graph.core.llm.usage import UsageRecorder
from graph.core.queries import CORE_DDL
from graph.core.query_files import read_ddl_file

# ScriptedGateway

STRUCTURED_CLASSES: Final = ("extraction", "small", "judge")
EMBED_PROMPT: Final = "embed"
T = TypeVar("T", bound=BaseModel)


class UnscriptedRequestError(AssertionError):
    """The test did not script an answer for this request."""


class ScriptError(AssertionError):
    """A scripted answer is unusable (does not validate as the requested output type)."""


class NoAnswer:
    """Returned by a handler to pass the request on to the answer table and the fallback."""

    def __repr__(self) -> str:
        return "NO_ANSWER"


NO_ANSWER: Final = NoAnswer()
DEFAULT_CONFIDENCE: Final = "high"
"""The judge's confidence a scripted consolidation group or dedup verdict gives unless it names one."""


@dataclass(frozen=True)
class ScriptedRequest:
    """What a key function or handler sees of one ``structured`` call."""

    call_class: str
    model: str
    prompt: str
    messages: tuple[Mapping[str, Any], ...]
    output_type: type[BaseModel]

    def contents(self, role: str | None = None) -> list[str]:
        """The string contents of the messages (of one role), in order."""

        return [
            message["content"]
            for message in self.messages
            if isinstance(message.get("content"), str) and (role is None or message.get("role") == role)
        ]

    @property
    def text(self) -> str:
        """Every message content joined with blank lines."""

        return "\n\n".join(self.contents())

    @property
    def system(self) -> str:
        return "\n\n".join(self.contents("system"))

    @property
    def user(self) -> str:
        """The user messages joined with blank lines ("" when there is none): a node or edge call's
        unit message and the message after it read as one."""

        return "\n\n".join(self.contents("user"))

    def user_json(self) -> Any:
        """The user message parsed as JSON (for prompts whose payload is JSON)."""

        return json.loads(self.user)


@dataclass(frozen=True)
class RecordedCall:
    """One call the library made: ``texts`` for embeddings, ``messages``/``key`` for structured calls."""

    call_class: str
    prompt: str
    model: str
    messages: tuple[Mapping[str, Any], ...] = ()
    output_type: type[BaseModel] | None = None
    key: Hashable = None
    texts: tuple[str, ...] = ()


KeyFunction = Callable[[ScriptedRequest], Hashable]
Handler = Callable[[ScriptedRequest], Any]


def _default_key(_: ScriptedRequest) -> Hashable:
    return None


class ScriptedGateway:
    """Drop-in for ``graph.core.Gateway`` answering from hand-written data."""

    untrusted = staticmethod(untrusted)

    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        answers: Mapping[str, Mapping[Hashable, Any]] | None = None,
        keys: Mapping[str, KeyFunction] | None = None,
        handlers: Mapping[str, Handler] | None = None,
        fallback: Handler | None = None,
        synonyms: Iterable[Iterable[str]] = (),
        rename: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config or llm_config()
        self._answers: dict[str, dict[Hashable, Any]] = {
            prompt: dict(table) for prompt, table in (answers or {}).items()
        }
        self._keys: dict[str, KeyFunction] = dict(keys or {})
        self._handlers: dict[str, Handler] = dict(handlers or {})
        self._fallback = fallback
        self._synonym_of: dict[str, str] = {}
        for group in synonyms:
            self.synonyms(*group)
        self._related: list[tuple[Callable[[str], bool], str, float]] = []
        self.rename: dict[str, str] = dict(rename or {})
        self.calls: list[RecordedCall] = []
        self.warm_ups: list[RecordedCall] = []
        self.unscripted: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.max_in_flight_by_class: dict[str, int] = {}
        self._in_flight_by_class: dict[str, int] = {}
        self._usage = UsageRecorder()
        self.closed = False

    # Scripting

    def answer(self, prompt: str, key: Hashable, value: Any) -> ScriptedGateway:
        """Answer requests of ``prompt`` whose key is ``key`` with ``value``."""

        self._answers.setdefault(prompt, {})[key] = value
        return self

    def key_by(self, prompt: str, key_function: KeyFunction) -> ScriptedGateway:
        """Key requests of ``prompt`` with ``key_function`` (semantic key, never the raw prompt text)."""

        self._keys[prompt] = key_function
        return self

    def handle(self, prompt: str, handler: Handler) -> ScriptedGateway:
        self._handlers[prompt] = handler
        return self

    def fallback(self, handler: Handler | None) -> ScriptedGateway:
        self._fallback = handler
        return self

    def synonyms(self, *texts: str) -> ScriptedGateway:
        """Give ``texts`` one shared embedding vector (that of the first text)."""

        if texts:
            canonical = self._synonym_of.get(texts[0], texts[0])
            for text in texts:
                self._synonym_of[text] = canonical
        return self

    def relate(
        self,
        anchor: str,
        cosine: float,
        *,
        texts: Iterable[str] = (),
        matching: Callable[[str], bool] | None = None,
    ) -> ScriptedGateway:
        """Give each of ``texts`` (and every embedded text ``matching`` accepts) a vector whose cosine to ``anchor``'s
        vector is ``cosine``.
        """

        if not -1.0 < cosine < 1.0:
            raise ScriptError(f"related cosine {cosine} must be inside (-1, 1)")
        exact = frozenset(texts)
        if exact:
            self._related.append((exact.__contains__, anchor, float(cosine)))
        if matching is not None:
            self._related.append((matching, anchor, float(cosine)))
        return self

    # Gateway surface

    @property
    def config(self) -> LLMConfig:
        return self._config

    def usage(self) -> UsageSnapshot:
        return self._usage.snapshot()

    async def aclose(self) -> None:
        self.closed = True

    async def structured(
        self,
        call_class: CallClass,
        model: str | None,
        messages: Sequence[Mapping[str, Any]],
        output_type: type[T],
        *,
        prompt: str,
    ) -> T:
        if call_class not in STRUCTURED_CLASSES:
            raise ConfigurationError("llm_call_class_invalid", call_class=str(call_class))
        request = ScriptedRequest(
            call_class=call_class,
            model=model or self._config.model_for(call_class),
            prompt=prompt,
            messages=tuple(copy.deepcopy(dict(message)) for message in messages),
            output_type=output_type,
        )
        key = self._keys.get(prompt, _default_key)(request)
        self.calls.append(
            RecordedCall(
                call_class=call_class,
                prompt=prompt,
                model=request.model,
                messages=request.messages,
                output_type=output_type,
                key=key,
            )
        )
        async with self._track(call_class, prompt) as outcome:
            answer = await self._resolve(request, key)
            result = self._coerce(answer, output_type, prompt, key)
            self._usage.tokens(
                call_class,
                prompt,
                _token_estimate(request.text),
                _token_estimate(result.model_dump_json()),
            )
            outcome["failed"] = False
            return result

    async def warm_up(
        self,
        call_class: CallClass,
        model: str | None,
        messages: Sequence[Mapping[str, Any]],
        output_type: type[BaseModel],
        *,
        prompt: str,
    ) -> None:
        """A prompt-cache warm-up: never scripted, recorded in ``warm_ups`` (not ``calls``) and in the
        usage under ``prompt`` (input tokens estimated, no output)."""

        if call_class not in STRUCTURED_CLASSES:
            raise ConfigurationError("llm_call_class_invalid", call_class=str(call_class))
        request = ScriptedRequest(
            call_class=call_class,
            model=model or self._config.model_for(call_class),
            prompt=prompt,
            messages=tuple(copy.deepcopy(dict(message)) for message in messages),
            output_type=output_type,
        )
        self.warm_ups.append(
            RecordedCall(
                call_class=call_class,
                prompt=prompt,
                model=request.model,
                messages=request.messages,
                output_type=output_type,
            )
        )
        async with self._track(call_class, prompt) as outcome:
            self._usage.tokens(call_class, prompt, _token_estimate(request.text), 0)
            outcome["failed"] = False

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        items = tuple(texts)
        self.calls.append(
            RecordedCall(
                call_class="embedding",
                prompt=EMBED_PROMPT,
                model=self._config.embedding_model,
                texts=items,
            )
        )
        async with self._track("embedding", EMBED_PROMPT) as outcome:
            vectors = [self.vector(text) for text in items]
            self._usage.tokens("embedding", EMBED_PROMPT, sum(_token_estimate(t) for t in items), 0)
            outcome["failed"] = False
            return vectors

    # Assertion helpers

    def vector(self, text: str) -> list[float]:
        """The deterministic unit vector of ``text`` (shared by its synonyms; at the declared cosines to
        the anchors it is related to, ``relate``)."""

        dimension = self._config.embedding_dim
        text = self._unrenamed(text)
        own = _unit_vector(self._synonym_of.get(text, text), dimension)
        anchors = tuple(
            (self._synonym_of.get(anchor, anchor), cosine)
            for accepts, anchor, cosine in self._related
            if anchor != text and accepts(text)
        )
        if not anchors:
            return list(own)
        return list(_related_vector(own, anchors, dimension))

    def calls_for(self, prompt: str | None = None, call_class: str | None = None) -> list[RecordedCall]:
        return [
            call
            for call in self.calls
            if (prompt is None or call.prompt == prompt) and (call_class is None or call.call_class == call_class)
        ]

    def keys_for(self, prompt: str) -> list[Hashable]:
        return [call.key for call in self.calls_for(prompt)]

    # Plumbing

    def _unrenamed(self, text: str) -> str:
        """``text`` with a leading renamed name (before ``": "``) put back to its original name."""

        if not self.rename:
            return text
        head, separator, rest = text.partition(": ")
        if not separator:
            return text
        original = {new: old for old, new in self.rename.items()}.get(head)
        return text if original is None else f"{original}{separator}{rest}"

    async def _resolve(self, request: ScriptedRequest, key: Hashable) -> Any:
        handler = self._handlers.get(request.prompt)
        if handler is not None:
            answer = await _call(handler, request)
            if answer is not NO_ANSWER:
                return answer
        table = self._answers.get(request.prompt, {})
        try:
            if key in table:
                return table[key]
        except TypeError as error:
            raise ScriptError(f"key of prompt {request.prompt!r} is not hashable: {key!r}") from error
        if self._fallback is not None:
            answer = await _call(self._fallback, request)
            if answer is not NO_ANSWER:
                return answer
        known = sorted(map(repr, table))
        message = (
            f"no scripted answer for prompt {request.prompt!r} ({request.call_class}) key {key!r}; "
            f"scripted keys: {known or 'none'}"
        )
        self.unscripted.append(message)
        raise UnscriptedRequestError(message)

    def _coerce(self, answer: Any, output_type: type[BaseModel], prompt: str, key: Hashable) -> BaseModel:
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, type) and issubclass(answer, BaseException):
            raise answer()
        if isinstance(answer, output_type) and not self.rename:
            return answer
        data = answer.model_dump(mode="json") if isinstance(answer, BaseModel) else answer
        data = _renamed(copy.deepcopy(data), self.rename)
        try:
            return output_type.model_validate(data)
        except ValidationError as error:
            raise ScriptError(
                f"scripted answer for prompt {prompt!r} key {key!r} is not a valid {output_type.__name__}: {error}"
            ) from None

    def _track(self, call_class: str, prompt: str) -> _CallTracker:
        return _CallTracker(self, call_class, prompt)


class _CallTracker:
    """In-flight counting and usage around one call."""

    def __init__(self, gateway: ScriptedGateway, call_class: str, prompt: str) -> None:
        self._gateway = gateway
        self._call_class = call_class
        self._prompt = prompt
        self._started = 0.0
        self._outcome: dict[str, bool] = {"failed": True}

    async def __aenter__(self) -> dict[str, bool]:
        gateway = self._gateway
        gateway.in_flight += 1
        gateway.max_in_flight = max(gateway.max_in_flight, gateway.in_flight)
        count = gateway._in_flight_by_class.get(self._call_class, 0) + 1
        gateway._in_flight_by_class[self._call_class] = count
        gateway.max_in_flight_by_class[self._call_class] = max(
            gateway.max_in_flight_by_class.get(self._call_class, 0), count
        )
        gateway._usage.request(self._call_class, self._prompt)
        self._started = time.monotonic()
        # Always yield to the loop once, so concurrent callers really overlap.
        await asyncio.sleep(0)
        return self._outcome

    async def __aexit__(self, *exc_info: Any) -> None:
        gateway = self._gateway
        gateway.in_flight -= 1
        gateway._in_flight_by_class[self._call_class] -= 1
        gateway._usage.call(
            self._call_class,
            self._prompt,
            duration_s=time.monotonic() - self._started,
            failed=self._outcome["failed"],
        )


async def _call(handler: Handler, request: ScriptedRequest) -> Any:
    answer = handler(request)
    if inspect.isawaitable(answer):
        answer = await answer
    return answer


def _renamed(value: Any, rename: Mapping[str, str]) -> Any:
    """``value`` with every string and mapping key exactly equal to a ``rename`` key replaced."""

    if not rename:
        return value
    if isinstance(value, str):
        return rename.get(value, value)
    if isinstance(value, Mapping):
        return {_renamed(k, rename): _renamed(v, rename) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_renamed(item, rename) for item in value]
    return value


def _token_estimate(text: str) -> int:
    return (len(text) + 3) // 4


@lru_cache(maxsize=4096)
def _unit_vector(text: str, dimension: int) -> tuple[float, ...]:
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    vector = np.random.default_rng(seed).standard_normal(dimension)
    return tuple(float(x) for x in vector / np.linalg.norm(vector))


@lru_cache(maxsize=4096)
def _related_vector(
    own: tuple[float, ...], anchors: tuple[tuple[str, float], ...], dimension: int
) -> tuple[float, ...]:
    """``own`` turned towards ``anchors``: ``sum(c_i e_i) + sqrt(1 - sum(c_i^2)) w``, with ``e_i`` the
    anchors' vectors orthonormalised in order and ``w`` the part of ``own`` orthogonal to all of them."""

    weight = sum(cosine * cosine for _, cosine in anchors)
    if weight >= 1.0:
        raise ScriptError(f"related cosines {[cosine for _, cosine in anchors]} square-sum to {weight:.3f} >= 1")
    basis: list[np.ndarray] = []
    for anchor, _ in anchors:
        vector = np.asarray(_unit_vector(anchor, dimension))
        for done in basis:
            vector = vector - float(vector @ done) * done
        basis.append(vector / np.linalg.norm(vector))
    rest = np.asarray(own)
    for done in basis:
        rest = rest - float(rest @ done) * done
    rest = rest / np.linalg.norm(rest)
    result = sum(cosine * done for (_, cosine), done in zip(anchors, basis, strict=True)) + np.sqrt(1.0 - weight) * rest
    return tuple(float(x) for x in result / np.linalg.norm(result))


def assert_all_scripted(*gateways: ScriptedGateway) -> None:
    """Fail the test when any gateway got a request nobody scripted (``unscripted`` not empty)."""

    problems = [message for gateway in gateways for message in gateway.unscripted]
    if problems:
        pytest.fail(
            "unscripted LLM requests (the library may have swallowed the error):\n"
            + "\n".join(f"- {message}" for message in problems),
            pytrace=False,
        )


@pytest.fixture
def scripted_gateway() -> Iterator[Callable[..., ScriptedGateway]]:
    """Factory ``scripted_gateway(config=None, **options) -> ScriptedGateway``."""

    made: list[ScriptedGateway] = []

    def factory(config: LLMConfig | None = None, **options: Any) -> ScriptedGateway:
        gateway = ScriptedGateway(config, **options)
        made.append(gateway)
        return gateway

    yield factory
    assert_all_scripted(*made)


# Configs

OFFLINE_EMBEDDING_DIM: Final = 16


def llm_config(**overrides: Any) -> LLMConfig:
    """An ``LLMConfig`` for offline tests; nothing is ever sent to its endpoint."""

    values: dict[str, Any] = {
        "endpoint": "http://llm.invalid/v1",
        "api_key": SecretStr("test-key-not-secret"),
        "main_model": "test-main",
        "small_model": "test-small",
        "embedding_model": "test-embedding",
        "embedding_dim": OFFLINE_EMBEDDING_DIM,
    }
    values.update(overrides)
    return LLMConfig(**values)


# Real ``Gateway`` on an ``httpx.MockTransport`` (offline gateway and judge tests)

TEST_API_KEY: Final = "sk-test-secret-0123456789"
GATEWAY_EMBEDDING_DIM: Final = 3


def gateway_config(**overrides: Any) -> LLMConfig:
    """The ``LLMConfig`` of the mock-transport gateway tests: models ``main``/``small``/``emb``."""

    values: dict[str, Any] = {
        "endpoint": "http://proxy.test/v1",
        "api_key": TEST_API_KEY,
        "main_model": "main",
        "small_model": "small",
        "embedding_model": "emb",
        "embedding_dim": GATEWAY_EMBEDDING_DIM,
    }
    values.update(overrides)
    return LLMConfig(**values)


def chat(
    content: str | None,
    *,
    finish: str = "stop",
    refusal: str | None = None,
    usage: tuple[int, int] | None = (10, 5),
) -> dict[str, Any]:
    """A Chat Completions response body with one choice."""

    body: dict[str, Any] = {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        }
    return body


def ok(content: str) -> httpx.Response:
    """A 200 Chat Completions response answering ``content``."""

    return httpx.Response(200, json=chat(content))


def error(status: int, message: str = "boom", code: str | None = None, **headers: str) -> httpx.Response:
    """An OpenAI-style error response."""

    return httpx.Response(status, json={"error": {"message": message, "code": code}}, headers=headers)


GatewayFactory = Callable[..., tuple[Gateway, list[float]]]


@pytest_asyncio.fixture
async def make_gateway() -> AsyncIterator[GatewayFactory]:
    """Factory ``make_gateway(handler, *, sleep=None, jitter=0.0, **config) -> (gateway, sleeps)``."""

    made: list[Gateway] = []

    def factory(
        handler: Callable[[httpx.Request], Any],
        *,
        sleep: Callable[[float], Any] | None = None,
        jitter: float = 0.0,
        **overrides: Any,
    ) -> tuple[Gateway, list[float]]:
        sleeps: list[float] = []

        async def instant(seconds: float) -> None:
            sleeps.append(seconds)
            if sleep is not None:
                await sleep(seconds)

        gateway = Gateway(
            gateway_config(**overrides),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            sleep=instant,
            jitter=lambda: jitter,
        )
        made.append(gateway)
        return gateway, sleeps

    yield factory
    for gateway in made:
        await gateway.aclose()


def live_embedding_dim() -> int:
    """The dimension of the existing vector indexes of the local Neo4j (``GRAPH_EMBEDDING_DIM``)."""

    return int(os.environ.get("GRAPH_EMBEDDING_DIM", "3072"))


def live_llm_config() -> LLMConfig:
    """An ``LLMConfig`` whose ``embedding_dim`` matches the local Neo4j vector indexes."""

    return llm_config(embedding_dim=live_embedding_dim())


# Ontologies

# The one hand-written submodule file of the tests, owned by ``gap``: a foreign node type and two
# foreign relations. ``FLAGS`` ends at build-owned labels (the ``requirement`` group, ``module``) and
# also joins two modules; ``CITES`` ends at the built-in ``Fragment``. Tests deep-copy it to mutate.
GAP_ONTOLOGY: Final[dict[str, Any]] = {
    "format_version": 2,
    "name": "gap-test",
    "version": "0.1",
    "owner": "gap",
    "node_types": {
        "finding": {
            "label": "Finding",
            "origin": "analysis",
            "tier": "governance",
            "description": "A gap finding.",
        },
    },
    "edge_types": {
        "FLAGS": {
            "origin": "analysis",
            "tier": "governance",
            "description": "A finding flags a requirement or a module; a module flags a module.",
            "pairs": [
                {"from": "finding", "to": "requirement"},
                {"from": "finding", "to": "module"},
                {"from": "module", "to": "module"},
            ],
        },
        "CITES": {
            "origin": "analysis",
            "tier": "governance",
            "description": "A finding cites a fragment.",
            "pairs": [{"from": "finding", "to": "Fragment"}],
        },
    },
}


@lru_cache(maxsize=1)
def shipped_ontology() -> Ontology:
    """The shipped build ontology alone (no submodule files)."""

    return load_ontology(submodule_files=[])


FROZEN_ONTOLOGY_FILE: Final = Path(__file__).resolve().parent / "fixtures" / "graph" / "ontology-v1.3-f2.yaml"
"""Flag-off ontology used by mechanics tests. It names shipped judge prompts (``FROZEN_PROMPT_REFS``); load it
with ``load_frozen_ontology``."""
SHIPPED_PROMPTS: Final = Path(str(files("graph") / "core" / "ontology" / "judge_prompts"))
"""The shipped judge prompts."""
FROZEN_PROMPT_REFS: Final = ("judge_endpoints", "judge_realization", "judge_attach_orphan")
"""The shipped judge prompts the frozen ontology names; ``copy_frozen_prompts`` copies only these."""


def copy_frozen_prompts(directory: Path) -> Path:
    """Write ``directory/judge_prompts/`` holding the frozen ontology's judge prompts (``FROZEN_PROMPT_REFS``)
    and return it."""

    prompts = directory / "judge_prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    for ref in FROZEN_PROMPT_REFS:
        shutil.copy(SHIPPED_PROMPTS / f"{ref}.md", prompts / f"{ref}.md")
    return prompts


def load_frozen_ontology(submodule_files: Sequence[Path] = ()) -> Ontology:
    """Load ``FROZEN_ONTOLOGY_FILE`` with its judge prompts beside it, in a temporary directory."""

    with tempfile.TemporaryDirectory(prefix="frozen-ontology-") as name:
        directory = Path(name)
        shutil.copy(FROZEN_ONTOLOGY_FILE, directory / FROZEN_ONTOLOGY_FILE.name)
        copy_frozen_prompts(directory)
        return load_ontology(directory / FROZEN_ONTOLOGY_FILE.name, submodule_files=list(submodule_files))


@lru_cache(maxsize=1)
def frozen_ontology() -> Ontology:
    """The flag-off ontology alone (no submodule files)."""

    return load_frozen_ontology()


def with_v2_flags(data: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the flag-off ontology ``data`` with every format-2 flag set."""

    data = copy.deepcopy(dict(data))
    data["node_groups"]["requirement"]["attributes"]["acceptance_criteria"]["inherit"] = {"via": "PART_OF"}
    types = data["node_types"]
    types["module"]["attributes"]["external"]["default"] = "no"
    types["capability"]["merge_across_labels"] = False
    types["capability"]["supported_by"] = [
        {"relation": "REALIZED_BY", "direction": "out"},
        {"relation": "GROUPED_BY", "direction": "in"},
    ]
    relations = data["edge_types"]
    part_of = relations["PART_OF"]["pairs"]
    part_of[0].update(owner=True, owner_rank=1)
    for pair in part_of[1:7]:
        pair.update(owner=True, owner_rank=2)
    part_of[1].update(target_where={"external": ["no"]}, otherwise="RELATES_TO", where_reason="external_owner")
    part_of[9].update(
        default=True, cue_required=False, max_depth=1, not_across=["external"], target_where={"type": ["deployment"]}
    )
    relations["INTEGRATES_WITH"]["pairs"][0]["target_where"] = {"type": ["backend", "datastore"]}
    relations["INTEGRATES_WITH"]["derive_via"] = {"consumer": "REALIZED_BY", "provider": "REALIZED_BY"}
    relations["RELATES_TO"]["pairs"][0]["distinct_owner"] = True
    relations["GROUPED_BY"]["pairs"][1]["redundant_via_owner"] = True
    relations["GROUPED_BY"].update(acyclic=True, stated_only=True)
    data["spine"]["exclude"] = {"external": ["yes"]}  # declared on the sources' owner (Module) only
    data["conform"] = {"judge_prompt": "judge_endpoints", "cap": 10, "floor": 0.5, "fallback_relation": "RELATES_TO"}
    rules = data["inference"]["rules"]
    rules[0]["exclude_owned_by"] = ["feature"]
    rules[1]["container_paths"].append({"via": ["RELATES_TO"], "min": 1, "max": 1})
    rules[2]["exclude_where"] = {"is_assumption": ["yes"]}
    return data


# The renamed-feature ontology (FR-MCP-TOOL-06): the shipped one with only the ``feature`` label and id prefix changed.
RENAMED_FEATURE_LABEL: Final = "Funktion"
RENAMED_FEATURE_PREFIX: Final = "FUNK::"


def write_feature_renamed_ontology(
    directory: Path, *, label: str = RENAMED_FEATURE_LABEL, id_prefix: str = RENAMED_FEATURE_PREFIX
) -> Path:
    """A copy of the shipped ontology whose ``feature`` type keeps its key but gets ``label`` and ``id_prefix``,
    with the judge prompts it names, written into ``directory``; return the YAML file."""

    source = files("graph") / "core" / "ontology"
    data = yaml.safe_load((source / "spec-ontology.yaml").read_text(encoding="utf-8"))
    feature = data["node_types"]["feature"]
    old_prefix = shipped_ontology().node_types["feature"].id_prefix
    assert feature["id"]["format"].startswith(old_prefix)
    feature["label"] = label
    feature["id"]["format"] = id_prefix + feature["id"]["format"].removeprefix(old_prefix)
    path = directory / "feature-renamed-ontology.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    prompts = directory / "judge_prompts"
    prompts.mkdir(exist_ok=True)
    for prompt in (source / "judge_prompts").iterdir():
        if prompt.name.endswith(".md"):
            (prompts / prompt.name).write_bytes(prompt.read_bytes())
    return path


def load_feature_renamed_ontology(
    directory: Path, *, label: str = RENAMED_FEATURE_LABEL, id_prefix: str = RENAMED_FEATURE_PREFIX
) -> Ontology:
    """``write_feature_renamed_ontology`` into ``directory``, loaded."""

    path = write_feature_renamed_ontology(directory, label=label, id_prefix=id_prefix)
    return load_ontology(path, submodule_files=[])


def load_gap_ontology(directory: Path) -> Ontology:
    """The shipped ontology merged with ``GAP_ONTOLOGY`` written into ``directory``."""

    path = Path(directory) / "gap-ontology.yaml"
    path.write_text(yaml.safe_dump(GAP_ONTOLOGY, sort_keys=False), encoding="utf-8")
    return load_ontology(submodule_files=[path])


@pytest.fixture(scope="session")
def gap_ontology(tmp_path_factory: pytest.TempPathFactory) -> Ontology:
    return load_gap_ontology(tmp_path_factory.mktemp("gap-ontology"))


# Schema DDL of ``GAP_ONTOLOGY``: a submodule ontology brings the DDL of what it declares, the edge-key
# range index of each of its relations and the (group_id, uuid) constraint of each foreign label.
GAP_DDL_TEXT: Final = """// Schema DDL of the gap test ontology (applied after the shipped DDL).
CREATE INDEX graph_edge_key_cites IF NOT EXISTS FOR ()-[r:CITES]-() ON (r.group_id, r.edge_key);
CREATE INDEX graph_edge_key_flags IF NOT EXISTS FOR ()-[r:FLAGS]-() ON (r.group_id, r.edge_key);
CREATE CONSTRAINT graph_foreign_uuid_finding IF NOT EXISTS
  FOR (n:Finding) REQUIRE (n.group_id, n.uuid) IS UNIQUE;
"""


def load_gap_ddl(directory: Path) -> tuple[str, ...]:
    """The shipped DDL statements followed by those of ``GAP_DDL_TEXT``, written into ``directory``."""

    path = Path(directory) / "gap-ddl.cypher"
    path.write_text(GAP_DDL_TEXT, encoding="utf-8")
    return (*CORE_DDL, *read_ddl_file(path))


# Offline executor


class OfflineExecutor:
    """Stands in for ``graph.core.Executor`` where a test must not reach Neo4j: ``read`` answers from
    ``rows`` by query name (no rows by default) and records ``(query name, project id)``; every write
    path fails the test."""

    def __init__(self, rows: Mapping[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {name: list(found) for name, found in (rows or {}).items()}
        self.reads: list[tuple[str, str]] = []

    async def read(self, query: Any, project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        self.reads.append((query.name, project_id))
        return copy.deepcopy(self.rows.get(query.name, []))

    async def write(self, query: Any, _project_id: str, /, **_params: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"offline test wrote with {query.name}")

    def tx(self, _project_id: str) -> Any:
        raise AssertionError("offline test opened a write transaction")


# File ports


@dataclass(frozen=True)
class CoreFileStores:
    """The core file ports under one root (build adds its own stores in a later milestone)."""

    root: Path
    spec_ids: FileSpecIdRegistry


def open_core_file_stores(root: Path) -> CoreFileStores:
    root = Path(root)
    return CoreFileStores(root=root, spec_ids=FileSpecIdRegistry(root / "spec-ids"))


@pytest.fixture
def file_stores(tmp_path: Path) -> CoreFileStores:
    return open_core_file_stores(tmp_path / "stores")


# Live Neo4j

PROJECT_PREFIX: Final = "pytest-graph-"
_SCHEMA_READY: set[tuple[str, str | None]] = set()


def neo4j_config_from_env() -> Neo4jConfig:
    return Neo4jConfig(
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        user=os.environ.get("NEO4J_USER", "neo4j"),
        password=SecretStr(os.environ.get("NEO4J_PASSWORD", "")),
        database=os.environ.get("NEO4J_DATABASE") or None,
    )


def new_project_id() -> str:
    return f"{PROJECT_PREFIX}{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class GraphSnapshot:
    """Every node and relationship touching a project, canonical and comparable."""

    nodes: tuple[str, ...]
    relationships: tuple[str, ...]


@dataclass
class Neo4jProject:
    """One test's view of the local Neo4j: a project id (more on request) and helpers."""

    driver: Any
    database: str | None
    ontology: Ontology
    project_id: str
    project_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.project_id not in self.project_ids:
            self.project_ids.append(self.project_id)

    def new_project(self) -> str:
        """Another unique project id of this test, cleaned up with the first."""

        project_id = new_project_id()
        self.project_ids.append(project_id)
        return project_id

    @property
    def executor(self) -> Executor:
        return Executor(self.driver, self.database)

    def writer(
        self, owner: str = "build", *, project_id: str | None = None, ontology: Ontology | None = None
    ) -> OwnedWriter:
        return OwnedWriter(
            self.driver,
            ontology or self.ontology,
            owner,
            project_id or self.project_id,
            database=self.database,
        )

    def reader(self, *, ontology: Ontology | None = None, gateway: Any = None) -> GraphReader:
        return GraphReader(self.driver, ontology or self.ontology, gateway, database=self.database)

    async def run(self, text: str, /, **params: Any) -> list[dict[str, Any]]:
        """Raw Cypher for test setup and assertions (the library never sees it)."""

        result = await self.driver.execute_query(text, params, database_=self.database)
        return [dict(record) for record in result.records]

    async def snapshot(self, project_id: str | None = None) -> GraphSnapshot:
        """Nodes (labels, properties) and relationships (type, ends, properties) of one project."""

        project_id = project_id or self.project_id
        nodes = await self.run(
            "MATCH (n) WHERE n.group_id = $p RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props",
            p=project_id,
        )
        relationships = await self.run(
            "MATCH (s)-[r]->(t) WHERE s.group_id = $p OR t.group_id = $p OR r.group_id = $p "
            "RETURN elementId(s) AS s, type(r) AS type, elementId(t) AS t, properties(r) AS props",
            p=project_id,
        )
        return GraphSnapshot(
            nodes=tuple(sorted(_canonical({**row, "labels": sorted(row["labels"])}) for row in nodes)),
            relationships=tuple(sorted(_canonical(row) for row in relationships)),
        )

    async def count(self, project_id: str | None = None) -> int:
        rows = await self.run("MATCH (n) WHERE n.group_id = $p RETURN count(n) AS n", p=project_id or self.project_id)
        return int(rows[0]["n"])

    async def cleanup(self) -> int:
        """Delete everything of this test's project ids; return what is left (0 expected)."""

        await self.run("MATCH (n) WHERE n.group_id IN $ids DETACH DELETE n", ids=self.project_ids)
        rows = await self.run("MATCH (n) WHERE n.group_id IN $ids RETURN count(n) AS n", ids=self.project_ids)
        return int(rows[0]["n"])


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def tree_hash(root: Path) -> str:
    """sha256 over every file below ``root`` (relative path and bytes): equal hashes mean nothing was
    written there (stores, build records, artifacts, corpus index)."""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


async def ensure_session_schema(driver: Any, database: str | None) -> None:
    """Run ``ensure_schema`` with the shipped ontology once per pytest session and database."""

    marker = (os.environ.get("NEO4J_URI", ""), database)
    if marker in _SCHEMA_READY:
        return
    await ensure_schema(driver, shipped_ontology(), live_llm_config(), database)
    _SCHEMA_READY.add(marker)


# What a test may never touch on the shared dev database: nodes without a project, spec nodes
# documents and fragments of projects that are not test projects, and relationships not between two
# test-project nodes (a test's own relationships are deleted with its nodes before the second count).
# Counted before and after every live test.
OUTSIDE_TEST_PROJECTS: Final = (
    "CALL () { MATCH (n) WHERE n.group_id IS NULL RETURN count(n) AS unscoped } "
    "CALL () { MATCH (s:SpecNode) WHERE s.group_id IS NULL OR NOT s.group_id STARTS WITH $prefix "
    "RETURN count(s) AS foreign_spec_nodes } "
    "CALL () { MATCH (d:Document) WHERE d.group_id IS NULL OR NOT d.group_id STARTS WITH $prefix "
    "RETURN count(d) AS foreign_documents } "
    "CALL () { MATCH (f:Fragment) WHERE f.group_id IS NULL OR NOT f.group_id STARTS WITH $prefix "
    "RETURN count(f) AS foreign_fragments } "
    "CALL () { MATCH (a)-[r]->(b) "
    "WHERE NOT (coalesce(a.group_id, '') STARTS WITH $prefix AND coalesce(b.group_id, '') STARTS WITH $prefix) "
    "RETURN count(r) AS foreign_relationships } "
    "RETURN unscoped, foreign_spec_nodes, foreign_documents, foreign_fragments, foreign_relationships"
)


async def outside_counts(driver: Any, database: str | None) -> dict[str, int]:
    result = await driver.execute_query(OUTSIDE_TEST_PROJECTS, {"prefix": PROJECT_PREFIX}, database_=database)
    return {key: int(value) for key, value in dict(result.records[0]).items()}


async def spine_unreachable_count(project: Neo4jProject, project_id: str, bases: Sequence[str] | None = None) -> int:
    """Spine sources of a project that reach no spine root by a spine path, counted by Cypher independently of
    ``graph.build.spine`` (shipped ontology: ``PART_OF`` 1-3 hops, ``GROUPED_BY`` then ``REALIZED_BY``, or one
    ``RELATES_TO`` in either direction, to a ``Module``).
    """

    spine = project.ontology.spine
    assert spine.roots == {"Module"}, "the query below is written for the shipped spine"
    walked = "($bases IS NULL OR all(r IN relationships(path) WHERE coalesce(r.basis, 'stated') IN $bases))"
    rows = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p AND any(l IN labels(n) WHERE l IN $sources) "
        f"AND NOT EXISTS {{ MATCH path = (n)-[:PART_OF*1..3]->(:Module) WHERE {walked} }} "
        f"AND NOT EXISTS {{ MATCH path = (n)-[:GROUPED_BY]->(:SpecNode)-[:REALIZED_BY]->(:Module) WHERE {walked} }} "
        f"AND NOT EXISTS {{ MATCH path = (n)-[:RELATES_TO]-(:Module) WHERE {walked} }} "
        "RETURN count(n) AS n",
        p=project_id,
        sources=sorted(spine.sources),
        bases=list(bases) if bases is not None else None,
    )
    return int(rows[0]["n"])


def live_ontology() -> Ontology:
    """Ontology of a live shop project. Answers target the flag-off fixture, not the shipped ontology."""

    return frozen_ontology()


@pytest_asyncio.fixture
async def neo4j_project() -> AsyncIterator[Neo4jProject]:
    """Tier-2 fixture (its tests carry ``requires_live("NEO4J")``): unique project ids, cleaned up after."""

    config = neo4j_config_from_env()
    driver = connect(config)
    project = Neo4jProject(
        driver=driver, database=config.database, ontology=live_ontology(), project_id=new_project_id()
    )
    try:
        await ensure_session_schema(driver, config.database)
        before = await outside_counts(driver, config.database)
        yield project
    finally:
        try:
            left = await project.cleanup()
            after = await outside_counts(driver, config.database)
        finally:
            await driver.close()
    assert left == 0, f"{left} nodes of {project.project_ids} left after cleanup"
    assert after == before, f"data outside the test projects changed: {before} -> {after}"


# Shop corpus and its hand-written answers

SHOP_DIR: Final = Path(__file__).resolve().parent / "fixtures" / "graph" / "shop"
# Prompt names (``NODE_HUBS_PROMPT``, ``EDGE_WINDOW_PROMPT``,...) and block labels are the library's constants
# imported above; test modules import them from the library too.
FRAGMENT_BLOCK_LABEL: Final = re.compile(re.escape(FRAGMENT_LABEL).replace(re.escape("{number}"), r"(\d+)"))
"""A fragment block label of a node or edge request (``fragment <n>``), its number captured."""

_UNTRUSTED_BLOCK: Final = re.compile(r"<<<DOCUMENT ([^>\n]*)>>>\n(.*?)\n<<<END>>>", re.DOTALL)
_ATTRIBUTE_NODE: Final = re.compile(r'^(node_\d+): (\S+) "(.*?)" \(fragments [\d, ]+\)(?: also under: .*)?$')


def shop_corpus(snapshot: int, index_root: Path) -> FileCorpusSource:
    """The ``CorpusSource`` over ``shop/<snapshot>/`` (its version index under ``index_root``)."""

    return FileCorpusSource(SHOP_DIR / str(snapshot), Path(index_root))


def untrusted_blocks(text: str) -> list[tuple[str, str]]:
    """``(label, body)`` of every ``Gateway.untrusted`` block of ``text``, in order."""

    return [(match.group(1), match.group(2)) for match in _UNTRUSTED_BLOCK.finditer(text)]


def outside_untrusted(text: str) -> str:
    """``text`` with every ``Gateway.untrusted`` block removed."""

    return _UNTRUSTED_BLOCK.sub("", text)


_KNOWN_TERM_LINE: Final = re.compile(r"^(.*) \(([^;()]+)(?:; also: (.*))?\)$")


def known_terms_of(text: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """``(name, label, aliases)`` of every line of the known terms block of a node request's user
    message (``prompts.known_term_line``), in order; ``[]`` without the block."""

    found = []
    for label, body in untrusted_blocks(text):
        if label != KNOWN_TERMS_LABEL:
            continue
        for line in body.split("\n"):
            match = _KNOWN_TERM_LINE.match(line)
            if match is None:
                raise ScriptError(f"unreadable known term line {line!r}")
            aliases = tuple(match.group(3).split(", ")) if match.group(3) else ()
            found.append((match.group(1), match.group(2), aliases))
    return found


@dataclass(frozen=True)
class FragmentHandle:
    """How an answer file names a fragment: the breadcrumb of its fragment block, the start of its first
    body line (first non-blank line that is not a heading) and, for fragments equal in both, the
    occurrence (1-based, document order) among them."""

    handle: str
    breadcrumb: str
    first_line: str
    occurrence: int = 1


@dataclass(frozen=True)
class Related:
    """A declared nearness of an answer file (``ShopAnswers.related``)."""

    anchor: str
    cosine: float
    fragments: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()


@dataclass(frozen=True)
class Episode:
    """One fragment block of a node or edge request, identified as the answer file names it; ``index``
    is its 0-based position (its block number minus one)."""

    index: int
    handle: str
    breadcrumb: str
    first_line: str
    text: str


def _first_body_line(text: str) -> str:
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def node_labels(ontology: Ontology) -> frozenset[str]:
    """The labels of every node answer list of ``ontology`` (both passes)."""

    return frozenset(label for labels in node_lists(ontology).values() for label in labels)


def answer_lists(output_type: type[BaseModel]) -> dict[str, frozenset[str]]:
    """The answer lists of a node call's output model with the labels each list's ``label`` enum allows."""

    schema = strict_json_schema(output_type)
    lists: dict[str, frozenset[str]] = {}
    for name, field_schema in schema["properties"].items():
        reference = field_schema["items"]["$ref"].rsplit("/", 1)[-1]
        lists[name] = frozenset(schema["$defs"][reference]["properties"]["label"]["enum"])
    return lists


class ShopAnswers:
    """The hand-written answers of one shop snapshot (``shop/answers/<snapshot>.yaml``)."""

    def __init__(self, data: Mapping[str, Any], *, source: str = "answers") -> None:
        self.source = source
        self.snapshot = int(data["snapshot"])
        self.options = BuildOptions.model_validate(data.get("options") or {})
        self.fragments: dict[str, FragmentHandle] = {}
        for handle, spec in (data.get("fragments") or {}).items():
            breadcrumb, first_line, *rest = spec
            self.fragments[handle] = FragmentHandle(handle, breadcrumb, first_line, int(rest[0]) if rest else 1)
        self.nodes: dict[tuple[str, ...], Mapping[str, Any]] = {}
        for item in data.get("nodes") or ():
            self.nodes[self._key(item["window"], "nodes window")] = item
        self.edges: dict[tuple[str, ...], list[Any]] = {}
        for item in data.get("edges") or ():
            self.edges[self._key(item["unit"], "edges unit")] = list(item.get("facts") or ())
        self.attributes: dict[str, dict[str, Any]] = {
            str(name): dict(values or {}) for name, values in (data.get("attributes") or {}).items()
        }
        self.consolidation: dict[tuple[str, ...], list[Mapping[str, Any]]] = {
            tuple(sorted(item["block"])): list(item["groups"]) for item in data.get("consolidation") or ()
        }
        self.dedup: dict[tuple[str, ...], dict[str, Any]] = {
            tuple(sorted(item["pair"])): {
                "same": bool(item["same"]),
                "confidence": str(item.get("confidence", DEFAULT_CONFIDENCE)),
            }
            for item in data.get("dedup") or ()
        }
        self.synonyms: list[list[str]] = [list(group) for group in data.get("synonyms") or ()]
        self.related: list[Related] = [
            Related(
                anchor=str(item["anchor"]),
                cosine=float(item["cosine"]),
                fragments=tuple(item.get("fragments") or ()),
                texts=tuple(item.get("texts") or ()),
            )
            for item in data.get("related") or ()
        ]
        self._check_handles()

    @classmethod
    def load(cls, snapshot: int) -> ShopAnswers:
        return cls(shop_answer_data(snapshot), source=f"{snapshot}.yaml")

    def _key(self, handles: Iterable[str], what: str) -> tuple[str, ...]:
        key = tuple(sorted(handles))
        if len(set(key)) != len(key):
            raise ScriptError(f"{self.source}: {what} {key} repeats a fragment")
        return key

    def _check_handles(self) -> None:
        used = [handle for key in (*self.nodes, *self.edges) for handle in key]
        for item in self.nodes.values():
            for entity in item.get("entities") or ():
                used.extend(entity[2] if len(entity) > 2 else ())
        for facts in self.edges.values():
            for fact in facts:
                used.extend(fact[4])
        unknown = sorted(set(used) - set(self.fragments))
        if unknown:
            raise ScriptError(f"{self.source}: undeclared fragment handles {unknown}")
        for related in self.related:
            for handle in related.fragments:
                if handle not in self.fragments:
                    raise ScriptError(f"{self.source}: related fragment {handle!r} is not declared")
                first = self.fragments[handle].first_line
                if any(other.first_line == first for name, other in self.fragments.items() if name != handle):
                    raise ScriptError(f"{self.source}: related fragment {handle!r} shares its first line")

    def fragment_text_matcher(self, handles: Iterable[str]) -> Callable[[str], bool]:
        """Whether an embedded text is the text of one of ``handles``' fragments (by first body line)."""

        firsts = tuple(self.fragments[handle].first_line for handle in handles)

        def matches(text: str) -> bool:
            return bool(firsts) and _first_body_line(text).startswith(firsts)

        return matches

    def fragment_ids(self, plan: PlanArtifact) -> dict[str, str]:
        """Fragment id of each handle that names a fragment of ``plan`` (by breadcrumb, first body line
        and occurrence, as the requests show them)."""

        titles = {document.document_id: document.title for document in plan.documents}
        found: dict[str, str] = {}
        seen: dict[tuple[str, str], int] = {}
        for fragment in plan.fragments:
            crumb = breadcrumb(titles.get(fragment.document_id, ""), fragment.heading_path)
            first = _first_body_line(fragment.text)
            seen[(crumb, first)] = seen.get((crumb, first), 0) + 1
            handle = self.handle_of(crumb, fragment.text, seen[(crumb, first)])
            if handle is not None:
                found[handle] = fragment.fragment_id
        return found

    # Request parsing

    def handle_of(self, breadcrumb: str, text: str, occurrence: int = 1) -> str | None:
        """The handle naming a fragment block (breadcrumb, fragment text, 1-based occurrence among blocks
        equal in both); None when no handle matches; ``ScriptError`` when several do."""

        first = _first_body_line(text)
        matches = [
            handle.handle
            for handle in self.fragments.values()
            if handle.breadcrumb == breadcrumb
            and first.startswith(handle.first_line)
            and handle.occurrence == occurrence
        ]
        if len(matches) > 1:
            raise ScriptError(f"{self.source}: fragment handles {matches} match one fragment")
        return matches[0] if matches else None

    def episodes(self, request: ScriptedRequest) -> list[Episode]:
        """The fragment blocks (``fragment <n>``) of a node or edge request, in order, named by their
        handles (a block no handle matches gets ``?<breadcrumb> | <first line>``, which no answer key
        holds)."""

        found: list[Episode] = []
        seen: dict[tuple[str, str], int] = {}
        for label, body in untrusted_blocks(request.user):
            number = FRAGMENT_BLOCK_LABEL.fullmatch(label)
            if number is None:
                continue
            if int(number.group(1)) != len(found) + 1:
                raise ScriptError(f"{self.source}: fragment block {label!r} out of order")
            breadcrumb, _, text = body.partition("\n")
            first = _first_body_line(text)
            seen[(breadcrumb, first)] = seen.get((breadcrumb, first), 0) + 1
            name = self.handle_of(breadcrumb, text, seen[(breadcrumb, first)]) or f"?{breadcrumb} | {first[:60]}"
            found.append(Episode(len(found), name, breadcrumb, first, text))
        return found

    def window(self, request: ScriptedRequest, episodes: Sequence[Episode]) -> list[Episode]:
        """The fragment blocks the request extracts from (its window line, read with the library's
        ``prompts.window_fragments``; all blocks without one)."""

        numbers = window_fragments(outside_untrusted(request.user))
        if not numbers:
            return list(episodes)
        if numbers[-1] > len(episodes):
            raise ScriptError(f"{self.source}: window {numbers} beyond the request's {len(episodes)} fragments")
        return [episodes[number - 1] for number in numbers]

    def node_key(self, request: ScriptedRequest) -> Hashable:
        return tuple(sorted(episode.handle for episode in self.window(request, self.episodes(request))))

    def edge_key(self, request: ScriptedRequest) -> Hashable:
        return tuple(sorted(episode.handle for episode in self.episodes(request)))

    @staticmethod
    def attribute_nodes(request: ScriptedRequest) -> list[tuple[str, str, str]]:
        """``(field, label, name)`` of every node of an attributes batch."""

        nodes = []
        for label, body in untrusted_blocks(request.user):
            if label == NODES_LABEL:
                for line in body.split("\n"):
                    match = _ATTRIBUTE_NODE.match(line)
                    if match is None:
                        raise ScriptError(f"unreadable attributes node line {line!r}")
                    nodes.append((match.group(1), match.group(2), match.group(3)))
        return nodes

    def attribute_key(self, request: ScriptedRequest) -> Hashable:
        return tuple(name for _, _, name in self.attribute_nodes(request))

    @staticmethod
    def hub_items(request: ScriptedRequest) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for label, body in untrusted_blocks(request.user)
            if label == "hubs"
            for line in body.split("\n")
            if line.strip()
        ]

    def consolidation_key(self, request: ScriptedRequest) -> Hashable:
        return tuple(sorted(item["name"] for item in self.hub_items(request)))

    @staticmethod
    def dedup_key(request: ScriptedRequest) -> Hashable:
        for label, body in untrusted_blocks(request.user):
            if label == "statements":
                return tuple(sorted(item["name"] for item in json.loads(body)["statements"]))
        return None

    # Answers

    def _nodes(
        self,
        request: ScriptedRequest,
        rename: Mapping[str, str] | None = None,
        labels: Collection[str] | None = None,
    ) -> Any:
        """``rename``: the gateway's renamed ontology names; an entity's label is looked up renamed."""

        rename = rename or {}
        labels = node_labels(shipped_ontology()) if labels is None else labels
        episodes = self.episodes(request)
        window = self.window(request, episodes)
        item = self.nodes.get(tuple(sorted(episode.handle for episode in window)))
        if item is None:
            return NO_ANSWER
        if "error" in item:
            return LLMError(item["error"])
        lists = answer_lists(request.output_type)
        # both passes send one schema (every list): the pass's prompt names the list it fills
        asked = next((name for name, prompt in NODE_PROMPTS.items() if prompt == request.prompt), None)
        number_of = {episode.handle: episode.index + 1 for episode in episodes}
        answer: dict[str, list[dict[str, Any]]] = {name: [] for name in lists}
        for entity in item.get("entities") or ():
            name, label, *rest = entity
            label = rename.get(label, label)
            target = next((list_name for list_name, labels in lists.items() if label in labels), None)
            if target is not None and asked is not None and target != asked:
                continue  # the other pass's entity: its list stays empty
            if target is None:
                if entity[1] not in labels and label not in labels:
                    raise ScriptError(f"{self.source}: entity {name!r}: label {entity[1]!r} is in neither node list")
                continue  # the other pass's entity
            handles = self._entity_handles(name, rest, window, number_of)
            quote = rest[1] if len(rest) > 1 and rest[1] is not None else self.default_quote(name)
            found: dict[str, Any] = {
                "name": name,
                "label": label,
                "quote": quote,
                "fragments": [number_of[handle] for handle in handles],
            }
            if target == HUBS_LIST:
                found["aliases"] = list(rest[2]) if len(rest) > 2 else []
            answer[target].append(found)
        return answer

    def _entity_handles(
        self, name: str, rest: Sequence[Any], window: Sequence[Episode], number_of: Mapping[str, int]
    ) -> list[str]:
        """The fragment handles of an answer-file entity (a one-fragment window's own by default)."""

        if rest:
            handles = list(rest[0])
        elif len(window) == 1:
            handles = [window[0].handle]
        else:
            raise ScriptError(f"{self.source}: entity {name!r} of a multi-fragment window names no fragment")
        missing = [handle for handle in handles if handle not in number_of]
        if missing:
            raise ScriptError(f"{self.source}: entity {name!r}: fragments {missing} not in the request")
        return handles

    def default_quote(self, name: str) -> str:
        """The quote of an entity the answer file gives none: the ``attributes`` quote of its name, or
        of its name without an explicit id (``FR-1: Name``, as the attributes table names the node)."""

        for key in (name, name.partition(": ")[2]):
            quote = (self.attributes.get(key) or {}).get("quote")
            if isinstance(quote, str) and quote:
                return quote
        return ""

    def _edges(self, request: ScriptedRequest) -> Any:
        """The scripted facts of the request's unit (keyed by all its fragments) that its edge window states: those with
        a fragment in the window, with only those fragments (a window over the whole unit: every fact, as listed).
        """

        episodes = self.episodes(request)
        facts = self.edges.get(tuple(sorted(episode.handle for episode in episodes)))
        if facts is None:
            return NO_ANSWER
        window = {episode.handle for episode in self.window(request, episodes)}
        if len(window) < len(episodes):
            facts = [
                (source, relation, target, fact, [handle for handle in handles if handle in window])
                for source, relation, target, fact, handles in facts
                if any(handle in window for handle in handles)
            ]
        names = {
            json.loads(line)["name"]
            for label, body in untrusted_blocks(request.user)
            if label == ENTITIES_LABEL
            for line in body.split("\n")
            if line.strip()
        }
        number_of = {episode.handle: episode.index + 1 for episode in episodes}
        edges = []
        for source, relation, target, fact, handles in facts:
            missing = [name for name in (source, target) if name not in names]
            missing += [handle for handle in handles if handle not in number_of]
            if missing:
                raise ScriptError(f"{self.source}: fact {source!r} {relation} {target!r}: {missing} not in the request")
            edges.append(
                {
                    "source": source,
                    "relation": relation,
                    "target": target,
                    "fact": fact,
                    "fragments": [number_of[handle] for handle in handles],
                }
            )
        return {"edges": edges}

    def _attributes(self, request: ScriptedRequest, rename: Mapping[str, str] | None = None) -> Any:
        """``rename``: the gateway's renamed ontology names; an attribute is kept when its renamed
        name is a field of the request's output model (the gateway renames the keys afterwards)."""

        rename = rename or {}
        nodes = self.attribute_nodes(request)
        if any(name not in self.attributes for _, _, name in nodes):
            return NO_ANSWER
        fields = request.output_type.model_fields
        answer: dict[str, dict[str, Any]] = {}
        for field_name, _, name in nodes:
            model = fields[field_name].annotation
            known = set(model.model_fields) if isinstance(model, type) and issubclass(model, BaseModel) else set()
            answer[field_name] = {
                key: value for key, value in self.attributes[name].items() if rename.get(key, key) in known
            }
        return answer

    def _consolidation(self, request: ScriptedRequest) -> Any:
        items = self.hub_items(request)
        groups = self.consolidation.get(tuple(sorted(item["name"] for item in items)))
        if groups is None:
            return NO_ANSWER
        # A name stands for every item of the block that carries it (one name under several labels).
        ids: dict[str, list[str]] = {}
        for item in items:
            ids.setdefault(item["name"], []).append(item["id"])
        return {
            "groups": [
                {
                    "ids": [found for name in dict.fromkeys(group["names"]) for found in ids[name]],
                    "label": group["label"],
                    "confidence": group.get("confidence", DEFAULT_CONFIDENCE),
                }
                for group in groups
            ]
        }

    def _dedup(self, request: ScriptedRequest) -> Any:
        verdict = self.dedup.get(self.dedup_key(request))
        return NO_ANSWER if verdict is None else dict(verdict)

    @staticmethod
    def _twins(request: ScriptedRequest) -> Any:
        """Cross-label twins the scripted block judge left apart stay apart: every pair is different."""

        return {
            "answers": [
                {"id": json.loads(line)["id"], "same": False, "label": "", "confidence": "high"}
                for label, body in untrusted_blocks(request.user)
                if label == "hubs"
                for line in body.split("\n")
                if line.strip()
            ]
        }

    def install(self, gateway: ScriptedGateway, ontology: Ontology | None = None) -> ScriptedGateway:
        """Script ``gateway`` with these answers (keys, handlers and synonyms); returns it. ``ontology``:
        the build's (its node labels check the answers' labels; default the shipped one)."""

        labels = node_labels(ontology or shipped_ontology())

        def reported(handler: Handler) -> Handler:
            # The library may catch a script error, so it is also recorded where the
            # ``scripted_gateway`` fixture reports it at teardown.
            def run(request: ScriptedRequest) -> Any:
                try:
                    return handler(request)
                except ScriptError as error:
                    gateway.unscripted.append(f"script error for prompt {request.prompt!r}: {error}")
                    raise

            return run

        for prompt, key, handler in (
            (NODE_HUBS_PROMPT, self.node_key, lambda request: self._nodes(request, gateway.rename, labels)),
            (NODE_STATEMENTS_PROMPT, self.node_key, lambda request: self._nodes(request, gateway.rename, labels)),
            (EDGE_WINDOW_PROMPT, self.edge_key, self._edges),
            (ATTRIBUTES_PROMPT, self.attribute_key, lambda request: self._attributes(request, gateway.rename)),
            (HUBS_PROMPT, self.consolidation_key, self._consolidation),
            (DEDUP_PROMPT, self.dedup_key, self._dedup),
            (TWINS_PROMPT, lambda _: None, self._twins),
        ):
            gateway.key_by(prompt, key).handle(prompt, reported(handler))
        for group in self.synonyms:
            gateway.synonyms(*group)
        for related in self.related:
            gateway.relate(
                related.anchor,
                related.cosine,
                texts=related.texts,
                matching=self.fragment_text_matcher(related.fragments) if related.fragments else None,
            )
        return gateway


def shop_answer_data(snapshot: int) -> dict[str, Any]:
    """A fresh copy of the parsed answer file of shop snapshot ``snapshot`` (tests mutate it for variants)."""

    path = SHOP_DIR / "answers" / f"{snapshot}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@cache
def shop_answers(snapshot: int) -> ShopAnswers:
    """The hand-written answers of shop snapshot ``snapshot`` (``shop/answers/<snapshot>.yaml``)."""

    return ShopAnswers.load(snapshot)


SHOP_NEARNESS: Final = "name"
"""Edge nearness the shop answer files were written for.

Their ``related`` nearness puts hubs with no line names into edge catalogs. A build leaves those
out when ``EdgeWindowParams.nearness`` is ``off``.
"""


@contextmanager
def shop_edge_windows() -> Iterator[None]:
    """The build's edge window parameters (``edges.DEFAULT_EDGE_WINDOWS``) with ``SHOP_NEARNESS``; the
    others stay as they are (a test may have set its own)."""

    with pytest.MonkeyPatch.context() as patch:
        written = replace(edge_extraction.DEFAULT_EDGE_WINDOWS, nearness=SHOP_NEARNESS)
        patch.setattr(edge_extraction, "DEFAULT_EDGE_WINDOWS", written)
        yield


@contextmanager
def follow_up_builds(limit: int) -> Iterator[None]:
    """``run_build`` chains at most ``limit`` follow-up builds (``graph.build.run.MAX_FOLLOW_UPS``, "Follow-up builds
    and settlement"); an internal bound, never part of the build API.
    """

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(build_run, "MAX_FOLLOW_UPS", limit)
        yield


async def run_shop_build(ctx: BuildContext, *, follow_ups: int = 0) -> BuildResult:
    """``run_build`` of a shop or shop-v2 build with the nearness its answers were written for."""

    with shop_edge_windows(), follow_up_builds(follow_ups):
        return await run_build(ctx)


# Shop builds with inference and verification on (renamed-ontology and log-capture tests)

# Hand-written attach-orphan verdicts of the shop builds, by subject name: the candidate to attach
# and a quote of the subject's fragment. Every other subject of every rule is answered "no link".
SHOP_ORPHAN_VERDICTS: Final[dict[str, tuple[str, str]]] = {
    "System cannot store full card numbers": ("Payment flow", "never stores full card numbers"),
    "System can leave cancelled orders out of the order file": (
        "Order history export",
        "cancelled orders are left out of the file",
    ),
}
NO_LINK_VERDICTS: Final[dict[str, dict[str, Any]]] = {
    "endpoints": {"confidence": 0.9, "evidence": [], "links": []},
    "realization": {"confidence": 0.9, "evidence": [], "targets": []},
    "attach_orphan": {"confidence": 0.9, "evidence": [], "target": None},
    "cross_document": {"confidence": 0.9, "evidence": [], "links": [], "owners": []},
}


def judge_payload(request: ScriptedRequest) -> dict[str, Any]:
    """The JSON ``payload`` block of a judge request."""

    return json.loads(dict(untrusted_blocks(request.user))["payload"])


def judge_subject(request: ScriptedRequest) -> str:
    """The judged subject's name: the ``subject``, or the ``hub`` of a ``cross_document`` payload."""

    payload = judge_payload(request)
    return str((payload.get("subject") or payload["hub"])["name"])


def script_shop_judge(gateway: ScriptedGateway, ontology: Ontology) -> ScriptedGateway:
    """Script the judge prompts of ``ontology``'s inference rules for shop builds, keyed by subject name:
    ``SHOP_ORPHAN_VERDICTS`` for attach-orphan subjects (the candidate's local id and the quoted fragment are looked up
    in the request), a no-link verdict for every other subject.
    """

    def handler(template: str) -> Handler:
        def handle(request: ScriptedRequest) -> Any:
            verdict = SHOP_ORPHAN_VERDICTS.get(judge_subject(request)) if template == "attach_orphan" else None
            if verdict is None:
                return copy.deepcopy(NO_LINK_VERDICTS[template])
            target, quote = verdict
            ids = [item["id"] for item in judge_payload(request)["candidates"] if item["name"] == target]
            fragments = [
                label
                for label, body in untrusted_blocks(request.user)
                if label != "payload" and quote in " ".join(body.split())
            ]
            if len(ids) != 1 or not fragments:
                message = f"shop verdict for {judge_subject(request)!r}: {target!r} or its quote not in the request"
                gateway.unscripted.append(message)
                raise ScriptError(message)
            return {"confidence": 0.9, "evidence": [{"fragment_id": fragments[0], "quote": quote}], "target": ids[0]}

        return handle

    for rule in ontology.inference.rules:
        gateway.key_by(rule.prompt, judge_subject).handle(rule.prompt, handler(rule.template))
    return gateway


@dataclass
class ShopBuilds:
    """Builds of shop snapshots into one project with inference and verification on, sharing file stores and the corpus
    index under ``root``; every model answer is hand-written (``ShopAnswers``, ``script_shop_judge``).
    """

    project: Neo4jProject
    factory: Callable[..., ScriptedGateway]
    root: Path
    project_id: str = ""
    ontology: Ontology | None = None
    rename: Mapping[str, str] | None = None
    llm: LLMConfig | None = None
    stores: FileStores = field(init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.project_id = self.project_id or self.project.project_id
        self.ontology = self.ontology or self.project.ontology
        self.llm = self.llm or live_llm_config()
        self.stores = open_file_stores(self.root)

    def gateway(self, snapshot: int) -> ScriptedGateway:
        gateway = shop_answers(snapshot).install(self.factory(self.llm, rename=self.rename), self.ontology)
        return script_shop_judge(gateway, self.ontology)

    def context(self, snapshot: int, gateway: Any) -> BuildContext:
        return BuildContext(
            project_id=self.project_id,
            ontology=self.ontology,
            driver=self.project.driver,
            gateway=gateway,
            registry=self.stores.registry,
            corpus=shop_corpus(snapshot, self.root),
            extraction=self.stores.extraction,
            builds=self.stores.builds,
            options=shop_answers(snapshot).options,
            database=self.project.database,
            settings=BuildSettings(inference=True),
        )

    async def build(self, snapshot: int) -> tuple[BuildResult, ScriptedGateway]:
        gateway = self.gateway(snapshot)
        return await run_shop_build(self.context(snapshot, gateway)), gateway


# A live project shared by the tests of one module (expensive setups built once per module)


@asynccontextmanager
async def open_neo4j_project() -> AsyncIterator[Neo4jProject]:
    """What ``neo4j_project`` does, as a context manager for wider fixture scopes (its tests carry
    ``requires_live("NEO4J")``): unique project ids, deleted on exit; fails when data outside the test projects changed
    meanwhile.
    """

    config = neo4j_config_from_env()
    driver = connect(config)
    project = Neo4jProject(
        driver=driver, database=config.database, ontology=live_ontology(), project_id=new_project_id()
    )
    try:
        await ensure_session_schema(driver, config.database)
        before = await outside_counts(driver, config.database)
        yield project
    finally:
        try:
            left = await project.cleanup()
            after = await outside_counts(driver, config.database)
        finally:
            await driver.close()
    assert left == 0, f"{left} nodes of {project.project_ids} left after cleanup"
    assert after == before, f"data outside the test projects changed: {before} -> {after}"
