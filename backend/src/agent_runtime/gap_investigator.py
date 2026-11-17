"""The tool agent ``Investigator`` of gap runs: ``AgentsInvestigator`` (docs/graph/gap/investigation.md "Tool agent
adapter"; FR-GINV-09, GC1).

The host adapter of the gap library's ``Investigator`` port on the OpenAI Agents SDK runtime, chosen by
``GapSettings.investigator = "agents"`` (the structured loop stays the default). It runs in the process of the gap run
(``Runner.run`` outside a Temporal workflow falls back to the SDK's own runner) because its reads are the run's
``SiteReads``, which remember every id they showed for the check step.

- The read menu becomes one function tool per read (``node``, ``evidence``, ``neighbours``, ``trace``, ``paths``,
  ``similar``, ``find_nodes``, ``find_fragments``); the tools read through the ``SiteReads`` the run hands over in the
  run context, so every id comes from the run: no tool parameter carries a project id, a query or a key. A tool that
  raises (anything other than the empty results the reads return) raises out of the run
  (``failure_error_function=None``): no error text reaches the model, and the site is deferred like a failed call.
- The same budget as the loop: a turn is a round, a tool call is a read. A turn runs at most ``reads_per_round``
  reads (more answer ``read_limit``, as do reads after turn ``rounds``); after turn ``rounds`` the tool choice becomes
  the ``report`` tool (until then any tool is ``required``), so a site costs at most ``rounds + 1`` calls. When
  the conversation would exceed ``context_tokens``, the oldest read results are replaced by one-line summaries.
- The run ends at the ``report`` tool, whose argument is the loop's strict ``WireReport``; the adapter returns the same
  ``GapReport`` the loop returns, and the check step checks it the same way.
- Every model call goes to the endpoint, key and ``extraction`` model of the run's gateway configuration (GC1), with
  that call class's timeout and attempts, through the Responses API (``/v1/responses``) with ``store`` off: the
  gateway's reasoning models refuse function tools on Chat Completions unless reasoning is switched off, which the
  structured loop does not do. Tracing is off: traces would carry document text (NFR-GSEC-02).

The pack and every read result hold document text, node names and quotes only inside marked untrusted blocks, as in
the loop.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast, override

import openai
from agents import (
    Agent,
    AgentsException,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    StopAtTools,
    function_tool,
)
from agents.items import ModelResponse, TResponseInputItem
from agents.models.interface import Model
from agents.run_config import CallModelData, ModelInputData
from agents.tool import FunctionTool
from agents.tool_context import ToolContext
from openai import AsyncOpenAI
from pydantic import ValidationError

from graph.core.config import LLMConfig
from graph.core.llm.gateway import UNTRUSTED_NOTICE, estimate_tokens, untrusted
from graph.gap.models import GapReport, InvestigationResult, ReadRecord, ReadResult, SiteBudget, SitePack
from graph.gap.ports import SiteReads
from graph.gap.prompts import INVESTIGATE_PROMPT, prompt_text
from graph.gap.steps.investigate import WireRead, WireReport, pack_message, report_of, run_read
from logger.logger import INFO, WARNING, get_logger, log_event

logger = get_logger(__name__)

CALL_CLASS: Final = "extraction"
AGENT_NAME: Final = "gap-investigator"
REVISION: Final = 1
"""The revision of the adapter's instructions and tool shapes; raise it whenever either changes."""
PROMPT_VERSION: Final = f"{INVESTIGATE_PROMPT}.agents.{REVISION}"
"""The investigate prompt version of runs with this adapter (part of every site's input hash)."""
REPORT_TOOL: Final = "report"
READ_LIMIT: Final = "read_limit"
REPORT_MISSING: Final = "llm_report_missing"
MALFORMED: Final = "llm_malformed"
UNREACHABLE: Final = "llm_unreachable"
EXHAUSTED: Final = "llm_exhausted"
FATAL: Final = "llm_fatal"
FAILED: Final = "investigator_failed"
_FATAL_STATUSES: Final = frozenset({400, 401, 403, 404, 422})

_TURN_MARK: Final = "Each turn, answer with one JSON object:"
_REPORT_MARK: Final = "The report:"
TOOLS_PARAGRAPH: Final = (
    "Work with the tools. To read more of the graph first, call the read tools: `node`, `evidence`, `neighbours`, "
    "`trace` (`direction` `based_on` or `affects`, `depth` 1 to 3), `paths` (between `spec_id` and `target`), "
    "`similar` (a spec id or a text), `find_nodes` (words, optional labels) and `find_fragments` (words). Read only "
    "what you need: the number of turns and of reads per turn is limited, and every read result is data in a text "
    "block. When you know enough, call `report` exactly once with the report. Never answer with plain text.\n"
)
_FINAL_NOTE: Final = "The read budget is used up. Call report now with what you have."
_READ_HEADER: Final = "Read result (JSON); its items are in the data block named in it."


def agent_instructions() -> str:
    """The investigate prompt with its turn format replaced by the tools, plus the untrusted-data rule."""

    text = prompt_text("investigate")
    head, turn_mark, rest = text.partition(_TURN_MARK)
    _, report_mark, report = rest.partition(_REPORT_MARK)
    if not turn_mark or not report_mark:
        raise ValueError("the investigate prompt lost its turn or report section")
    return f"{head}{TOOLS_PARAGRAPH}\n{_REPORT_MARK}{report.rstrip()}\n\n{UNTRUSTED_NOTICE}\n"


@dataclass
class SiteRun:
    """The run context of one investigation: the only source of what the tools read (FR-GINV-03)."""

    reads: SiteReads
    budget: SiteBudget
    site_id: str = ""
    turns: int = 0  # model calls started
    tokens: int = 0  # input and output tokens of the finished calls
    reads_by_turn: dict[int, int] = field(default_factory=dict)
    records: list[ReadRecord] = field(default_factory=list)
    summaries: dict[str, str] = field(default_factory=dict)  # tool call id -> one-line summary
    report: WireReport | None = None

    def reads_open(self) -> bool:
        """Whether the turn whose tools run now may still read (turns 1 to ``rounds``)."""

        return self.turns <= self.budget.rounds


def _arguments(request: WireRead) -> dict[str, Any]:
    return request.model_dump(mode="json", exclude_none=True, exclude={"read"})


def read_output(label: str, request: WireRead, result: ReadResult) -> str:
    """One read's tool output: a JSON head (read, item count, reason) and its items as an untrusted block."""

    head: dict[str, Any] = {"read": request.read, "items": len(result.items)}
    if result.reason:
        head["reason"] = result.reason
    if result.truncated:
        head["truncated"] = True
    parts = []
    if result.items:
        body = {"arguments": _arguments(request), "items": list(result.items)}
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"), default=str)
        head["block"] = label
        parts.append(untrusted(label, text))
    return "\n\n".join([_READ_HEADER, json.dumps(head, ensure_ascii=False, sort_keys=True), *parts])


def read_summary(request: WireRead, result: ReadResult) -> str:
    """The one-line summary that replaces a read result when the context runs full (no document text)."""

    names = ", ".join(sorted(_arguments(request)))
    return f"Read result summarised to save space: {request.read}({names}): {len(result.items)} items"


async def _run(ctx: RunContextWrapper[SiteRun], request: WireRead) -> str:
    """Run one read of the site's menu within the turn's read budget and record it."""

    run = ctx.context
    turn = run.turns
    used = run.reads_by_turn.get(turn, 0)
    if used >= run.budget.reads_per_round or not run.reads_open():
        result = ReadResult(read=request.read, reason=READ_LIMIT)
    else:
        run.reads_by_turn[turn] = used + 1
        result = await run_read(run.reads, request)
        run.records.append(
            ReadRecord(read=request.read, arguments=_arguments(request), empty=not result.items, reason=result.reason)
        )
    call_id = ctx.tool_call_id if isinstance(ctx, ToolContext) else ""
    if call_id:
        run.summaries[call_id] = read_summary(request, result)
    output = read_output(f"R{turn}.{used + 1} {request.read}", request, result)
    return f"{output}\n\n{_FINAL_NOTE}" if turn >= run.budget.rounds else output


_TOOL = function_tool(failure_error_function=None)


@_TOOL
async def node(ctx: RunContextWrapper[SiteRun], spec_id: str) -> str:
    """Read one spec node: label, name, attributes and owner.

    Args:
        spec_id: The spec id of the node.
    """

    return await _run(ctx, WireRead(read="node", spec_id=spec_id))


@_TOOL
async def evidence(ctx: RunContextWrapper[SiteRun], spec_id: str) -> str:
    """Read the source lines of a spec node, with two lines of context around them.

    Args:
        spec_id: The spec id of the node.
    """

    return await _run(ctx, WireRead(read="evidence", spec_id=spec_id))


@_TOOL
async def neighbours(ctx: RunContextWrapper[SiteRun], spec_id: str, relations: list[str] | None = None) -> str:
    """Read the nodes adjacent to a spec node, with their relations.

    Args:
        spec_id: The spec id of the node.
        relations: Relation names to keep, or null for all.
    """

    return await _run(ctx, WireRead(read="neighbours", spec_id=spec_id, relations=relations))


@_TOOL
async def trace(
    ctx: RunContextWrapper[SiteRun], spec_id: str, direction: Literal["based_on", "affects"], depth: int
) -> str:
    """Walk the impact trace from a spec node.

    Args:
        spec_id: The spec id to start from.
        direction: based_on (what the node rests on) or affects (what rests on the node).
        depth: The number of hops, 1 to 3.
    """

    return await _run(ctx, WireRead(read="trace", spec_id=spec_id, direction=direction, depth=depth))


@_TOOL
async def paths(ctx: RunContextWrapper[SiteRun], spec_id: str, target: str) -> str:
    """Whether and how two spec nodes are connected (at most 4 hops).

    Args:
        spec_id: The spec id of one node.
        target: The spec id of the other node.
    """

    return await _run(ctx, WireRead(read="paths", spec_id=spec_id, target=target))


@_TOOL
async def similar(ctx: RunContextWrapper[SiteRun], subject: str, limit: int) -> str:
    """Near statements that may conflict: of a spec id, or of a text searched as words.

    Args:
        subject: A spec id or a short text.
        limit: The number of results, 1 to 10.
    """

    return await _run(ctx, WireRead(read="similar", words=subject, limit=limit))


@_TOOL
async def find_nodes(ctx: RunContextWrapper[SiteRun], words: str, labels: list[str] | None = None) -> str:
    """Nodes whose name or text match the words.

    Args:
        words: The words to search for.
        labels: Node labels to keep, or null for all.
    """

    return await _run(ctx, WireRead(read="find_nodes", words=words, labels=labels))


@_TOOL
async def find_fragments(ctx: RunContextWrapper[SiteRun], words: str) -> str:
    """Document fragments whose text matches the words, with their line ranges.

    Args:
        words: The words to search for.
    """

    return await _run(ctx, WireRead(read="find_fragments", words=words))


@function_tool(failure_error_function=None, name_override=REPORT_TOOL)
async def report(ctx: RunContextWrapper[SiteRun], report: WireReport) -> str:
    """Submit the report; this ends the investigation. Call it exactly once.

    Args:
        report: The report.
    """

    ctx.context.report = report
    return "Report received."


READ_TOOLS: Final[tuple[FunctionTool, ...]] = (
    node,
    evidence,
    neighbours,
    trace,
    paths,
    similar,
    find_nodes,
    find_fragments,
)
TOOLS: Final[tuple[FunctionTool, ...]] = (*READ_TOOLS, report)


class _Hooks(RunHooks[SiteRun]):
    """Counts the calls (turns) and the tokens of one investigation, also when the run fails, and forces the
    ``report`` tool for the call after turn ``rounds``."""

    @override
    async def on_llm_start(
        self,
        context: RunContextWrapper[SiteRun],
        agent: Agent[SiteRun],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        del agent, system_prompt, input_items
        context.context.turns += 1

    @override
    async def on_llm_end(
        self, context: RunContextWrapper[SiteRun], agent: Agent[SiteRun], response: ModelResponse
    ) -> None:
        usage = response.usage
        run = context.context
        run.tokens += int(usage.input_tokens or 0) + int(usage.output_tokens or 0)
        if run.turns >= run.budget.rounds:
            agent.model_settings = dataclasses.replace(agent.model_settings, tool_choice=REPORT_TOOL)


def _item_tokens(items: Sequence[Any]) -> int:
    return sum(estimate_tokens(json.dumps(item, ensure_ascii=False, default=str)) for item in items)


def compact(data: CallModelData[SiteRun]) -> ModelInputData:
    """Replace the oldest read results by their summaries while the input exceeds ``context_tokens``."""

    model_data = data.model_data
    run = data.context
    if run is None:
        return model_data
    limit = run.budget.context_tokens
    items: list[dict[str, Any]] = [dict(item) for item in model_data.input]
    instructions = estimate_tokens(model_data.instructions or "")
    for index, item in enumerate(items):
        if instructions + _item_tokens(items) <= limit:
            break
        if item.get("type") != "function_call_output":
            continue
        summary = run.summaries.get(str(item.get("call_id")))
        if summary is not None and item.get("output") != summary:
            items[index] = {**item, "output": summary}
    return ModelInputData(input=cast(list[TResponseInputItem], items), instructions=model_data.instructions)


def build_agent(model: Model | str) -> Agent[SiteRun]:
    """The investigator agent: the read tools and ``report``; the run stops at ``report``."""

    return Agent[SiteRun](
        name=AGENT_NAME,
        model=model,
        instructions=agent_instructions(),
        tools=list(TOOLS),
        tool_use_behavior=StopAtTools(stop_at_tool_names=[REPORT_TOOL]),
        reset_tool_choice=False,
        model_settings=ModelSettings(tool_choice="required", store=False),
    )


def error_code(error: BaseException) -> str:
    """The failure code of a run that raised (as the loop's codes for a failed model call)."""

    if isinstance(error, MaxTurnsExceeded):
        return REPORT_MISSING
    if isinstance(error, ModelBehaviorError | ValidationError):
        return MALFORMED
    if isinstance(error, openai.APIConnectionError):
        return UNREACHABLE
    if isinstance(error, openai.APIStatusError):
        return FATAL if error.status_code in _FATAL_STATUSES else EXHAUSTED
    if isinstance(error, openai.OpenAIError):
        return EXHAUSTED
    return FAILED


def gateway_client(config: LLMConfig) -> AsyncOpenAI:
    """A client of the run's gateway endpoint and key, with the ``extraction`` class's timeout and attempts (GC1)."""

    budget = config.budget(CALL_CLASS)
    return AsyncOpenAI(
        base_url=config.endpoint,
        api_key=config.api_key.get_secret_value(),
        max_retries=budget.attempts - 1,
        timeout=budget.timeout_s,
    )


class AgentsInvestigator:
    """``Investigator`` adapter ``agents`` (module docstring)."""

    prompt_version: Final = PROMPT_VERSION

    def __init__(self, model: Model, *, close: Callable[[], Awaitable[None]] | None = None) -> None:
        self._model = model
        self._close = close

    @classmethod
    def from_gateway(cls, gateway: Any, *, model: str | None = None) -> AgentsInvestigator:
        """The adapter on the configuration of a run's core gateway (``gateway.config``)."""

        config: LLMConfig = gateway.config
        client = gateway_client(config)
        chat = OpenAIResponsesModel(model=model or config.model_for(CALL_CLASS), openai_client=client)
        return cls(chat, close=client.close)

    async def aclose(self) -> None:
        if self._close is not None:
            await self._close()

    async def investigate(self, pack: SitePack, reads: SiteReads, budget: SiteBudget) -> InvestigationResult:
        run = SiteRun(reads=reads, budget=budget, site_id=pack.site_id)
        started = time.monotonic()
        error: str | None = None
        try:
            await Runner.run(
                build_agent(self._model),
                pack_message(pack, budget),
                context=run,
                max_turns=budget.rounds + 1,
                hooks=_Hooks(),
                run_config=RunConfig(tracing_disabled=True, call_model_input_filter=compact),
            )
        except (AgentsException, openai.OpenAIError, ValidationError) as failure:
            error = error_code(failure)
        found: GapReport | None = None
        if error is None:
            if run.report is None:
                error = REPORT_MISSING
            else:
                found = report_of(run.report)
        log_event(
            logger,
            INFO if error is None else WARNING,
            "agent_runtime.gap.investigated",
            site_id=pack.site_id,
            calls=run.turns,
            reads=len(run.records),
            tokens=run.tokens,
            duration_ms=round((time.monotonic() - started) * 1000),
            code=error,
        )
        return InvestigationResult(
            report=found,
            rounds_used=min(run.turns, budget.rounds),
            reads=tuple(run.records),
            model_calls=run.turns,
            tokens=run.tokens,
            error=error,
        )
