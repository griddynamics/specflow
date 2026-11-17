"""The tool agent investigator of gap runs (``agent_runtime.gap_investigator``; docs/graph/gap/investigation.md "Tool
agent adapter", FR-GINV-09): instructions, budget, compaction, failures and the runner's selection by settings."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

import httpx
import openai
import pytest
from agents import MaxTurnsExceeded, ModelBehaviorError, OpenAIResponsesModel
from pydantic import SecretStr

from agent_runtime.gap_investigator import (
    PROMPT_VERSION,
    READ_TOOLS,
    TOOLS,
    TOOLS_PARAGRAPH,
    AgentsInvestigator,
    agent_instructions,
    build_agent,
    error_code,
)
from api.gap.runner import investigator_for
from gap_agents_support import AgentCall, ScriptedAgentModel, ToolCall
from gap_host_support import PROJECT, StubGapRun, StubGateway, drained, make_gap_runner
from graph.core.config import LLMConfig
from graph.core.llm.gateway import UNTRUSTED_NOTICE, UNTRUSTED_OPEN
from graph.gap.models import ReadResult, SiteBudget, SitePack
from graph.gap.prompts import INVESTIGATE_PROMPT
from graph.gap.settings import GapSettings
from graph.gap.steps.investigate import LoopInvestigator

BUDGET = SiteBudget(rounds=4, reads_per_round=4, context_tokens=24_000)
PACK = SitePack(project_id="project-1", site_id="SITE::0123456789AB", input_hash="h", mode="full", seeds=())
REPORT = {"verdict": "not_a_gap", "why": "Settled by the text."}
LONG = "word " * 400


class Reads:
    """A read menu that answers every read with one item (``LONG`` text when ``long``), or raises ``fail``."""

    def __init__(self, *, long: bool = False, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.long = long
        self.fail = fail

    async def _answer(self, read: str, *args: Any) -> ReadResult:
        self.calls.append((read, args))
        if self.fail is not None:
            raise self.fail
        return ReadResult(read=read, items=({"spec_id": str(args[0]), "text": LONG if self.long else "short"},))

    async def node(self, spec_id: str) -> ReadResult:
        return await self._answer("node", spec_id)

    async def evidence(self, spec_id: str) -> ReadResult:
        return await self._answer("evidence", spec_id)

    async def neighbours(self, spec_id: str, relations: Sequence[str] = ()) -> ReadResult:
        return await self._answer("neighbours", spec_id, tuple(relations))

    async def trace(self, spec_id: str, direction: str, depth: int) -> ReadResult:
        return await self._answer("trace", spec_id, direction, depth)

    async def paths(self, source: str, target: str) -> ReadResult:
        return await self._answer("paths", source, target)

    async def similar(self, query: str, limit: int) -> ReadResult:
        return await self._answer("similar", query, limit)

    async def find_nodes(self, words: str, labels: Sequence[str] = ()) -> ReadResult:
        return await self._answer("find_nodes", words, tuple(labels))

    async def find_fragments(self, words: str) -> ReadResult:
        return await self._answer("find_fragments", words)


def node_reads(count: int) -> list[ToolCall]:
    return [("node", {"spec_id": f"REQ::{number}"}) for number in range(count)]


def outputs(call: AgentCall) -> list[str]:
    return [
        str(item["output"])
        for item in call.input
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]


async def investigate(script: Any, reads: Any = None, budget: SiteBudget = BUDGET) -> tuple[Any, ScriptedAgentModel]:
    model = ScriptedAgentModel(script)
    result = await AgentsInvestigator(model).investigate(PACK, reads or Reads(), budget)
    return result, model


def test_the_instructions_are_the_investigate_prompt_with_tools_instead_of_json_turns() -> None:
    text = agent_instructions()

    assert TOOLS_PARAGRAPH in text and "The report:" in text and "`verdict`" in text
    assert "Each turn, answer with one JSON object" not in text and '"action": "read"' not in text
    assert text.rstrip().endswith(UNTRUSTED_NOTICE)
    assert PROMPT_VERSION == f"{INVESTIGATE_PROMPT}.agents.1" == AgentsInvestigator.prompt_version


def test_the_read_tools_take_ids_and_words_only() -> None:
    parameters = {tool.name: set(tool.params_json_schema["properties"]) for tool in READ_TOOLS}

    assert parameters == {
        "node": {"spec_id"},
        "evidence": {"spec_id"},
        "neighbours": {"spec_id", "relations"},
        "trace": {"spec_id", "direction", "depth"},
        "paths": {"spec_id", "target"},
        "similar": {"subject", "limit"},
        "find_nodes": {"words", "labels"},
        "find_fragments": {"words"},
    }
    assert all(tool.strict_json_schema for tool in TOOLS)
    assert set(TOOLS[-1].params_json_schema["properties"]) == {"report"}


async def test_a_report_ends_the_investigation_with_the_loop_report_and_its_cost() -> None:
    def script(call: AgentCall) -> list[ToolCall]:
        return [("evidence", {"spec_id": "REQ::A"})] if call.turn == 1 else [("report", {"report": REPORT})]

    reads = Reads()
    result, model = await investigate(script, reads)

    assert result.error is None and result.report is not None and result.report.verdict == "not_a_gap"
    assert (result.model_calls, result.rounds_used, result.tokens) == (2, 2, 220)
    assert [(record.read, dict(record.arguments)) for record in result.reads] == [("evidence", {"spec_id": "REQ::A"})]
    assert reads.calls == [("evidence", ("REQ::A",))]
    first = model.calls[0]
    assert first.tool_choice == "required" and "project-1" not in json.dumps(first.input, default=str)
    (output,) = outputs(model.calls[1])
    assert UNTRUSTED_OPEN in output and '"items": 1' in output


async def test_the_budget_caps_reads_per_turn_and_ends_with_a_report_only_turn() -> None:
    def script(call: AgentCall) -> list[ToolCall]:
        return [("report", {"report": REPORT})] if call.tool_choice == "report" else node_reads(6)

    reads = Reads()
    result, model = await investigate(script, reads)

    assert len(model.calls) == BUDGET.rounds + 1 == result.model_calls
    assert [call.tool_choice for call in model.calls] == ["required"] * 4 + ["report"]
    assert len(reads.calls) == len(result.reads) == BUDGET.rounds * BUDGET.reads_per_round
    last = outputs(model.calls[-1])
    assert sum('"reason": "read_limit"' in output for output in last) == 4 * 2
    assert sum("Call report now" in output for output in last) == 6
    assert result.report is not None and result.rounds_used == BUDGET.rounds


async def test_the_oldest_read_results_are_summarised_when_the_context_runs_full() -> None:
    def script(call: AgentCall) -> list[ToolCall]:
        return node_reads(2) if call.turn < 3 else [("report", {"report": REPORT})]

    result, model = await investigate(script, Reads(long=True), BUDGET.model_copy(update={"context_tokens": 2_600}))

    assert result.report is not None
    third = outputs(model.calls[2])
    assert len(third) == 4 and all(UNTRUSTED_OPEN in output for output in outputs(model.calls[1]))
    summarised = [output.startswith("Read result summarised to save space: node(spec_id): 1 items") for output in third]
    assert summarised[0] and not summarised[-1] and summarised == sorted(summarised, reverse=True)


@pytest.mark.parametrize(
    ("script", "code"),
    [
        (lambda _call: [], "llm_report_missing"),
        (lambda _call: [("report", {"report": {"verdict": "maybe"}})], "llm_malformed"),
        (lambda _call: node_reads(1), "llm_report_missing"),  # reads only: max turns
        (lambda _call: [("no_such_tool", {})], "llm_malformed"),
    ],
)
async def test_a_run_without_a_valid_report_fails_with_a_code(script: Any, code: str) -> None:
    result, _ = await investigate(script)

    assert (result.report, result.error) == (None, code)
    assert result.model_calls >= 1


async def test_a_failing_tool_raises_out_of_the_run_and_never_reaches_the_model() -> None:
    result, model = await investigate(lambda _call: node_reads(1), Reads(fail=RuntimeError("store text")))

    assert (result.report, result.error, len(model.calls)) == (None, "investigator_failed", 1)


def _status(code: int) -> openai.APIStatusError:
    response = httpx.Response(code, request=httpx.Request("POST", "http://gateway"))
    return openai.APIStatusError("failed", response=response, body=None)


def test_failures_map_to_the_loop_codes() -> None:
    request = httpx.Request("POST", "http://gateway")

    assert error_code(MaxTurnsExceeded("turns")) == "llm_report_missing"
    assert error_code(ModelBehaviorError("bad")) == "llm_malformed"
    assert error_code(openai.APIConnectionError(request=request)) == "llm_unreachable"
    assert error_code(_status(401)) == "llm_fatal"
    assert error_code(_status(429)) == "llm_exhausted"
    assert error_code(RuntimeError("x")) == "investigator_failed"


LLM = LLMConfig(
    endpoint="http://gateway.invalid/v1",
    api_key=SecretStr("k"),
    main_model="main-model",
    small_model="small-model",
    embedding_model="embed",
    embedding_dim=8,
)


class ConfigGateway(StubGateway):
    def __init__(self, llm: Any) -> None:
        super().__init__(llm)
        self.config = llm


async def test_the_adapter_uses_the_run_gateway_configuration_and_closes_its_client() -> None:
    investigator = AgentsInvestigator.from_gateway(ConfigGateway(LLM))
    model = investigator._model

    assert isinstance(model, OpenAIResponsesModel) and model.model == "main-model"
    settings = build_agent(model).model_settings
    assert (settings.store, settings.tool_choice) == (False, "required")
    client = model._client
    assert str(client.base_url).startswith("http://gateway.invalid/v1") and client.max_retries == 3
    await investigator.aclose()
    assert client._client.is_closed


async def test_the_runner_runs_the_investigator_the_settings_name(tmp_path: Any, monkeypatch: Any) -> None:
    assert investigator_for("loop") is LoopInvestigator
    run = StubGapRun()
    runner = make_gap_runner(tmp_path, run, gap=GapSettings(investigator="agents"))
    runner._deps = dataclasses.replace(runner._deps, investigator=None, gateway_factory=ConfigGateway)
    closed: list[bool] = []
    original = AgentsInvestigator.aclose

    async def aclose(self: AgentsInvestigator) -> None:
        closed.append(True)
        await original(self)

    monkeypatch.setattr(AgentsInvestigator, "aclose", aclose)

    await runner.after_build(PROJECT, "build-1")
    await drained(runner)

    [context] = run.contexts
    assert isinstance(context.investigator, AgentsInvestigator) and closed == [True]
