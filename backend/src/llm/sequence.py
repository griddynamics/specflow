from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from llm.agent import AgentInput
from llm.result import AgentResult


class StepRunner[InputT, ResultT](Protocol):
    """Minimal runner interface required by a prompt sequence."""

    async def run(self, value: InputT) -> ResultT: ...


class AgentRunner(Protocol):
    """Minimal agent interface required by a prompt sequence."""

    async def run(
        self,
        prompt_or_messages: AgentInput,
        *,
        resume_session_id: str | None = None,
    ) -> AgentResult: ...


@dataclass(frozen=True, slots=True)
class PromptStep[ContextT]:
    """A named caller-owned prompt template."""

    name: str
    render: Callable[
        [PromptRenderContext[ContextT]],
        Any | Awaitable[Any],
    ]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("step name must not be empty")
        if not callable(self.render):
            raise TypeError("step render must be callable")


@dataclass(frozen=True, slots=True)
class PromptStepResult[ContextT]:
    """The rendered input and runner outcome for one executed step."""

    index: int
    step: PromptStep[ContextT]
    prompt: Any
    result: Any


@dataclass(frozen=True, slots=True)
class PromptRenderContext[ContextT]:
    """Caller context and prior outcomes available while rendering a step."""

    context: ContextT
    index: int
    step: PromptStep[ContextT]
    prior_results: tuple[PromptStepResult[ContextT], ...]


@dataclass(frozen=True, slots=True)
class PromptSequenceContext[ContextT]:
    """Inputs available to an external start-index resolver."""

    context: ContextT
    steps: tuple[PromptStep[ContextT], ...]


async def _resolve[ValueT](value: ValueT | Awaitable[ValueT]) -> ValueT:
    if inspect.isawaitable(value):
        return await cast(Awaitable[ValueT], value)
    return value


class PromptSequence[ContextT]:
    """Run caller-owned templates with a fresh runner for every step."""

    def __init__(
        self,
        steps: Sequence[PromptStep[ContextT]],
        *,
        agent_factory: Callable[
            [PromptRenderContext[ContextT]],
            StepRunner[Any, Any] | Awaitable[StepRunner[Any, Any]],
        ]
        | None = None,
        runner_factory: Callable[
            [PromptRenderContext[ContextT]],
            StepRunner[Any, Any] | Awaitable[StepRunner[Any, Any]],
        ]
        | None = None,
        is_success: Callable[
            [Any],
            bool | Awaitable[bool],
        ]
        | None = None,
    ) -> None:
        self.steps = tuple(steps)
        if agent_factory is not None and runner_factory is not None:
            raise ValueError("agent_factory and runner_factory are mutually exclusive")
        selected_factory = runner_factory if runner_factory is not None else agent_factory
        if not callable(selected_factory):
            raise TypeError("a runner factory must be callable")
        if is_success is not None and not callable(is_success):
            raise TypeError("is_success must be callable")
        self.runner_factory = selected_factory
        self.agent_factory = selected_factory
        self.is_success = is_success

    async def run(
        self,
        context: ContextT,
        *,
        start_index: int | None = None,
        end_index: int | None = None,
        resolve_start_index: Callable[
            [PromptSequenceContext[ContextT]],
            int | Awaitable[int],
        ]
        | None = None,
        before_step: Callable[
            [PromptRenderContext[ContextT]],
            Awaitable[None] | None,
        ]
        | None = None,
        after_success: Callable[
            [PromptStepResult[ContextT]],
            Awaitable[None] | None,
        ]
        | None = None,
        is_cancelled: Callable[
            [PromptRenderContext[ContextT]],
            bool | Awaitable[bool],
        ]
        | None = None,
        continue_on_failure: bool = False,
    ) -> tuple[PromptStepResult[ContextT], ...]:
        """Execute a zero-based, end-exclusive range of prompt steps."""

        if start_index is not None and resolve_start_index is not None:
            raise ValueError("start_index and resolve_start_index are mutually exclusive")

        if resolve_start_index is not None:
            start = await _resolve(resolve_start_index(PromptSequenceContext(context=context, steps=self.steps)))
        else:
            start = 0 if start_index is None else start_index
        end = len(self.steps) if end_index is None else end_index
        self._validate_range(start, end)

        results: list[PromptStepResult[ContextT]] = []
        for index in range(start, end):
            step_context = PromptRenderContext(
                context=context,
                index=index,
                step=self.steps[index],
                prior_results=tuple(results),
            )
            if is_cancelled is not None and await _resolve(is_cancelled(step_context)):
                raise asyncio.CancelledError
            if before_step is not None:
                await _resolve(before_step(step_context))

            prompt = await _resolve(step_context.step.render(step_context))
            runner = await _resolve(self.runner_factory(step_context))
            outcome = PromptStepResult(
                index=index,
                step=step_context.step,
                prompt=prompt,
                result=await runner.run(prompt),
            )
            results.append(outcome)

            if not await self._result_is_success(outcome.result):
                if not continue_on_failure:
                    break
                continue
            if after_success is not None:
                await _resolve(after_success(outcome))

        return tuple(results)

    async def _result_is_success(self, result: Any) -> bool:
        if self.is_success is not None:
            return bool(await _resolve(self.is_success(result)))
        is_error = getattr(result, "is_error", None)
        if not isinstance(is_error, bool):
            raise TypeError("non-agent sequence results require an is_success callback")
        return not is_error

    def _validate_range(self, start: int, end: int) -> None:
        for name, value in (("start_index", start), ("end_index", end)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 0 <= value <= len(self.steps):
                raise ValueError(f"{name} must be between 0 and {len(self.steps)}")
        if start > end:
            raise ValueError("start_index must not exceed end_index")
