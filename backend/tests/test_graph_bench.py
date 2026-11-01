"""Offline tests of the benchmark record/replay gateways and the snapshot normalisation (examples/graph_bench.py)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from graph.core import LLMConfig
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm.usage import UsageRecorder
from graph_bench import (
    Cassette,
    CassetteError,
    CassetteMissError,
    RecordingGateway,
    ReplayGateway,
    build_snapshot,
    decode_vector,
    embed_key,
    encode_vector,
    structured_key,
)

API_KEY = "sk-test-not-a-real-key"
ENDPOINT = "https://llm.example.invalid/v1"


class Answer(BaseModel):
    text: str
    count: int = 0


class Other(BaseModel):
    text: str


def config(**overrides: Any) -> LLMConfig:
    values: dict[str, Any] = {
        "endpoint": ENDPOINT,
        "api_key": SecretStr(API_KEY),
        "main_model": "m-main",
        "small_model": "m-small",
        "embedding_model": "m-embed",
        "embedding_dim": 4,
    }
    values.update(overrides)
    return LLMConfig(**values)


class FakeReal:
    """Stands in for the real Gateway: answers by the user message, counts live calls."""

    def __init__(self, cfg: LLMConfig | None = None) -> None:
        self.config = cfg or config()
        self.structured_calls = 0
        self.embed_calls: list[list[str]] = []
        self.fail: Exception | None = None
        self.closed = False

    def usage(self) -> Any:
        return UsageRecorder().snapshot()

    async def aclose(self) -> None:
        self.closed = True

    async def structured(self, call_class: str, model: str | None, messages: Any, output_type: Any, *, prompt: str) -> Any:
        self.structured_calls += 1
        if self.fail is not None:
            raise self.fail
        return output_type(text=f"answer:{messages[-1]['content']}")

    async def embed(self, texts: Any) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [[len(text) / 10, 0.1, 0.2, 0.3] for text in texts]


MESSAGES = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}]


def test_key_depends_on_semantics_only() -> None:
    base = structured_key("extraction", "m", "p", MESSAGES, Answer)
    assert base == structured_key("extraction", "m", "p", [dict(m) for m in MESSAGES], Answer)
    assert base != structured_key("small", "m", "p", MESSAGES, Answer)
    assert base != structured_key("extraction", "m2", "p", MESSAGES, Answer)
    assert base != structured_key("extraction", "m", "q", MESSAGES, Answer)
    assert base != structured_key("extraction", "m", "p", MESSAGES[:1], Answer)
    assert base != structured_key("extraction", "m", "p", MESSAGES, Other)
    assert embed_key("e", "a") != embed_key("e", "b")
    assert embed_key("e", "a") != embed_key("e2", "a")


def test_vector_roundtrip_is_float32() -> None:
    vector = decode_vector(encode_vector([0.1, 0.25, -3.0]))
    assert vector == decode_vector(encode_vector(vector))
    assert vector[1] == 0.25


async def test_record_then_replay(tmp_path: Path) -> None:
    path = tmp_path / "c" / "v1.jsonl"
    real = FakeReal()
    recorder = RecordingGateway(real, Cassette(path))  # type: ignore[arg-type]
    first = await recorder.structured("extraction", None, MESSAGES, Answer, prompt="nodes")
    again = await recorder.structured("extraction", None, MESSAGES, Answer, prompt="nodes")
    vectors = await recorder.embed(["aa", "bbbb", "aa"])
    assert first == again == Answer(text="answer:hello")
    assert real.structured_calls == 1
    assert real.embed_calls == [["aa", "bbbb"]]
    assert len(vectors) == 3 and vectors[0] == vectors[2]
    assert recorder.live_structured == 1 and recorder.live_embed_texts == 2

    replay = ReplayGateway(Cassette.load(path))
    assert replay.config.embedding_dim == 4 and replay.config.main_model == "m-main"
    assert await replay.structured("extraction", "m-main", MESSAGES, Answer, prompt="nodes") == first
    assert await replay.embed(["aa", "bbbb", "aa"]) == vectors
    assert replay.misses == []


async def test_replay_miss_names_prompt_and_call_class(tmp_path: Path) -> None:
    path = tmp_path / "v1.jsonl"
    await RecordingGateway(FakeReal(), Cassette(path)).structured(  # type: ignore[arg-type]
        "extraction", None, MESSAGES, Answer, prompt="nodes"
    )
    replay = ReplayGateway(Cassette.load(path))
    with pytest.raises(CassetteMissError, match=r"call_class=small prompt=edges"):
        await replay.structured("small", None, MESSAGES, Answer, prompt="edges")
    with pytest.raises(CassetteMissError, match=r"call_class=embedding prompt=embed"):
        await replay.embed(["never recorded"])
    assert replay.misses == ["small/edges", "embedding/embed"]


async def test_cassette_holds_no_secret_or_url(tmp_path: Path) -> None:
    path = tmp_path / "v1.jsonl"
    recorder = RecordingGateway(FakeReal(), Cassette(path))  # type: ignore[arg-type]
    await recorder.structured("small", None, MESSAGES, Answer, prompt="p")
    await recorder.embed(["x"])
    text = path.read_text(encoding="utf-8")
    for forbidden in (API_KEY, ENDPOINT, "llm.example", "authorization", "api_key", "Bearer"):
        assert forbidden.lower() not in text.lower()
    header = json.loads(text.splitlines()[0])
    assert header["kind"] == "header" and "endpoint" not in header


async def test_recorded_llm_error_replays_and_fatal_is_not_stored(tmp_path: Path) -> None:
    path = tmp_path / "v1.jsonl"
    real = FakeReal()
    recorder = RecordingGateway(real, Cassette(path))  # type: ignore[arg-type]
    real.fail = LLMError("refused", prompt="p", call_class="small", model="m-small")
    with pytest.raises(LLMError) as raised:
        await recorder.structured("small", None, MESSAGES, Answer, prompt="p")
    assert raised.value.reason == "refused"
    real.fail = LLMFatalError(prompt="p2")
    with pytest.raises(LLMFatalError):
        await recorder.structured("small", None, MESSAGES, Answer, prompt="p2")
    replay = ReplayGateway(Cassette.load(path))
    with pytest.raises(LLMError) as replayed:
        await replay.structured("small", None, MESSAGES, Answer, prompt="p")
    assert replayed.value.reason == "refused" and replayed.value.fields["prompt"] == "p"
    with pytest.raises(CassetteMissError):
        await replay.structured("small", None, MESSAGES, Answer, prompt="p2")


async def test_recording_resumes_from_an_existing_cassette(tmp_path: Path) -> None:
    path = tmp_path / "v1.jsonl"
    await RecordingGateway(FakeReal(), Cassette(path)).structured(  # type: ignore[arg-type]
        "small", None, MESSAGES, Answer, prompt="p"
    )
    real = FakeReal()
    resumed = RecordingGateway(real, Cassette.load(path))  # type: ignore[arg-type]
    await resumed.structured("small", None, MESSAGES, Answer, prompt="p")
    assert real.structured_calls == 0 and resumed.served == 1
    with pytest.raises(CassetteError):
        RecordingGateway(FakeReal(config(main_model="other")), Cassette.load(path))  # type: ignore[arg-type]


def test_snapshot_drops_volatile_properties_and_sorts() -> None:
    nodes = [
        {
            "labels": ["SpecNode", "Module"],
            "props": {
                "spec_id": "MOD::B",
                "group_id": "p",
                "uuid": "u2",
                "name": "B",
                "created_at": "t",
                "last_build_id": "b1",
                "embedding": [0.1],
                "statements": json.dumps([{"fragment_id": "f", "text": "x", "at": "2026-01-01"}]),
            },
        },
        {"labels": ["Module", "SpecNode"], "props": {"spec_id": "MOD::A", "name": "A", "updated_at": "t"}},
    ]
    edges = [
        {
            "type": "USES",
            "source": {"spec_id": "MOD::B"},
            "target": {"spec_id": "MOD::A"},
            "props": {"edge_key": "k", "fact": "f", "created_at": "t", "last_build_id": "b"},
        }
    ]
    snapshot = build_snapshot("p", nodes, edges)
    assert [node["spec_id"] for node in snapshot["nodes"]] == ["MOD::A", "MOD::B"]
    assert snapshot["nodes"][0]["labels"] == ["Module", "SpecNode"]
    properties = snapshot["nodes"][1]["properties"]
    assert set(properties) == {"spec_id", "uuid", "name", "statements"}
    assert properties["statements"] == [{"fragment_id": "f", "text": "x"}]
    assert snapshot["edges"] == [
        {"type": "USES", "edge_key": "k", "source": "MOD::B", "target": "MOD::A", "properties": {"edge_key": "k", "fact": "f"}}
    ]
    same = build_snapshot("p", list(reversed(nodes)), edges)
    assert same == snapshot


def test_capped_budgets_and_unit_tokens_header(tmp_path: Path) -> None:
    from graph.core.config import DEFAULT_BUDGETS
    from graph_bench import capped_budgets

    capped = capped_budgets(270.0, 6)
    assert capped["extraction"].timeout_s == 270.0 and capped["small"].timeout_s == 270.0
    assert capped["embedding"].timeout_s == DEFAULT_BUDGETS["embedding"].timeout_s
    assert capped["extraction"].attempts == 6
    path = tmp_path / "v1.jsonl"
    RecordingGateway(FakeReal(), Cassette(path), 10000)  # type: ignore[arg-type]
    replay = ReplayGateway(Cassette.load(path))
    assert replay.unit_tokens == 10000
    with pytest.raises(CassetteError):
        RecordingGateway(FakeReal(), Cassette.load(path), 8000)  # type: ignore[arg-type]
