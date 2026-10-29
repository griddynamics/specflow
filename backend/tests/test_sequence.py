from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from llm import (
    AgentError,
    AgentErrorKind,
    AgentProgress,
    AgentResult,
    PromptRenderContext,
    PromptSequence,
    PromptStep,
)


def agent_result(output: str, *, is_error: bool = False) -> AgentResult:
    return AgentResult(
        output=output,
        session_id=f"session-{output}",
        is_error=is_error,
        progress=AgentProgress(num_messages=1),
        error=(AgentError(AgentErrorKind.RESULT, output) if is_error else None),
    )


class FakeAgent:
    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.calls: list[tuple[Any, str | None]] = []

    async def run(
        self,
        prompt_or_messages: Any,
        *,
        resume_session_id: str | None = None,
    ) -> AgentResult:
        self.calls.append((prompt_or_messages, resume_session_id))
        return self.result


class FailingAgent:
    async def run(
        self,
        prompt_or_messages: Any,
        *,
        resume_session_id: str | None = None,
    ) -> AgentResult:
        del prompt_or_messages, resume_session_id
        raise RuntimeError("agent transport failed")


@pytest.mark.asyncio
async def test_sequence_runs_in_order_with_context_and_prior_results() -> None:
    opaque_context = object()
    rendered: list[PromptRenderContext[object]] = []
    agents: list[FakeAgent] = []
    checkpoints: list[str] = []

    def render(step_context: PromptRenderContext[object]) -> str:
        rendered.append(step_context)
        prior = ",".join(result.result.output or "" for result in step_context.prior_results)
        return f"{step_context.step.name}:{prior}"

    async def agent_factory(
        step_context: PromptRenderContext[object],
    ) -> FakeAgent:
        agent = FakeAgent(agent_result(step_context.step.name))
        agents.append(agent)
        return agent

    async def checkpoint(result: Any) -> None:
        checkpoints.append(result.step.name)

    sequence = PromptSequence(
        [
            PromptStep("first", render),
            PromptStep("second", render),
            PromptStep("third", render),
        ],
        agent_factory=agent_factory,
    )

    results = await sequence.run(
        opaque_context,
        after_success=checkpoint,
    )

    assert [result.step.name for result in results] == [
        "first",
        "second",
        "third",
    ]
    assert [result.prompt for result in results] == [
        "first:",
        "second:first",
        "third:first,second",
    ]
    assert all(item.context is opaque_context for item in rendered)
    assert [len(item.prior_results) for item in rendered] == [0, 1, 2]
    assert len({id(agent) for agent in agents}) == 3
    assert [agent.calls for agent in agents] == [
        [("first:", None)],
        [("second:first", None)],
        [("third:first,second", None)],
    ]
    assert checkpoints == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_sequence_supports_async_prompt_templates() -> None:
    async def render(step_context: PromptRenderContext[str]) -> str:
        await asyncio.sleep(0)
        return f"{step_context.context}:{step_context.step.name}"

    agent = FakeAgent(agent_result("done"))
    sequence = PromptSequence(
        [PromptStep("async", render)],
        agent_factory=lambda _: agent,
    )

    results = await sequence.run("context")

    assert results[0].prompt == "context:async"


@pytest.mark.asyncio
async def test_sequence_runs_an_explicit_end_exclusive_range() -> None:
    agents: list[FakeAgent] = []

    def factory(step_context: PromptRenderContext[None]) -> FakeAgent:
        agent = FakeAgent(agent_result(step_context.step.name))
        agents.append(agent)
        return agent

    sequence = PromptSequence(
        [
            PromptStep("zero", lambda _: "zero"),
            PromptStep("one", lambda _: "one"),
            PromptStep("two", lambda _: "two"),
        ],
        agent_factory=factory,
    )

    results = await sequence.run(None, start_index=1, end_index=3)

    assert [result.index for result in results] == [1, 2]
    assert [agent.calls[0][0] for agent in agents] == ["one", "two"]


@pytest.mark.asyncio
async def test_external_start_index_resolver_supports_resume() -> None:
    seen_steps: tuple[str, ...] = ()

    async def resolve_start(sequence_context: Any) -> int:
        nonlocal seen_steps
        seen_steps = tuple(step.name for step in sequence_context.steps)
        return 2

    sequence = PromptSequence(
        [
            PromptStep("zero", lambda _: "zero"),
            PromptStep("one", lambda _: "one"),
            PromptStep("two", lambda _: "two"),
        ],
        agent_factory=lambda _: FakeAgent(agent_result("done")),
    )

    results = await sequence.run(
        {"checkpoint": 1},
        resolve_start_index=resolve_start,
    )

    assert seen_steps == ("zero", "one", "two")
    assert [result.index for result in results] == [2]


@pytest.mark.asyncio
async def test_cancellation_is_checked_between_steps() -> None:
    agents: list[FakeAgent] = []

    def factory(step_context: PromptRenderContext[None]) -> FakeAgent:
        agent = FakeAgent(agent_result(step_context.step.name))
        agents.append(agent)
        return agent

    sequence = PromptSequence(
        [
            PromptStep("first", lambda _: "first"),
            PromptStep("second", lambda _: "second"),
        ],
        agent_factory=factory,
    )

    with pytest.raises(asyncio.CancelledError):
        await sequence.run(
            None,
            is_cancelled=lambda step_context: step_context.index == 1,
        )

    assert len(agents) == 1


@pytest.mark.asyncio
async def test_failure_stops_before_checkpointing_failed_step() -> None:
    checkpoints: list[str] = []

    def factory(step_context: PromptRenderContext[None]) -> FakeAgent:
        return FakeAgent(
            agent_result(
                step_context.step.name,
                is_error=step_context.index == 1,
            )
        )

    sequence = PromptSequence(
        [
            PromptStep("success", lambda _: "success"),
            PromptStep("failure", lambda _: "failure"),
            PromptStep("skipped", lambda _: "skipped"),
        ],
        agent_factory=factory,
    )

    results = await sequence.run(
        None,
        after_success=lambda result: checkpoints.append(result.step.name),
    )

    assert [result.step.name for result in results] == ["success", "failure"]
    assert checkpoints == ["success"]


@pytest.mark.asyncio
async def test_agent_exception_propagates_without_checkpointing() -> None:
    checkpoints: list[str] = []
    sequence = PromptSequence(
        [PromptStep("failure", lambda _: "failure")],
        agent_factory=lambda _: FailingAgent(),
    )

    with pytest.raises(RuntimeError, match="agent transport failed"):
        await sequence.run(
            None,
            after_success=lambda result: checkpoints.append(result.step.name),
        )

    assert checkpoints == []


@pytest.mark.asyncio
async def test_failure_continuation_requires_explicit_policy() -> None:
    sequence = PromptSequence(
        [
            PromptStep("failure", lambda _: "failure"),
            PromptStep(
                "continued",
                lambda step_context: f"continued:{step_context.prior_results[-1].result.is_error}",
            ),
        ],
        agent_factory=lambda step_context: FakeAgent(
            agent_result(
                step_context.step.name,
                is_error=step_context.index == 0,
            )
        ),
    )

    results = await sequence.run(None, continue_on_failure=True)

    assert [result.step.name for result in results] == [
        "failure",
        "continued",
    ]
    assert results[1].prompt == "continued:True"


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_name", ["before", "after", "cancel"])
async def test_lifecycle_callback_errors_propagate(
    callback_name: str,
) -> None:
    def fail(_: Any) -> Any:
        raise RuntimeError(f"{callback_name} failed")

    callbacks: dict[str, Callable[..., Any]] = {
        "before_step": fail if callback_name == "before" else lambda _: None,
        "after_success": fail if callback_name == "after" else lambda _: None,
        "is_cancelled": fail if callback_name == "cancel" else lambda _: False,
    }
    sequence = PromptSequence(
        [PromptStep("step", lambda _: "prompt")],
        agent_factory=lambda _: FakeAgent(agent_result("success")),
    )

    with pytest.raises(RuntimeError, match=f"{callback_name} failed"):
        await sequence.run(None, **callbacks)


@pytest.mark.asyncio
async def test_start_index_callback_errors_propagate() -> None:
    def fail(_: Any) -> int:
        raise RuntimeError("checkpoint unavailable")

    sequence = PromptSequence(
        [PromptStep("step", lambda _: "prompt")],
        agent_factory=lambda _: FakeAgent(agent_result("success")),
    )

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await sequence.run(None, resolve_start_index=fail)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"start_index": -1}, ValueError, "between"),
        ({"end_index": 2}, ValueError, "between"),
        (
            {"start_index": 1, "end_index": 0},
            ValueError,
            "must not exceed",
        ),
        ({"start_index": True}, TypeError, "integer"),
    ],
)
@pytest.mark.asyncio
async def test_sequence_rejects_invalid_ranges(
    kwargs: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    sequence = PromptSequence(
        [PromptStep("step", lambda _: "prompt")],
        agent_factory=lambda _: FakeAgent(agent_result("success")),
    )

    with pytest.raises(error, match=message):
        await sequence.run(None, **kwargs)


@pytest.mark.asyncio
async def test_sequence_rejects_conflicting_resume_inputs() -> None:
    sequence = PromptSequence(
        [PromptStep("step", lambda _: "prompt")],
        agent_factory=lambda _: FakeAgent(agent_result("success")),
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        await sequence.run(
            None,
            start_index=0,
            resolve_start_index=lambda _: 0,
        )


def test_sequence_requires_exactly_one_runner_factory() -> None:
    steps = [PromptStep("step", lambda _: "prompt")]

    def factory(_: object) -> FakeAgent:
        return FakeAgent(agent_result("success"))

    with pytest.raises(TypeError, match="runner factory"):
        PromptSequence(steps)
    with pytest.raises(ValueError, match="mutually exclusive"):
        PromptSequence(
            steps,
            agent_factory=factory,
            runner_factory=factory,
        )


@pytest.mark.asyncio
async def test_non_agent_results_require_explicit_success_policy() -> None:
    class GenericRunner:
        async def run(self, value: Any) -> dict[str, Any]:
            return {"value": value, "complete": True}

    sequence = PromptSequence(
        [PromptStep("step", lambda _: "prompt")],
        runner_factory=lambda _: GenericRunner(),
    )

    with pytest.raises(TypeError, match="is_success"):
        await sequence.run(None)

    successful_sequence = PromptSequence(
        [PromptStep("step", lambda _: "prompt")],
        runner_factory=lambda _: GenericRunner(),
        is_success=lambda result: result["complete"],
    )
    results = await successful_sequence.run(None)

    assert results[0].result == {"value": "prompt", "complete": True}


def test_prompt_step_validates_name_and_renderer() -> None:
    with pytest.raises(ValueError, match="name"):
        PromptStep(" ", lambda _: "prompt")
    with pytest.raises(TypeError, match="render"):
        PromptStep("step", None)
