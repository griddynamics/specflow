"""Offline tests of the benchmark record/replay gateways and the snapshot normalisation (examples/graph_bench.py)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

import graph_bench
from graph.core import LLMConfig
from graph.core.config import DEFAULT_BUDGETS
from graph.core.errors import LLMError, LLMFatalError
from graph.core.llm.usage import UsageRecorder
from graph.stores import StoreConfig
from graph_bench import (
    Cassette,
    CassetteError,
    CassetteMissError,
    RecordingGateway,
    ReplayGateway,
    build_snapshot,
    capped_budgets,
    check_output_paths,
    check_question_arguments,
    check_wipe,
    decode_vector,
    embed_key,
    encode_vector,
    first_rank,
    load_questions,
    open_gateway,
    rank_metrics,
    require_ignored_path,
    structured_key,
    wipe_notice,
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

    async def structured(
        self, call_class: str, model: str | None, messages: Any, output_type: Any, *, prompt: str
    ) -> Any:
        del call_class, model, prompt
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
    recorder = RecordingGateway(real, Cassette(path))
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
    await RecordingGateway(FakeReal(), Cassette(path)).structured("extraction", None, MESSAGES, Answer, prompt="nodes")
    replay = ReplayGateway(Cassette.load(path))
    with pytest.raises(CassetteMissError, match=r"call_class=small prompt=edges"):
        await replay.structured("small", None, MESSAGES, Answer, prompt="edges")
    with pytest.raises(CassetteMissError, match=r"call_class=embedding prompt=embed"):
        await replay.embed(["never recorded"])
    assert replay.misses == ["small/edges", "embedding/embed"]


async def test_cassette_holds_no_secret_or_url(tmp_path: Path) -> None:
    path = tmp_path / "v1.jsonl"
    recorder = RecordingGateway(FakeReal(), Cassette(path))
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
    recorder = RecordingGateway(real, Cassette(path))
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
    await RecordingGateway(FakeReal(), Cassette(path)).structured("small", None, MESSAGES, Answer, prompt="p")
    real = FakeReal()
    resumed = RecordingGateway(real, Cassette.load(path))
    await resumed.structured("small", None, MESSAGES, Answer, prompt="p")
    assert real.structured_calls == 0 and resumed.served == 1
    with pytest.raises(CassetteError):
        RecordingGateway(FakeReal(config(main_model="other")), Cassette.load(path))


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
        {
            "type": "USES",
            "edge_key": "k",
            "source": "MOD::B",
            "target": "MOD::A",
            "properties": {"edge_key": "k", "fact": "f"},
        }
    ]
    same = build_snapshot("p", list(reversed(nodes)), edges)
    assert same == snapshot


def test_capped_budgets_and_unit_tokens_header(tmp_path: Path) -> None:
    capped = capped_budgets(270.0, 6)
    assert capped["extraction"].timeout_s == 270.0 and capped["small"].timeout_s == 270.0
    assert capped["embedding"].timeout_s == DEFAULT_BUDGETS["embedding"].timeout_s
    assert capped["extraction"].attempts == 6
    path = tmp_path / "v1.jsonl"
    RecordingGateway(FakeReal(), Cassette(path), 10000)
    replay = ReplayGateway(Cassette.load(path))
    assert replay.unit_tokens == 10000
    with pytest.raises(CassetteError):
        RecordingGateway(FakeReal(), Cassette.load(path), 8000)


def test_question_file_and_rank_metrics(tmp_path: Path) -> None:
    path = tmp_path / "questions.yaml"
    path.write_text(
        "- text: how are runs cancelled\n  spec_id: FR::A\n- question: what is a gate\n  spec_ids: [FR::B, FR::C]\n",
        encoding="utf-8",
    )
    questions = load_questions(path)
    assert questions == [
        {"text": "how are runs cancelled", "spec_ids": ["FR::A"]},
        {"text": "what is a gate", "spec_ids": ["FR::B", "FR::C"]},
    ]
    assert first_rank(["X", "FR::C", "FR::B"], ["FR::B", "FR::C"]) == 2
    assert rank_metrics([1, 3, None, 6]) == {
        "queries": 4,
        "top1": 1,
        "top5": 2,
        "mrr": round((1 + 1 / 3 + 1 / 6) / 4, 4),
    }


def test_wipe_is_neo4j_only(tmp_path: Path) -> None:
    arcade = StoreConfig(backend="arcadedb", uri="http://x", user="u", password=SecretStr("p"))
    with pytest.raises(SystemExit, match="Neo4j only"):
        check_wipe(SimpleNamespace(wipe=True, state=tmp_path), arcade)
    neo = StoreConfig(backend="neo4j", uri="bolt://x", user="u", password=SecretStr("p"))
    (tmp_path / "stale").write_text("x")
    with pytest.raises(SystemExit, match="empty"):
        check_wipe(SimpleNamespace(wipe=True, state=tmp_path), neo)
    check_wipe(SimpleNamespace(wipe=False, state=tmp_path), arcade)


def write_cassette(path: Path, *lines: str) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def header_line(**overrides: Any) -> str:
    header = {
        "kind": "header",
        "version": 1,
        "main_model": "m",
        "small_model": "s",
        "judge_model": None,
        "embedding_model": "e",
        "embedding_dim": 4,
        "unit_tokens": 100,
    }
    return json.dumps(header | overrides)


def structured_line(key: str = "k1", **overrides: Any) -> str:
    entry = {"key": key, "kind": "structured", "request": {}, "response": {"json": '{"text":"a"}'}}
    return json.dumps(entry | overrides)


def test_cassette_load_error_cases(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    cases = {
        "has no header": [],
        "unsupported": [header_line(version=99)],
        "lacks small_model": [json.dumps({"kind": "header", "version": 1, "main_model": "m"})],
        "line 2 is not JSON": [header_line(), "{broken", structured_line()],
        "not an object": [header_line(), "[1]"],
        "without a key": [header_line(), json.dumps({"kind": "structured", "response": {"json": "{}"}})],
        "unknown entry kind": [header_line(), structured_line(kind="other")],
        "exactly one of": [header_line(), json.dumps({"key": "k", "kind": "structured"})],
        "malformed error": [header_line(), structured_line(response=None, error={"reason": 1})],
        "lacks json": [header_line(), structured_line(response={"x": 1})],
        "entry before the header": [structured_line(), header_line()],
        "second, different header": [header_line(), header_line(main_model="other")],
        "not base64": [header_line(), json.dumps({"key": "k", "kind": "embed", "response": {"b64": "!!"}})],
    }
    for message, lines in cases.items():
        write_cassette(path, *lines)
        with pytest.raises(CassetteError, match=message):
            Cassette.load(path)


async def test_cassette_vector_must_match_the_header_dimension(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    entry = {"key": "k", "kind": "embed", "response": {"b64": encode_vector([0.1, 0.2, 0.3])}}
    write_cassette(path, header_line(), json.dumps(entry))
    with pytest.raises(CassetteError, match="3 values, header says 4"):
        Cassette.load(path)
    # a gateway whose vectors are 4 long while its config says 5 is refused before anything is stored
    recorder = RecordingGateway(FakeReal(config(embedding_dim=5)), Cassette(tmp_path / "n.jsonl"))
    with pytest.raises(CassetteError, match="embedding_dim=5"):
        await recorder.embed(["x"])
    assert len((tmp_path / "n.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_header_dimension_mismatch_against_the_recording_configuration(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    RecordingGateway(FakeReal(), Cassette(path))
    with pytest.raises(CassetteError, match="differs"):
        RecordingGateway(FakeReal(config(embedding_dim=8)), Cassette.load(path))


def test_partial_last_line_is_tolerated_and_truncated_on_resume(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "c.jsonl"
    good = structured_line("k1")
    path.write_text(header_line() + "\n" + good + "\n" + '{"key": "k2", "kind": "struct', encoding="utf-8")
    cassette = Cassette.load(path)
    assert list(cassette.entries) == ["k1"] and cassette.partial_tail
    assert "incomplete line" in capsys.readouterr().err
    replay = ReplayGateway(cassette)
    assert replay.config.main_model == "m"
    cassette.open_for_append(config(main_model="m", small_model="s", embedding_model="e", embedding_dim=4), 100)
    assert path.read_text(encoding="utf-8") == header_line() + "\n" + good + "\n"
    assert "truncated" in capsys.readouterr().err


def test_a_broken_line_in_the_middle_is_an_error_not_a_partial_tail(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    write_cassette(path, header_line(), "{broken", structured_line())
    with pytest.raises(CassetteError, match="line 2"):
        Cassette.load(path)
    path.write_text(header_line() + "\n" + "{broken\n", encoding="utf-8")
    with pytest.raises(CassetteError, match="line 2"):
        Cassette.load(path)


def test_first_answer_wins(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    write_cassette(
        path,
        header_line(),
        structured_line("k1", response={"json": '{"text":"first"}'}),
        structured_line("k1", response={"json": '{"text":"second"}'}),
    )
    loaded = Cassette.load(path)
    assert loaded.duplicates == 1 and loaded.entries["k1"]["response"]["json"] == '{"text":"first"}'
    stored = loaded.put({"key": "k1", "kind": "structured", "response": {"json": '{"text":"third"}'}})
    assert stored["response"]["json"] == '{"text":"first"}'
    assert path.read_text(encoding="utf-8").count("third") == 0


def test_open_gateway_checks_unit_tokens_and_the_cassette_file(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    RecordingGateway(FakeReal(), Cassette(path), 10000)
    gateway, _ = open_gateway(SimpleNamespace(command="replay", cassette=path, unit_tokens=None))
    assert gateway.unit_tokens == 10000
    open_gateway(SimpleNamespace(command="replay", cassette=path, unit_tokens=10000))
    with pytest.raises(SystemExit, match="differs from the cassette's 10000"):
        open_gateway(SimpleNamespace(command="replay", cassette=path, unit_tokens=8000))
    with pytest.raises(SystemExit, match="existing --cassette"):
        open_gateway(SimpleNamespace(command="replay", cassette=tmp_path / "missing.jsonl", unit_tokens=None))


async def test_recorder_never_stores_the_key_or_the_endpoint_even_when_an_error_echoes_them(tmp_path: Path) -> None:
    path = tmp_path / "v1.jsonl"
    real = FakeReal()
    recorder = RecordingGateway(real, Cassette(path))
    real.fail = LLMError(
        "refused",
        prompt="p",
        call_class="small",
        model="m-small",
        detail=f"POST {ENDPOINT}/chat failed for key {API_KEY} at llm.example.invalid",
    )
    with pytest.raises(LLMError):
        await recorder.structured("small", None, MESSAGES, Answer, prompt="p")
    text = path.read_text(encoding="utf-8")
    for forbidden in (API_KEY, ENDPOINT, "llm.example.invalid"):
        assert forbidden not in text
    assert "[redacted]" in text
    replay = ReplayGateway(Cassette.load(path))
    with pytest.raises(LLMError) as replayed:
        await replay.structured("small", None, MESSAGES, Answer, prompt="p")
    assert replayed.value.fields["prompt"] == "p" and API_KEY not in str(replayed.value.fields)


def test_output_paths_inside_the_repository_must_be_under_the_ignored_bench_folder(tmp_path: Path) -> None:
    outputs = graph_bench.OUTPUT_ROOT
    assert outputs == graph_bench.REPOSITORY / ".graph-bench"
    require_ignored_path(outputs / "run" / "v1.summary.json", "--out")
    with pytest.raises(SystemExit, match="must be under"):
        require_ignored_path(graph_bench.REPOSITORY / "reports" / "v1.summary.json", "--out")
    with pytest.raises(SystemExit, match="must be under"):
        require_ignored_path(graph_bench.REPOSITORY / "backend" / "v1.jsonl", "--cassette")
    require_ignored_path(tmp_path / "outside" / "v1.jsonl", "--cassette")  # outside the repository: anything
    args = SimpleNamespace(
        command="record",
        out=graph_bench.REPOSITORY / "reports",
        label="v1",
        cassette=outputs / "v1.jsonl",
        record_embeddings=False,
    )
    with pytest.raises(SystemExit, match="--out"):
        check_output_paths(args)
    args.out = outputs / "reports"
    check_output_paths(args)
    args.cassette = graph_bench.REPOSITORY / "cassettes" / "v1.jsonl"
    with pytest.raises(SystemExit, match="--cassette"):
        check_output_paths(args)


def test_the_bench_folder_is_git_ignored() -> None:
    """The guard above lets outputs into .graph-bench/ only because the root .gitignore ignores that folder."""

    lines = (graph_bench.REPOSITORY / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".graph-bench/" in lines


def test_question_arguments_fail_loudly(tmp_path: Path) -> None:
    questions = tmp_path / "q.yaml"
    questions.write_text("- text: a\n  spec_id: X\n", encoding="utf-8")
    cassette = tmp_path / "qa.jsonl"
    base = {"questions_meta": None, "qa_cassette": None, "record_embeddings": False}
    with pytest.raises(SystemExit, match="does not exist"):
        check_question_arguments(SimpleNamespace(command="replay", questions=tmp_path / "none.yaml", **base))
    with pytest.raises(SystemExit, match="needs --qa-cassette"):
        check_question_arguments(SimpleNamespace(command="replay", questions=questions, **base))
    with pytest.raises(SystemExit, match="--qa-cassette"):
        check_question_arguments(
            SimpleNamespace(command="replay", questions=questions, **(base | {"qa_cassette": cassette}))
        )
    with pytest.raises(SystemExit, match="--questions-meta"):
        check_question_arguments(
            SimpleNamespace(command="qa", questions=None, **(base | {"questions_meta": tmp_path / "m.yaml"}))
        )
    cassette.write_text("x", encoding="utf-8")
    check_question_arguments(
        SimpleNamespace(command="replay", questions=questions, **(base | {"qa_cassette": cassette}))
    )
    check_question_arguments(SimpleNamespace(command="replay", questions=None, **base))


def test_wipe_notice_names_backend_host_and_project_without_credentials() -> None:
    neo = StoreConfig(backend="neo4j", uri="bolt://user:hunter2@db.internal:7688", user="u", password=SecretStr("pw"))
    notice = wipe_notice(neo, "bench-demo")
    assert "neo4j" in notice and "db.internal:7688" in notice and "'bench-demo'" in notice
    assert "hunter2" not in notice and "pw" not in notice.replace("project", "")
