"""Log capture over full builds (plan 5.T4; NFR-SEC-01: logs never hold document text, prompt text
or secrets; tech-specs 8 and 9.3 "Log capture over a full build").

One test project is built once for the module from the shop corpus 1 -> 2 -> 3 with inference and
verification on (``ShopBuilds``, every model answer hand-written), then read through ``GraphReader``
(node and edge reads, scope, impact, evidence lookup, hybrid and fulltext search, and a search whose
embedding fails). Every log record is captured on the root logger while the ``graph`` and
``graphiti_core`` loggers (and the root) are at DEBUG; the central logger's third-party noise levels
(``neo4j``, ``httpx``, ...) stay as shipped. Each record is checked raw (message, structured fields,
exception and stack text) and as the central ``JsonFormatter`` prints it, whitespace collapsed.

Texts are matched by shingles, so an excerpt is caught as well as a whole line: no record may hold any
40-character window (one with at least four spaces, so prose and not an id, path or hash) or any
8-word n-gram of a line of:

- the corpus: the shop documents of the three snapshots, markdown markers stripped;
- prompt text: every message the library sent to the model, the judge prompt files and the
  ontology's prompt text (framing, exclusions, descriptions, guidance);
- model output: the hand-written answers' facts and attribute values, and the judge quotes;
- query text: the Cypher text of the library's ``Query`` constants.

And no record may hold, as a substring: a node or hub name of the answers of at least 15 characters
(shorter ones, such as "ERP" or "Stripe", are too generic to search for), an identity key, identity
alias or edge key of the built project, the LLM API key, the Neo4j password of the environment, a
reader search text or a whole Cypher line longer than 40 characters.

Gate: ``SPECFLOW_TEST_NEO4J=1`` and ``uv run --env-file ../.env.local``; the project is a
``pytest-graph-<hex>`` id (``open_neo4j_project``), deleted after the module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import Counter
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr

import graph.build.queries as build_queries
import graph.core.queries as core_queries
from graph.core.errors import InfrastructureUnavailableError
from graph.core.queries import Query
from graph_support import (
    SHOP_DIR,
    SHOP_ORPHAN_VERDICTS,
    Neo4jProject,
    ScriptedGateway,
    ShopBuilds,
    assert_all_scripted,
    live_embedding_dim,
    llm_config,
    open_neo4j_project,
    shop_answers,
)
from logger.formatters import JsonFormatter
from logger.logger import get_logger
from logger_harness import fields
from specflow_live import requires_live

pytestmark = requires_live("NEO4J")

API_KEY = "sk-graph-log-capture-7f3c9a0e51d24b86"
SEARCHES = (
    "customer exports the order history as a spreadsheet",
    "payment provider answers too slowly at checkout",
)
WINDOW_CHARS = 40
WINDOW_MIN_SPACES = 4
NGRAM_WORDS = 8
NAME_MIN_CHARS = 15
CYPHER_MIN_CHARS = 40
DEBUG_LOGGERS = ("", "graph", "graphiti_core")
_MARKDOWN = re.compile(r"^(?:#+|[-*+]|\d+\.|>)\s+")
_SPACE = re.compile(r"\s+")


def normalised(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


# Shingles


def _windows(text: str) -> Iterator[str]:
    for start in range(len(text) - WINDOW_CHARS + 1):
        window = text[start : start + WINDOW_CHARS]
        if window.count(" ") >= WINDOW_MIN_SPACES:
            yield window


def _ngrams(text: str) -> Iterator[str]:
    words = text.split(" ")
    for start in range(len(words) - NGRAM_WORDS + 1):
        yield " ".join(words[start : start + NGRAM_WORDS])


@dataclass
class Shingles:
    """The hashes of every prose window and word n-gram of the lines of some texts."""

    windows: set[int] = field(default_factory=set)
    ngrams: set[int] = field(default_factory=set)

    @classmethod
    def of(cls, texts: Iterable[str]) -> Shingles:
        shingles = cls()
        lines = {normalised(line) for text in texts for line in text.splitlines()}
        for line in lines:
            shingles.windows.update(map(hash, _windows(line)))
            shingles.ngrams.update(map(hash, _ngrams(line)))
        return shingles

    def __len__(self) -> int:
        return len(self.windows) + len(self.ngrams)

    def find(self, text: str) -> str | None:
        """The first window or n-gram of ``text`` (normalised) that one of the texts holds."""

        for window in _windows(text):
            if hash(window) in self.windows:
                return window
        for ngram in _ngrams(text):
            if hash(ngram) in self.ngrams:
                return ngram
        return None


@dataclass
class Forbidden:
    """What no record may hold: shingled texts and exact values, by kind."""

    texts: dict[str, Shingles]
    values: dict[str, set[str]]


# Capture


class Capture(logging.Handler):
    """Keeps every record that reaches the root logger."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _logger(name: str) -> logging.Logger:
    """The stdlib logger ``name`` (the root logger for ``""``) through the central logger's accessor."""

    return get_logger(name) if name else logging.root


class Capturing:
    """Capture every record with the ``DEBUG_LOGGERS`` at DEBUG; levels restored on exit."""

    def __enter__(self) -> Capture:
        get_logger("graph.tests.logging")  # bootstrap the central pipeline first, so its levels are set
        self.handler = Capture()
        self.levels = {name: _logger(name).level for name in DEBUG_LOGGERS}
        for name in DEBUG_LOGGERS:
            _logger(name).setLevel(logging.DEBUG)
        logging.root.addHandler(self.handler)
        return self.handler

    def __exit__(self, *exc_info: Any) -> None:
        logging.root.removeHandler(self.handler)
        for name, level in self.levels.items():
            _logger(name).setLevel(level)


def _flatten(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _flatten(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _flatten(item)
    elif value is not None:
        yield str(value)


def record_text(record: logging.LogRecord) -> str:
    """Everything a record carries, raw and as the central formatter prints it, whitespace collapsed."""

    parts: list[str] = []
    try:
        parts.append(record.getMessage())
    except (TypeError, ValueError, KeyError, IndexError) as error:
        parts.append(f"{record.msg!r} {record.args!r} {error!r}")
    parts.extend(_flatten(fields(record)))
    if record.exc_info:
        parts.append(logging.Formatter().formatException(record.exc_info))
    if record.exc_text:
        parts.append(record.exc_text)
    if record.stack_info:
        parts.append(record.stack_info)
    formatted = JsonFormatter().format(record)
    try:
        parts.extend(_flatten(json.loads(formatted)))
    except json.JSONDecodeError:
        parts.append(formatted)
    return normalised("\n".join(parts))


# What must never be logged


def corpus_texts() -> list[str]:
    return [
        _MARKDOWN.sub("", line.strip())
        for path in sorted(SHOP_DIR.glob("[0-9]/*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def prompt_texts(gateways: Iterable[ScriptedGateway], ontology: Any) -> list[str]:
    sent = [
        message["content"]
        for gateway in gateways
        for call in gateway.calls
        for message in call.messages
        if isinstance(message.get("content"), str)
    ]
    texts = [ontology.framing, *ontology.exclude, *ontology.judge_prompts.values()]
    for node_type in ontology.node_types.values():
        texts += [node_type.description, node_type.guidance]
        texts += [attribute.description for attribute in node_type.attributes.values()]
    for group in ontology.node_groups.values():
        texts += [group.description, group.guidance]
    texts += [relation.description for relation in ontology.relations.values()]
    return [*sent, *texts]


def model_output() -> tuple[list[str], set[str]]:
    """The hand-written answers of the three snapshots: (output texts, node and hub names)."""

    texts: list[str] = [quote for _, quote in SHOP_ORPHAN_VERDICTS.values()]
    names: set[str] = {name for name, _ in SHOP_ORPHAN_VERDICTS.values()} | set(SHOP_ORPHAN_VERDICTS)
    for snapshot in (1, 2, 3):
        answers = shop_answers(snapshot)
        for item in answers.nodes.values():
            names.update(str(entity[0]) for entity in item.get("entities") or ())
        for facts in answers.edges.values():
            for source, _, target, fact, _ in facts:
                names.update((str(source), str(target)))
                texts.append(str(fact))
        for name, values in answers.attributes.items():
            names.add(name)
            texts.extend(str(value) for value in values.values() if isinstance(value, str))
        for hubs in answers.hub_catalog.values():
            names.update(str(hub[0]) for hub in hubs)
        for pair in answers.dedup:
            names.update(pair)
    return texts, {name for name in names if len(name) >= NAME_MIN_CHARS}


def cypher_texts() -> list[str]:
    """The Cypher text of every ``Query`` constant of the library's modules."""

    texts = []
    for module in (core_queries, build_queries):
        texts.extend(value.text for value in vars(module).values() if isinstance(value, Query))
    assert len(texts) > 20, "the library's queries were not found"
    return texts


async def project_keys(project: Neo4jProject, project_id: str) -> set[str]:
    rows = await project.run(
        "MATCH (n:SpecNode) WHERE n.group_id = $p "
        "RETURN [n.identity_key] + coalesce(n.identity_aliases, []) AS keys "
        "UNION ALL MATCH (:SpecNode)-[r]->(:SpecNode) WHERE r.group_id = $p AND r.edge_key IS NOT NULL "
        "RETURN [r.edge_key] AS keys",
        p=project_id,
    )
    return {key for row in rows for key in row["keys"] if key}


def secret_label(kind: str, value: str) -> str:
    """How a leaked value is named in a failure: secrets by digest only, test data in full."""

    if kind == "secret":
        return f"<secret sha256:{hashlib.sha256(value.encode()).hexdigest()[:12]}>"
    return value


@dataclass(frozen=True)
class Leak:
    kind: str
    logger: str
    event: str
    value: str


def leaks(records: Iterable[logging.LogRecord], forbidden: Forbidden) -> list[Leak]:
    """One leak per record and kind it holds (the first match of the kind)."""

    found = []
    for record in records:
        text = record_text(record)
        event = str(getattr(record, "event", record.msg))
        for kind, shingles in forbidden.texts.items():
            match = shingles.find(text)
            if match is not None:
                found.append(Leak(kind, record.name, event, match))
        for kind, values in forbidden.values.items():
            match = next((value for value in values if value in text), None)
            if match is not None:
                found.append(Leak(kind, record.name, event, secret_label(kind, match)))
    return found


# The run, once per module


@dataclass
class Run:
    records: list[logging.LogRecord]
    gateways: list[ScriptedGateway]
    keys: set[str]
    builds: ShopBuilds
    forbidden: Forbidden


async def read_everything(builds: ShopBuilds, gateway: ScriptedGateway) -> None:
    reader = builds.project.reader(ontology=builds.ontology, gateway=gateway)
    pid = builds.project_id
    nodes = await reader.nodes(pid)
    assert nodes
    await reader.edges(pid)
    await reader.documents(pid)
    fragments = await reader.fragments(pid)
    roots = [node for node in nodes if node.label in builds.ontology.spine.roots]
    for node in (*roots[:2], nodes[0]):
        await reader.node(pid, node.spec_id)
        await reader.neighbours(pid, node.spec_id)
        await reader.scope(pid, node.spec_id)
    await reader.impact(pid, [node.spec_id for node in nodes[:3]])
    fragment = fragments[0]
    await reader.nodes_for_fragment(pid, fragment.document_id, 1, 40)
    for text in SEARCHES:
        await reader.find_nodes(pid, text)
        await reader.find_fragments(pid, text)

    async def unreachable(texts: Any) -> list[list[float]]:
        # What the gateway raises for an unreachable endpoint ("unreachable" is no ``LLMError`` reason).
        raise InfrastructureUnavailableError("llm_unreachable")

    gateway.embed = unreachable
    await reader.find_nodes(pid, SEARCHES[0])  # embedding fails: fulltext only (FR-READ-03)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def run(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Run]:
    """The builds and reads, once for the module; the tests reading it are synchronous (the driver
    lives on this fixture's module loop)."""

    config = llm_config(embedding_dim=live_embedding_dim(), api_key=SecretStr(API_KEY))
    gateways: list[ScriptedGateway] = []

    def factory(llm: Any = None, **options: Any) -> ScriptedGateway:
        gateway = ScriptedGateway(llm, **options)
        gateways.append(gateway)
        return gateway

    async with open_neo4j_project() as project:
        builds = ShopBuilds(project, factory, tmp_path_factory.mktemp("logging") / "stores", llm=config)
        with Capturing() as capture:
            for snapshot in (1, 2, 3):
                result, _ = await builds.build(snapshot)
                assert (result.failed_step, result.error_code) == (None, None), (snapshot, result.failed_step)
                assert result.verification is not None
            await read_everything(builds, factory(config))
        assert_all_scripted(*gateways)
        keys = await project_keys(project, builds.project_id)
        secrets = {API_KEY}
        password = os.environ.get("NEO4J_PASSWORD", "")
        if len(password) >= 4:
            secrets.add(password)
        output, names = model_output()
        cypher = cypher_texts()
        forbidden = Forbidden(
            texts={
                "corpus text": Shingles.of(corpus_texts()),
                "prompt text": Shingles.of(prompt_texts(gateways, builds.ontology)),
                "model output": Shingles.of(output),
                "cypher": Shingles.of(cypher),
            },
            values={
                "node name": names,
                "identity or edge key": keys,
                "secret": secrets,
                "search text": set(SEARCHES),
                "cypher line": {
                    line
                    for text in cypher
                    for line in map(normalised, text.splitlines())
                    if len(line) > CYPHER_MIN_CHARS
                },
            },
        )
        yield Run(list(capture.records), gateways, keys, builds, forbidden)


def test_the_capture_sees_debug_records_of_the_library_and_graphiti(run: Run, request: pytest.FixtureRequest) -> None:
    by_logger = Counter(record.name.split(".")[0] for record in run.records)
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"\ncaptured records by top-level logger: {dict(by_logger)}")
    levels = Counter(record.levelname for record in run.records if record.name.startswith("graph."))
    assert by_logger["graph"] > 50 and by_logger["graphiti_core"] > 50, by_logger
    assert levels["DEBUG"] > 0 and levels["WARNING"] > 0, levels  # the malformed window warns
    events = {getattr(record, "event", None) for record in run.records}
    assert {"graph.build.started", "graph.infer.rule_done", "graph.verify.checked", "graph.read.find_nodes"} <= events
    unavailable = [r for r in run.records if getattr(r, "event", None) == "graph.read.vector_unavailable"]
    assert [getattr(r, "error_code", None) for r in unavailable] == ["llm_unreachable"]
    # The run exercised what the forbidden sets are made of.
    assert sum(len(gateway.calls) for gateway in run.gateways) > 50
    assert len(run.keys) > 50
    assert all(len(shingles) > 100 for shingles in run.forbidden.texts.values())
    assert len(run.forbidden.values["node name"]) > 30 and len(run.forbidden.values["cypher line"]) > 20


def _excerpt(texts: Iterable[str], minimum: int = 80) -> str:
    """A mid-line excerpt (characters 7 to 67) of the first long line of ``texts``."""

    line = next(normalised(line) for text in texts for line in text.splitlines() if len(normalised(line)) > minimum)
    return line[7:67]


def test_the_checks_find_planted_leaks(run: Run) -> None:
    """The checks are not vacuous: a record planting one value is caught as its kind, excerpts too."""

    output, names = model_output()
    cypher = max(sorted(run.forbidden.values["cypher line"]), key=lambda line: line.count(" "))
    planted = {
        "corpus text": f"... {_excerpt(corpus_texts())} ...",
        "prompt text": f"... {_excerpt(run.builds.ontology.judge_prompts.values())} ...",
        "model output": f"fact={_excerpt(output, 70)}",
        "node name": f"node {min(names)!r} created",
        "identity or edge key": min(run.keys),
        "secret": f"Authorization: Bearer {API_KEY}",
        "search text": SEARCHES[0],
        "cypher": cypher[5:65],
        "cypher line": cypher,
    }
    places = ("message", "field", "nested", "exc_text")
    for number, (kind, value) in enumerate(sorted(planted.items())):
        place = places[number % len(places)]
        record = logging.LogRecord("graph.build.planted", logging.INFO, __file__, 1, "planted %s", ("x",), None)
        record.event = "graph.planted.leak"
        if place == "message":
            record.args = (value,)
        elif place == "field":
            record.detail = value
        elif place == "nested":
            record.detail = {"items": [{"text": value}]}
        else:
            record.exc_text = f"Traceback (most recent call last):\n  {value}"
        found = leaks([record], run.forbidden)
        assert kind in {leak.kind for leak in found}, (kind, place, found)
        assert all(API_KEY not in leak.value for leak in found)  # a secret is named by digest only


def test_no_record_holds_document_prompt_or_model_text(run: Run) -> None:
    texts = Forbidden(
        texts={k: v for k, v in run.forbidden.texts.items() if k != "cypher"},
        values={
            "node name": run.forbidden.values["node name"],
        },
    )
    assert leaks(run.records, texts) == []


def test_no_record_holds_a_key_or_a_secret(run: Run) -> None:
    kinds = run.forbidden.values
    assert leaks(run.records, Forbidden({}, {k: kinds[k] for k in ("identity or edge key", "secret")})) == []


def test_no_record_holds_query_text(run: Run) -> None:
    kinds = run.forbidden
    queries = Forbidden({"cypher": kinds.texts["cypher"]}, {k: kinds.values[k] for k in ("search text", "cypher line")})
    assert leaks(run.records, queries) == []
