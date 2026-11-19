"""A scripted Agents SDK model for the tool agent investigator (``agent_runtime.gap_investigator``), offline.

``ScriptedAgentModel`` answers every model call of an investigation with the tool calls a script returns for the
call's site and turn, and records what each call was offered (tool names, input). ``loop_script`` replays the recorded
answers of the structured loop (``recordings/gap_investigate.json``, by site and round) as tool calls, so both
adapters investigate the shop corpus from the same answers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, override

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall

from gap_investigation_support import answers_by_site

INPUT_TOKENS: Final = 100
OUTPUT_TOKENS: Final = 10

ToolCall = tuple[str, Mapping[str, Any]]


@dataclass(frozen=True)
class AgentCall:
    """One model call: the site, the turn (1 for the first call of an investigation), what it was offered."""

    site_id: str
    turn: int
    tools: tuple[str, ...]
    instructions: str
    input: tuple[Any, ...]
    tool_choice: Any = None


def _items(value: str | list[Any]) -> list[Any]:
    return [{"role": "user", "content": value}] if isinstance(value, str) else list(value)


def _site_of(items: Sequence[Any]) -> str:
    first = items[0]
    text = first["content"] if isinstance(first, Mapping) else first.content
    return str(json.loads(str(text).split("\n\n")[1])["site_id"])


@dataclass
class ScriptedAgentModel(Model):
    """Answers each call with ``script(call)``: a list of tool calls (name, arguments), or a text when empty."""

    script: Callable[[AgentCall], Sequence[ToolCall]]
    calls: list[AgentCall] = field(default_factory=list)
    _numbers: int = 0
    _turns: dict[str, int] = field(default_factory=dict)

    @override
    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        **kwargs: Any,
    ) -> ModelResponse:
        del output_schema, handoffs, tracing, kwargs
        items = _items(input)
        site_id = _site_of(items)
        turn = self._turns.get(site_id, 0) + 1 if any(_is_call(item) for item in items) else 1
        self._turns[site_id] = turn
        call = AgentCall(
            site_id=site_id,
            turn=turn,
            tools=tuple(tool.name for tool in tools),
            instructions=system_instructions or "",
            input=tuple(items),
            tool_choice=model_settings.tool_choice,
        )
        self.calls.append(call)
        output: list[Any] = []
        for name, arguments in self.script(call):
            self._numbers += 1
            output.append(
                ResponseFunctionToolCall(
                    arguments=json.dumps(arguments),
                    call_id=f"call-{self._numbers}",
                    name=name,
                    type="function_call",
                    id=f"fc{self._numbers}",
                    status="completed",
                )
            )
        usage = Usage(
            requests=1,
            input_tokens=INPUT_TOKENS,
            output_tokens=OUTPUT_TOKENS,
            total_tokens=INPUT_TOKENS + OUTPUT_TOKENS,
        )
        return ModelResponse(output=output, usage=usage, response_id=None)

    @override
    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _is_call(item: Any) -> bool:
    return isinstance(item, Mapping) and item.get("type") == "function_call"


def tool_call(read: Mapping[str, Any]) -> ToolCall:
    """The tool call of a loop read request (``WireRead`` as JSON)."""

    name = str(read["read"])
    spec_id, words = read.get("spec_id"), read.get("words")
    arguments: dict[str, Any] = {
        "node": {"spec_id": spec_id},
        "evidence": {"spec_id": spec_id},
        "neighbours": {"spec_id": spec_id, "relations": read.get("relations")},
        "trace": {"spec_id": spec_id, "direction": read.get("direction") or "affects", "depth": read.get("depth") or 2},
        "paths": {"spec_id": spec_id, "target": read.get("target")},
        "similar": {"subject": spec_id or words, "limit": read.get("limit") or 5},
        "find_nodes": {"words": words, "labels": read.get("labels")},
        "find_fragments": {"words": words},
    }[name]
    return name, arguments


def loop_answer_calls(answer: Mapping[str, Any]) -> list[ToolCall]:
    """A recorded loop answer (``LoopTurn``/``FinalTurn`` as JSON) as tool calls."""

    if answer["action"] == "report":
        return [("report", {"report": answer["report"]})]
    return [tool_call(read) for read in answer["reads"]]


def loop_script(call: AgentCall) -> list[ToolCall]:
    """The recorded loop answer of the call's site and turn as tool calls."""

    found = answers_by_site().get((call.site_id, call.turn))
    if found is None:
        raise AssertionError(f"no recorded answer for site {call.site_id} round {call.turn}")
    return loop_answer_calls(found[1])


def schema_names(schema: Any) -> set[str]:
    """Every property name of a JSON schema, nested definitions included."""

    names: set[str] = set()
    if isinstance(schema, Mapping):
        for key, value in schema.items():
            if key == "properties" and isinstance(value, Mapping):
                names.update(str(name) for name in value)
            names |= schema_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= schema_names(value)
    return names
