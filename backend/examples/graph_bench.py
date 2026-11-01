"""Record and replay graph builds for the cross-store benchmark (plan M5), and export a canonical snapshot.

Run from backend/ (record needs the LLM gateway environment, replay does not):

    uv run python examples/graph_bench.py record --project bench-demo --corpus <v1 folder> \
        --state <state folder> --cassette <dir>/v1.jsonl --out <report dir> --label v1 --wipe
    uv run python examples/graph_bench.py replay --project bench-demo --corpus <v1 folder> \
        --state <fresh state folder> --cassette <dir>/v1.jsonl --out <report dir> --label v1 --wipe

``record`` runs the build (what ``graph_build.py`` does: corpus folder, ``run_build``, file stores) with a
recording gateway: every model call is passed to the real ``Gateway`` and stored in the cassette (JSONL)
as ``request key -> response``. ``replay`` answers every call from the cassette only; a call the cassette
lacks raises ``CassetteMissError`` naming the prompt and call class (no live call, no fallback), and the
run fails.

One cassette per build (a first build and an update build each get their own file), so a build replays
from exactly what it recorded. A recording run that finds its cassette file continues it: requests
already stored are answered from it, which makes an interrupted recording resumable.

The request key is the sha256 of the call's semantic content: call class, model, prompt name, messages and
response schema for a structured call; model and text for each embedded text. Never timestamps, ids,
headers, keys or the endpoint. The cassette stores a request summary (counts, names) and the response; its
header names the models and the embedding dimension, never the gateway URL or key, so replay needs only
the Neo4j environment.

Both commands leave ``<out>/<label>.snapshot.json`` (canonical graph: nodes and edges without build ids,
timestamps and vectors, sorted) and ``<out>/<label>.summary.json`` (build result, verification report, counts,
wall time, LLM usage) and print the summary, never document text or keys.

Environment: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE (optional); record also LLM_GATEWAY_URL,
LLM_GATEWAY_API_KEY, GRAPH_LLM_MODEL, GRAPH_SMALL_MODEL, GRAPH_EMBEDDING_MODEL, GRAPH_EMBEDDING_DIM.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import time
from array import array
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TypeVar

from pydantic import BaseModel, SecretStr

from example_lib import run_example
from graph.build import BuildContext, BuildOptions, BuildResult, open_file_stores, run_build
from graph.build.verify import verify_project
from graph.core import Gateway, LLMConfig, Neo4jConfig, connect, ensure_schema, load_ontology
from graph.core.config import DEFAULT_BUDGETS, CallBudget, CallClass
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm import UsageSnapshot, strict_json_schema, untrusted
from graph.core.llm.usage import UsageRecorder
from logger.logger import INFO, get_logger, log_event

logger = get_logger("examples.graph_bench")

T = TypeVar("T", bound=BaseModel)

CASSETTE_VERSION: Final = 1
LLM_ENVIRONMENT: Final = (
    "LLM_GATEWAY_URL",
    "LLM_GATEWAY_API_KEY",
    "GRAPH_LLM_MODEL",
    "GRAPH_SMALL_MODEL",
    "GRAPH_EMBEDDING_MODEL",
    "GRAPH_EMBEDDING_DIM",
)
NEO4J_ENVIRONMENT: Final = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")
REPLAY_ENDPOINT: Final = "http://replay.invalid/v1"
"""Placeholder endpoint of a replay's ``LLMConfig``: the replay gateway never opens a connection."""
EMBED_PROMPT: Final = "embed"
DEFAULT_UNIT_TOKENS: Final = BuildOptions().unit_tokens
"""Extraction unit size of the library default; a recording with another size stores it in its header."""

# Snapshot: properties left out because they differ between runs (stamps, build ids, vectors).
SKIPPED_PROPERTIES: Final = (
    "embedding",
    "name_embedding",
    "created_at",
    "updated_at",
    "inferred_at",
    "created_build_id",
    "last_build_id",
)
STAMP_KEYS_IN_JSON: Final = frozenset({"at"})
"""Keys dropped inside JSON-text properties (a statement's ``at`` is its write time)."""


class CassetteMissError(RuntimeError):
    """Replay was asked for a request the cassette does not hold (names the prompt and call class only)."""


class CassetteError(RuntimeError):
    """The cassette file is unusable (wrong version, header mismatch, broken line)."""


# Request keys


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def structured_key(
    call_class: str,
    model: str,
    prompt: str,
    messages: Sequence[Mapping[str, Any]],
    output_type: type[BaseModel],
) -> str:
    """Key of a ``structured`` call: its semantic content only."""

    return digest(
        {
            "v": CASSETTE_VERSION,
            "kind": "structured",
            "call_class": call_class,
            "model": model,
            "prompt": prompt,
            "messages": [dict(message) for message in messages],
            "schema": strict_json_schema(output_type),
        }
    )


def embed_key(model: str, text: str) -> str:
    """Key of one embedded text (batching is not part of a request's meaning)."""

    return digest({"v": CASSETTE_VERSION, "kind": "embed", "model": model, "text": text})


def encode_vector(vector: Sequence[float]) -> str:
    return base64.b64encode(array("f", vector).tobytes()).decode("ascii")


def decode_vector(encoded: str) -> list[float]:
    values = array("f")
    values.frombytes(base64.b64decode(encoded))
    return values.tolist()


# Cassette


def header_of(config: LLMConfig, unit_tokens: int = DEFAULT_UNIT_TOKENS) -> dict[str, Any]:
    """The cassette header: models, dimension and build unit size, never the endpoint or the key."""

    return {
        "kind": "header",
        "version": CASSETTE_VERSION,
        "main_model": config.main_model,
        "small_model": config.small_model,
        "judge_model": config.judge_model,
        "embedding_model": config.embedding_model,
        "embedding_dim": config.embedding_dim,
        "unit_tokens": unit_tokens,
    }


def config_from_header(header: Mapping[str, Any]) -> LLMConfig:
    return LLMConfig(
        endpoint=REPLAY_ENDPOINT,
        api_key=SecretStr("replay"),
        main_model=header["main_model"],
        small_model=header["small_model"],
        judge_model=header.get("judge_model"),
        embedding_model=header["embedding_model"],
        embedding_dim=int(header["embedding_dim"]),
    )


class Cassette:
    """A JSONL file of ``{"key", "kind", "request", "response" | "error"}`` lines after a header line.

    The first stored answer of a key wins; later puts of the same key are ignored.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.header: dict[str, Any] | None = None
        self.entries: dict[str, dict[str, Any]] = {}
        self.duplicates = 0

    @classmethod
    def load(cls, path: Path) -> Cassette:
        cassette = cls(path)
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError as error:
                    raise CassetteError(f"cassette line {number} is not JSON") from error
                if item.get("kind") == "header":
                    if item.get("version") != CASSETTE_VERSION:
                        raise CassetteError("cassette version unsupported")
                    cassette.header = item
                elif item["key"] in cassette.entries:
                    cassette.duplicates += 1
                else:
                    cassette.entries[item["key"]] = item
        if cassette.header is None:
            raise CassetteError("cassette has no header")
        return cassette

    def open_for_append(self, config: LLMConfig, unit_tokens: int = DEFAULT_UNIT_TOKENS) -> None:
        """Start the file with its header, or check an existing header against ``config``."""

        header = header_of(config, unit_tokens)
        if self.header is not None and self.header != header:
            raise CassetteError("cassette header differs from the recording configuration")
        self.header = header
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._append(header)

    def put(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Store ``entry`` unless its key exists; return the stored entry for the key."""

        stored = self.entries.get(entry["key"])
        if stored is not None:
            return stored
        self.entries[entry["key"]] = entry
        self._append(entry)
        return entry

    def _append(self, item: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(item) + "\n")


def request_summary(
    call_class: str, model: str, prompt: str, messages: Sequence[Mapping[str, Any]], output_type: type[BaseModel]
) -> dict[str, Any]:
    chars = sum(len(content) for message in messages if isinstance(content := message.get("content"), str))
    return {
        "call_class": call_class,
        "model": model,
        "prompt": prompt,
        "schema": output_type.__name__,
        "messages": len(messages),
        "chars": chars,
    }


def error_entry(error: LLMError) -> dict[str, Any]:
    return {"reason": error.reason, "fields": dict(error.fields)}


def raise_recorded(error: Mapping[str, Any]) -> None:
    raise LLMError(error["reason"], **error["fields"])


def answer_structured(entry: Mapping[str, Any], output_type: type[T]) -> T:
    if "error" in entry:
        raise_recorded(entry["error"])
    return output_type.model_validate_json(entry["response"]["json"])


# Gateways (duck-typed stand-ins of graph.core.Gateway: structured, embed, usage, config, aclose, untrusted)


class RecordingGateway:
    """Passes every call to the real gateway and stores the answer in the cassette.

    A request the cassette already holds is answered from it (no live call). Answers are returned
    through the same decoding replay uses (structured: validated from the stored JSON; embeddings:
    float32 vectors), so a recording run and its replays see identical values. Fatal errors (rejected
    credentials, wrong endpoint) and unreachable endpoints are never stored.
    """

    untrusted = staticmethod(untrusted)

    def __init__(self, real: Gateway, cassette: Cassette, unit_tokens: int = DEFAULT_UNIT_TOKENS) -> None:
        self._real = real
        self._cassette = cassette
        cassette.open_for_append(real.config, unit_tokens)
        self.unit_tokens = unit_tokens
        self.live_structured = 0
        self.live_embed_texts = 0
        self.live_embed_calls = 0
        self.served = 0
        self.live_by_prompt: dict[str, int] = {}

    @property
    def config(self) -> LLMConfig:
        return self._real.config

    def usage(self) -> UsageSnapshot:
        return self._real.usage()

    async def aclose(self) -> None:
        await self._real.aclose()

    async def structured(
        self,
        call_class: CallClass,
        model: str | None,
        messages: Sequence[Mapping[str, Any]],
        output_type: type[T],
        *,
        prompt: str,
    ) -> T:
        model_name = model or self._real.config.model_for(call_class)
        key = structured_key(call_class, model_name, prompt, messages, output_type)
        stored = self._cassette.entries.get(key)
        if stored is not None:
            self.served += 1
            return answer_structured(stored, output_type)
        entry: dict[str, Any] = {
            "key": key,
            "kind": "structured",
            "request": request_summary(call_class, model_name, prompt, messages, output_type),
        }
        self.live_structured += 1
        self.live_by_prompt[prompt] = self.live_by_prompt.get(prompt, 0) + 1
        try:
            result = await self._real.structured(call_class, model, messages, output_type, prompt=prompt)
        except LLMFatalError:
            raise
        except LLMError as error:
            entry["error"] = error_entry(error)
            self._cassette.put(entry)
            raise
        entry["response"] = {"json": result.model_dump_json(by_alias=True)}
        return answer_structured(self._cassette.put(entry), output_type)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._real.config.embedding_model
        keys = [embed_key(model, text) for text in texts]
        missing: dict[str, str] = {}
        for key, text in zip(keys, texts, strict=True):
            if key not in self._cassette.entries:
                missing.setdefault(key, text)
        self.served += len(texts) - len(missing)
        if missing:
            self.live_embed_calls += 1
            self.live_embed_texts += len(missing)
            vectors = await self._real.embed(list(missing.values()))
            for (key, text), vector in zip(missing.items(), vectors, strict=True):
                self._cassette.put(
                    {
                        "key": key,
                        "kind": "embed",
                        "request": {"call_class": "embedding", "model": model, "prompt": EMBED_PROMPT, "chars": len(text)},
                        "response": {"b64": encode_vector(vector)},
                    }
                )
        return [decode_vector(self._cassette.entries[key]["response"]["b64"]) for key in keys]


class ReplayGateway:
    """Answers only from the cassette. A miss raises ``CassetteMissError`` and is remembered in ``misses``."""

    untrusted = staticmethod(untrusted)

    def __init__(self, cassette: Cassette) -> None:
        if cassette.header is None:
            raise CassetteError("cassette has no header")
        self._cassette = cassette
        self._config = config_from_header(cassette.header)
        self._usage = UsageRecorder()
        self.served = 0
        self.misses: list[str] = []

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def unit_tokens(self) -> int:
        return int(self._cassette.header.get("unit_tokens", DEFAULT_UNIT_TOKENS)) if self._cassette.header else DEFAULT_UNIT_TOKENS

    def usage(self) -> UsageSnapshot:
        return self._usage.snapshot()

    async def aclose(self) -> None:
        return None

    def _miss(self, call_class: str, prompt: str) -> CassetteMissError:
        self.misses.append(f"{call_class}/{prompt}")
        return CassetteMissError(f"cassette miss: call_class={call_class} prompt={prompt}")

    async def structured(
        self,
        call_class: CallClass,
        model: str | None,
        messages: Sequence[Mapping[str, Any]],
        output_type: type[T],
        *,
        prompt: str,
    ) -> T:
        model_name = model or self._config.model_for(call_class)
        key = structured_key(call_class, model_name, prompt, messages, output_type)
        entry = self._cassette.entries.get(key)
        if entry is None:
            raise self._miss(call_class, prompt)
        self.served += 1
        started = time.monotonic()
        failed = "error" in entry
        try:
            return answer_structured(entry, output_type)
        finally:
            self._usage.call(call_class, prompt, duration_s=time.monotonic() - started, failed=failed)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._config.embedding_model
        vectors: list[list[float]] = []
        for text in texts:
            entry = self._cassette.entries.get(embed_key(model, text))
            if entry is None:
                raise self._miss("embedding", EMBED_PROMPT)
            vectors.append(decode_vector(entry["response"]["b64"]))
        self.served += len(texts)
        self._usage.call("embedding", EMBED_PROMPT, duration_s=0.0, failed=False)
        return vectors


# Snapshot


def _strip_stamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_stamps(item) for key, item in value.items() if key not in STAMP_KEYS_IN_JSON}
    if isinstance(value, list):
        return [_strip_stamps(item) for item in value]
    return value


def normalize_property(name: str, value: Any) -> Any:
    """A property value in JSON-friendly canonical form: JSON text is parsed (stamps inside dropped),
    anything not JSON-native becomes its string form."""

    if name == "statements" and isinstance(value, str):
        try:
            return _strip_stamps(json.loads(value))
        except json.JSONDecodeError:
            return value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [normalize_property(name, item) for item in value]
    return str(value)


def clean_properties(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: normalize_property(name, value)
        for name, value in sorted(raw.items())
        if name not in SKIPPED_PROPERTIES and name != "group_id"
    }


def node_identity(properties: Mapping[str, Any]) -> str:
    for name in ("spec_id", "uuid", "document_id"):
        value = properties.get(name)
        if isinstance(value, str):
            return value
    return ""


def build_snapshot(project: str, node_rows: Sequence[Mapping[str, Any]], edge_rows: Sequence[Mapping[str, Any]]) -> dict:
    """The canonical snapshot of rows read from a store: nodes and edges sorted, volatile properties removed.

    A node row has ``labels`` and ``props`` (all properties); an edge row has ``type``, ``source`` and
    ``target`` (the end nodes' properties) and ``props``.
    """

    nodes = []
    for row in node_rows:
        props = clean_properties(row["props"])
        nodes.append({"labels": sorted(row["labels"]), "spec_id": node_identity(row["props"]), "properties": props})
    nodes.sort(key=lambda node: (node["spec_id"], canonical_json(node)))
    edges = []
    for row in edge_rows:
        props = clean_properties(row["props"])
        edges.append(
            {
                "type": row["type"],
                "edge_key": row["props"].get("edge_key"),
                "source": node_identity(row["source"]),
                "target": node_identity(row["target"]),
                "properties": props,
            }
        )
    edges.sort(key=lambda edge: (edge["type"], edge["source"], edge["target"], canonical_json(edge)))
    return {"project_id": project, "nodes": nodes, "edges": edges}


NODE_QUERY: Final = (
    "MATCH (n {group_id: $p}) RETURN labels(n) AS labels, "
    "[k IN keys(n) WHERE NOT k IN $skip | [k, n[k]]] AS props"
)
EDGE_QUERY: Final = (
    "MATCH (a {group_id: $p})-[r]->(b {group_id: $p}) RETURN type(r) AS type, "
    "[k IN keys(a) WHERE k IN ['spec_id', 'uuid', 'document_id'] | [k, a[k]]] AS source, "
    "[k IN keys(b) WHERE k IN ['spec_id', 'uuid', 'document_id'] | [k, b[k]]] AS target, "
    "[k IN keys(r) WHERE NOT k IN $skip | [k, r[k]]] AS props"
)


async def read_rows(driver: Any, database: str | None, project: str) -> tuple[list[dict], list[dict]]:
    skip = list(SKIPPED_PROPERTIES)
    records, _, _ = await driver.execute_query(NODE_QUERY, {"p": project, "skip": skip}, database_=database)
    nodes = [{"labels": list(record["labels"]), "props": dict(record["props"])} for record in records]
    records, _, _ = await driver.execute_query(EDGE_QUERY, {"p": project, "skip": skip}, database_=database)
    edges = [
        {
            "type": record["type"],
            "source": dict(record["source"]),
            "target": dict(record["target"]),
            "props": dict(record["props"]),
        }
        for record in records
    ]
    return nodes, edges


async def export_snapshot(driver: Any, database: str | None, project: str) -> dict:
    nodes, edges = await read_rows(driver, database, project)
    return build_snapshot(project, nodes, edges)


def snapshot_bytes(snapshot: Mapping[str, Any]) -> bytes:
    return (json.dumps(snapshot, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


# Summary


def usage_summary(usage: UsageSnapshot) -> dict[str, Any]:
    total = usage.total()
    return {
        "calls": total.calls,
        "failures": total.failures,
        "requests": total.requests,
        "input_tokens": total.input_tokens,
        "output_tokens": total.output_tokens,
        "by_class": {
            call_class: usage.total(call_class=call_class).model_dump(exclude={"duration_s"})
            for call_class in sorted({entry.call_class for entry in usage.entries})
        },
        "by_prompt": {
            f"{entry.call_class}/{entry.prompt}": entry.model_dump(exclude={"call_class", "prompt", "duration_s"})
            for entry in usage.entries
        },
    }


def result_summary(result: BuildResult) -> dict[str, Any]:
    metrics = result.metrics
    return {
        "project_id": result.project_id,
        "build_id": result.build_id,
        "mode": result.mode,
        "status": result.status,
        "settled": result.settled,
        "failed_step": result.failed_step,
        "error_code": result.error_code,
        "spec_changes": {name: len(values) for name, values in result.spec_changes.model_dump().items() if values},
        "skipped_fragments": dict(sorted(result.skipped_fragments.items())),
        "follow_up_lineages": list(result.verification.follow_up_lineages) if result.verification else [],
        "fragments_by_status": metrics.fragments_by_status,
        "units_extracted": metrics.units_extracted,
        "units_reused": metrics.units_reused,
        "nodes_by_label": metrics.nodes_by_label,
        "edges_by_relation": metrics.edges_by_relation,
        "dropped_edges": metrics.dropped_edges,
        "exclusive_conflicts": metrics.exclusive_conflicts,
        "attribution_fallbacks": metrics.attribution_fallbacks,
        "duration_s": metrics.duration_s,
    }


def snapshot_counts(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    by_label: dict[str, int] = {}
    for node in snapshot["nodes"]:
        label = next((name for name in node["labels"] if name != "SpecNode"), "SpecNode")
        by_label[label] = by_label.get(label, 0) + 1
    by_type: dict[str, int] = {}
    for edge in snapshot["edges"]:
        by_type[edge["type"]] = by_type.get(edge["type"], 0) + 1
    return {
        "nodes": len(snapshot["nodes"]),
        "edges": len(snapshot["edges"]),
        "nodes_by_label": dict(sorted(by_label.items())),
        "edges_by_type": dict(sorted(by_type.items())),
    }


# Neo4j and the environment


def require_environment(names: Sequence[str]) -> dict[str, str]:
    missing = [name for name in names if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit("missing environment variables: " + ", ".join(missing))
    return {name: os.environ[name].strip() for name in names}


def neo4j_config(env: Mapping[str, str]) -> Neo4jConfig:
    return Neo4jConfig(
        uri=env["NEO4J_URI"],
        user=env["NEO4J_USER"],
        password=SecretStr(env["NEO4J_PASSWORD"]),
        database=os.environ.get("NEO4J_DATABASE", "").strip() or None,
    )


def capped_budgets(timeout_s: float | None, min_attempts: int = 1) -> dict[CallClass, CallBudget]:
    """The default budgets with every chat class's per-attempt timeout set to ``timeout_s`` (None: defaults) and
    at least ``min_attempts`` attempts per call. The proxy cuts a connection at 300 s, so a slow call should
    fail as a timeout below that, not as a dropped connection."""

    return {
        call_class: CallBudget(
            timeout_s=timeout_s if timeout_s is not None and call_class != "embedding" else budget.timeout_s,
            attempts=max(budget.attempts, min_attempts),
        )
        for call_class, budget in DEFAULT_BUDGETS.items()
    }


def llm_config(env: Mapping[str, str], timeout_s: float | None = None, min_attempts: int = 1) -> LLMConfig:
    return LLMConfig(
        budgets=capped_budgets(timeout_s, min_attempts),
        endpoint=env["LLM_GATEWAY_URL"],
        api_key=SecretStr(env["LLM_GATEWAY_API_KEY"]),
        main_model=env["GRAPH_LLM_MODEL"],
        small_model=env["GRAPH_SMALL_MODEL"],
        embedding_model=env["GRAPH_EMBEDDING_MODEL"],
        embedding_dim=int(env["GRAPH_EMBEDDING_DIM"]),
    )


async def wipe_project(driver: Any, database: str | None, project: str) -> int:
    """Delete every node (and relationship) of the project by ``group_id``; returns the node count."""

    deleted = 0
    while True:
        records, _, _ = await driver.execute_query(
            "MATCH (n {group_id: $p}) WITH n LIMIT 2000 DETACH DELETE n RETURN count(*) AS c",
            {"p": project},
            database_=database,
        )
        count = int(records[0]["c"])
        if not count:
            return deleted
        deleted += count


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("record", "build with the real gateway and record a cassette"), ("replay", "build from a cassette")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--project", required=True, help="project id (graph partition)")
        command.add_argument("--corpus", required=True, type=Path, help="folder of .md documents")
        command.add_argument("--state", required=True, type=Path, help="folder of the file stores")
        command.add_argument("--cassette", required=True, type=Path, help="cassette file (.jsonl)")
        command.add_argument("--out", required=True, type=Path, help="folder for the snapshot and summary files")
        command.add_argument("--label", required=True, help="file name stem of the outputs, e.g. v1")
        command.add_argument("--wipe", action="store_true", help="delete the project from Neo4j first (needs an empty state)")
        command.add_argument("--rebuild", action="store_true", help="ask for a rebuild")
        command.add_argument(
            "--call-timeout-s",
            type=float,
            default=None,
            help="record only: per-attempt timeout of every chat call (keep below the proxy's 300 s cut)",
        )
        command.add_argument(
            "--unit-tokens",
            type=int,
            default=None,
            help=f"extraction unit size (record: default {DEFAULT_UNIT_TOKENS}, stored in the cassette header; "
            "replay: must equal the header's value)",
        )
        command.add_argument("--min-attempts", type=int, default=1, help="record only: at least this many attempts per LLM call")
    return parser.parse_args(argv)


def check_wipe(args: argparse.Namespace) -> None:
    if args.wipe and args.state.exists() and any(args.state.iterdir()):
        raise SystemExit("--wipe needs a missing or empty --state folder (stale state does not fit a wiped graph)")


def open_gateway(args: argparse.Namespace) -> tuple[Any, LLMConfig]:
    """The recording or replaying gateway of the command, and the ``LLMConfig`` the build runs with."""

    if args.command == "record":
        config = llm_config(require_environment(LLM_ENVIRONMENT), args.call_timeout_s, args.min_attempts)
        cassette = Cassette.load(args.cassette) if args.cassette.exists() else Cassette(args.cassette)
        unit_tokens = args.unit_tokens or DEFAULT_UNIT_TOKENS
        return RecordingGateway(Gateway(config), cassette, unit_tokens), config
    if not args.cassette.exists():
        raise SystemExit("replay needs an existing --cassette file")
    gateway = ReplayGateway(Cassette.load(args.cassette))
    if args.unit_tokens is not None and args.unit_tokens != gateway.unit_tokens:
        raise SystemExit(f"--unit-tokens {args.unit_tokens} differs from the cassette's {gateway.unit_tokens}")
    return gateway, gateway.config


def llm_report(gateway: Any) -> dict[str, Any]:
    report: dict[str, Any] = {"usage": usage_summary(gateway.usage()), "served_from_cassette": gateway.served}
    if isinstance(gateway, RecordingGateway):
        report |= {
            "live_structured_calls": gateway.live_structured,
            "live_embed_calls": gateway.live_embed_calls,
            "live_embed_texts": gateway.live_embed_texts,
            "live_structured_by_prompt": dict(sorted(gateway.live_by_prompt.items())),
        }
    else:
        report["cassette_misses"] = list(gateway.misses)
    return report


async def main() -> None:
    args = parse_args()
    check_wipe(args)
    neo4j = neo4j_config(require_environment(NEO4J_ENVIRONMENT))
    gateway, llm = open_gateway(args)
    ontology = load_ontology()
    unit_tokens = gateway.unit_tokens
    stores = open_file_stores(args.state, args.corpus)
    if stores.corpus is None:
        raise RuntimeError("file stores were opened without a corpus")
    driver = connect(neo4j)
    try:
        wiped = await wipe_project(driver, neo4j.database, args.project) if args.wipe else None
        created = await ensure_schema(driver, ontology, llm, neo4j.database)
        log_event(logger, INFO, "example.graph_bench.schema", created=len(created), command=args.command)
        ctx = BuildContext(
            project_id=args.project,
            ontology=ontology,
            driver=driver,
            gateway=gateway,
            registry=stores.registry,
            corpus=stores.corpus,
            extraction=stores.extraction,
            builds=stores.builds,
            rebuild=args.rebuild,
            options=BuildOptions(unit_tokens=unit_tokens),
            database=neo4j.database,
        )
        started = time.monotonic()
        result = await run_build(ctx)
        wall_s = time.monotonic() - started
        verification = await verify_project(ctx.with_build_id(result.build_id))
        snapshot = await export_snapshot(driver, neo4j.database, args.project)
        data = snapshot_bytes(snapshot)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{args.label}.snapshot.json").write_bytes(data)
        summary = {
            "command": args.command,
            "label": args.label,
            "project_id": args.project,
            "unit_tokens": unit_tokens,
            "wiped_nodes": wiped,
            "wall_s": round(wall_s, 2),
            "build": result_summary(result),
            "verify": verification.model_dump(mode="json"),
            "snapshot": {"sha256": hashlib.sha256(data).hexdigest(), **snapshot_counts(snapshot)},
            "llm": llm_report(gateway),
        }
        (args.out / f"{args.label}.summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
        if isinstance(gateway, ReplayGateway) and gateway.misses:
            raise CassetteMissError(f"{len(gateway.misses)} cassette misses during replay")
    finally:
        await gateway.aclose()
        await driver.close()


if __name__ == "__main__":
    raise SystemExit(run_example(main))
