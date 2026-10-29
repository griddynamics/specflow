from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from llm.agent import AgentInput
from llm.captured import CapturedException
from llm.dag import END, AgentDAG, AgentNode, DAGResult, Decision, Destination
from llm.progress_detection import ProgressDetector
from llm.result import AgentResult
from llm.retry_policy import (
    CrashBackoffPolicy,
    ErrorClassifier,
    Failure,
    FailureDisposition,
)


class ValidatedAgentRunner(Protocol):
    """Minimal agent interface required by a validated agent DAG."""

    async def run(self, prompt_or_messages: AgentInput) -> AgentResult: ...


class ValidatedRunOutcome(StrEnum):
    """Outcomes produced by validated-run graph nodes."""

    SUCCEEDED = "succeeded"
    COMPLETED = "completed"
    RESUME = "resume"
    RETRY = "retry"
    RESUME_EXHAUSTED = "resume_exhausted"
    CRASH_EXHAUSTED = "crash_exhausted"
    TERMINAL_FAILURE = "terminal_failure"


class ValidatedRunRole(StrEnum):
    """Agent roles in a validated-run graph."""

    INITIAL = "initial"
    VALIDATOR = "validator"
    RESUME = "resume"


@dataclass(frozen=True, slots=True)
class ValidatedRunContext:
    """Caller input for a validated agent DAG."""

    prompt: AgentInput


@dataclass(frozen=True, slots=True)
class ValidatedRunRecord:
    """One initial, validator, or resume node result."""

    role: ValidatedRunRole
    outcome: ValidatedRunOutcome
    result: AgentResult | None
    failure: Failure | None
    resumes_used: int
    crash_streak: int
    progress_before: object | None = None


@dataclass(frozen=True, slots=True)
class RetryEvent:
    """A failed attempt that will be retried after a delay."""

    role: ValidatedRunRole
    failure: Failure
    delay: float
    saved_progress: bool
    resumes_used: int
    crash_streak: int


@dataclass(frozen=True, slots=True)
class GiveUpEvent:
    """A terminal validated-run outcome."""

    role: ValidatedRunRole
    outcome: ValidatedRunOutcome
    failure: Failure | None
    resumes_used: int
    crash_streak: int


ValidatedAgentFactory = Callable[[], ValidatedAgentRunner]
PromptBuilder = Callable[
    [
        ValidatedRunContext,
        DAGResult[ValidatedRunContext, ValidatedRunRecord],
    ],
    AgentInput | Awaitable[AgentInput],
]
CompletionPredicate = Callable[[AgentResult], bool | Awaitable[bool]]
RetryCallback = Callable[[RetryEvent], Awaitable[None] | None]
GiveUpCallback = Callable[[GiveUpEvent], Awaitable[None] | None]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ValidatedRunPrompts:
    """Builders of the validator prompt and of the resume prompt."""

    validator: PromptBuilder
    resume: PromptBuilder


@dataclass(frozen=True, slots=True)
class ValidatedRunPolicy:
    """Budgets, crash backoff and retry/give-up hooks of a validated agent DAG."""

    max_resumes: int = 2
    max_steps: int = 100
    crash_backoff: CrashBackoffPolicy | None = None
    sleep: Sleep = asyncio.sleep
    on_retry: RetryCallback | None = None
    on_give_up: GiveUpCallback | None = None


class UnavailableValidatorError(RuntimeError):
    """A validator completed without a usable result and session."""

    def __init__(self, result: AgentResult) -> None:
        super().__init__("Validator did not return a usable result and session")
        self.progress = result.progress


def build_validated_agent_dag(
    *,
    initial_agent: ValidatedAgentRunner,
    validator_agent_factory: ValidatedAgentFactory,
    resume_agent_factory: ValidatedAgentFactory,
    prompts: ValidatedRunPrompts,
    completion_predicate: CompletionPredicate,
    progress_detector: ProgressDetector,
    classify_error: ErrorClassifier,
    policy: ValidatedRunPolicy | None = None,
) -> AgentDAG[ValidatedRunContext, ValidatedRunRecord]:
    """Build a bounded initial-run, validation, and resume graph (``policy`` defaults to
    ``ValidatedRunPolicy()``)."""

    policy = policy or ValidatedRunPolicy()
    max_resumes, max_steps = policy.max_resumes, policy.max_steps
    resume_limit: object = max_resumes
    if isinstance(resume_limit, bool) or not isinstance(resume_limit, int):
        raise TypeError("max_resumes must be an integer")
    if max_resumes < 0:
        raise ValueError("max_resumes must not be negative")
    backoff = policy.crash_backoff or CrashBackoffPolicy()
    minimum_steps = 2 * max_resumes + 2
    step_limit: object = max_steps
    if isinstance(step_limit, int) and not isinstance(step_limit, bool) and step_limit < minimum_steps:
        raise ValueError(f"max_steps must be at least {minimum_steps} for max_resumes={max_resumes}")

    graph = _ValidatedRunGraph(
        initial_agent=initial_agent,
        validator_agent_factory=validator_agent_factory,
        resume_agent_factory=resume_agent_factory,
        validator_prompt_builder=prompts.validator,
        resume_prompt_builder=prompts.resume,
        completion_predicate=completion_predicate,
        progress_detector=progress_detector,
        classify_error=classify_error,
        max_resumes=max_resumes,
        backoff=backoff,
        sleep=policy.sleep,
        on_retry=policy.on_retry,
        on_give_up=policy.on_give_up,
    )
    return graph.build(max_steps=max_steps)


@dataclass(frozen=True, slots=True)
class _ValidatedRunGraph:
    """The validated-run collaborators and the node bodies that use them."""

    initial_agent: ValidatedAgentRunner
    validator_agent_factory: ValidatedAgentFactory
    resume_agent_factory: ValidatedAgentFactory
    validator_prompt_builder: PromptBuilder
    resume_prompt_builder: PromptBuilder
    completion_predicate: CompletionPredicate
    progress_detector: ProgressDetector
    classify_error: ErrorClassifier
    max_resumes: int
    backoff: CrashBackoffPolicy
    sleep: Sleep
    on_retry: RetryCallback | None
    on_give_up: GiveUpCallback | None

    def build(self, *, max_steps: int) -> AgentDAG[ValidatedRunContext, ValidatedRunRecord]:
        work_routes: dict[Hashable, Destination] = {
            ValidatedRunOutcome.SUCCEEDED: "validator",
            ValidatedRunOutcome.RETRY: "resume",
            ValidatedRunOutcome.CRASH_EXHAUSTED: END,
            ValidatedRunOutcome.TERMINAL_FAILURE: END,
        }
        validator_routes: dict[Hashable, Destination] = {
            ValidatedRunOutcome.COMPLETED: END,
            ValidatedRunOutcome.RESUME: "resume",
            ValidatedRunOutcome.RETRY: "resume",
            ValidatedRunOutcome.RESUME_EXHAUSTED: END,
            ValidatedRunOutcome.CRASH_EXHAUSTED: END,
            ValidatedRunOutcome.TERMINAL_FAILURE: END,
        }
        return AgentDAG(
            [
                AgentNode("initial", self.initial, "after_agent"),
                AgentNode("validator", self.validate, "after_validation"),
                AgentNode("resume", self.resume, "after_agent"),
            ],
            [
                Decision(
                    "after_agent",
                    lambda state: state.history[-1][1].outcome,
                    work_routes,
                ),
                Decision(
                    "after_validation",
                    lambda state: state.history[-1][1].outcome,
                    validator_routes,
                ),
            ],
            start_node="initial",
            max_steps=max_steps,
        )

    async def notify_give_up(self, record: ValidatedRunRecord) -> ValidatedRunRecord:
        if self.on_give_up is not None:
            await _maybe_await(
                self.on_give_up(
                    GiveUpEvent(
                        role=record.role,
                        outcome=record.outcome,
                        failure=record.failure,
                        resumes_used=record.resumes_used,
                        crash_streak=record.crash_streak,
                    )
                )
            )
        return record

    async def failed_record(
        self,
        *,
        role: ValidatedRunRole,
        failure: Failure,
        state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
        progress_before: object | None,
    ) -> ValidatedRunRecord:
        previous = _latest_record(state)
        resumes_used = previous.resumes_used if previous else 0
        crash_streak = previous.crash_streak if previous else 0
        disposition = await _maybe_await(self.classify_error(failure))
        if disposition is FailureDisposition.TERMINAL:
            return await self.notify_give_up(
                ValidatedRunRecord(
                    role=role,
                    outcome=ValidatedRunOutcome.TERMINAL_FAILURE,
                    result=None,
                    failure=failure,
                    resumes_used=resumes_used,
                    crash_streak=crash_streak,
                )
            )
        if disposition is not FailureDisposition.RETRY:
            raise ValueError(f"unsupported failure disposition: {disposition!r}")

        progress_after = await self.progress_detector.snapshot()
        saved_progress = await _maybe_await(
            self.progress_detector.saved_progress(
                progress_before,
                progress_after,
                failure,
            )
        )
        retry = self.backoff.next_retry(
            crash_streak=crash_streak,
            saved_progress=bool(saved_progress),
        )
        if retry is None:
            return await self.notify_give_up(
                ValidatedRunRecord(
                    role=role,
                    outcome=ValidatedRunOutcome.CRASH_EXHAUSTED,
                    result=None,
                    failure=failure,
                    resumes_used=resumes_used,
                    crash_streak=len(self.backoff.delays),
                )
            )

        event = RetryEvent(
            role=role,
            failure=failure,
            delay=retry.delay,
            saved_progress=retry.saved_progress,
            resumes_used=resumes_used,
            crash_streak=retry.crash_streak,
        )
        if self.on_retry is not None:
            await _maybe_await(self.on_retry(event))
        await self.sleep(retry.delay)
        return ValidatedRunRecord(
            role=role,
            outcome=ValidatedRunOutcome.RETRY,
            result=None,
            failure=failure,
            resumes_used=resumes_used,
            crash_streak=retry.crash_streak,
        )

    async def run_agent(
        self,
        role: ValidatedRunRole,
        agent_factory: ValidatedAgentFactory,
        prompt: AgentInput,
        state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
    ) -> ValidatedRunRecord:
        progress_before = await self.progress_detector.snapshot()
        result = await _attempt(agent_factory, prompt)
        if isinstance(result, Exception) or result.is_error:
            return await self.failed_record(
                role=role,
                failure=result,
                state=state,
                progress_before=progress_before,
            )
        previous = _latest_record(state)
        return ValidatedRunRecord(
            role=role,
            outcome=ValidatedRunOutcome.SUCCEEDED,
            result=result,
            failure=None,
            resumes_used=previous.resumes_used if previous else 0,
            crash_streak=previous.crash_streak if previous else 0,
            progress_before=progress_before,
        )

    async def initial(
        self,
        state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
    ) -> ValidatedRunRecord:
        return await self.run_agent(
            ValidatedRunRole.INITIAL,
            lambda: self.initial_agent,
            state.context.prompt,
            state,
        )

    async def resume(
        self,
        state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
    ) -> ValidatedRunRecord:
        prompt = await _render(self.resume_prompt_builder, state)
        return await self.run_agent(
            ValidatedRunRole.RESUME,
            self.resume_agent_factory,
            prompt,
            state,
        )

    async def validate(
        self,
        state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
    ) -> ValidatedRunRecord:
        work = _latest_successful_agent_run(state)
        prompt = await _render(self.validator_prompt_builder, state)
        result = await _attempt(self.validator_agent_factory, prompt)
        if isinstance(result, Exception) or result.is_error:
            return await self._validator_failed(result, state, work)
        unusable = _unusable_validator_failure(result)
        if unusable is not None:
            return await self._validator_failed(unusable, state, work)
        return await self._validated_record(result, work)

    async def _validator_failed(
        self,
        failure: Failure,
        state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
        work: ValidatedRunRecord,
    ) -> ValidatedRunRecord:
        return await self.failed_record(
            role=ValidatedRunRole.VALIDATOR,
            failure=failure,
            state=state,
            progress_before=work.progress_before,
        )

    async def _validated_record(self, result: AgentResult, work: ValidatedRunRecord) -> ValidatedRunRecord:
        complete = await _maybe_await(self.completion_predicate(result))
        if complete:
            return ValidatedRunRecord(
                role=ValidatedRunRole.VALIDATOR,
                outcome=ValidatedRunOutcome.COMPLETED,
                result=result,
                failure=None,
                resumes_used=work.resumes_used,
                crash_streak=0,
            )

        resumes_used = work.resumes_used + 1
        if resumes_used > self.max_resumes:
            return await self.notify_give_up(
                ValidatedRunRecord(
                    role=ValidatedRunRole.VALIDATOR,
                    outcome=ValidatedRunOutcome.RESUME_EXHAUSTED,
                    result=result,
                    failure=None,
                    resumes_used=self.max_resumes,
                    crash_streak=0,
                )
            )
        return ValidatedRunRecord(
            role=ValidatedRunRole.VALIDATOR,
            outcome=ValidatedRunOutcome.RESUME,
            result=result,
            failure=None,
            resumes_used=resumes_used,
            crash_streak=0,
        )


async def _attempt(agent_factory: ValidatedAgentFactory, prompt: AgentInput) -> AgentResult | Exception:
    """Run one agent; any failure other than cancellation is returned as a value."""

    capture = CapturedException()
    with capture:
        result = await agent_factory().run(prompt)
    if capture.error is not None:
        return capture.error
    return result


def _unusable_validator_failure(result: AgentResult) -> UnavailableValidatorError | None:
    if result.session_id is None or (result.output is None and result.structured_output is None):
        return UnavailableValidatorError(result)
    return None


def _latest_record(
    state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
) -> ValidatedRunRecord | None:
    return state.history[-1][1] if state.history else None


def _latest_successful_agent_run(
    state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
) -> ValidatedRunRecord:
    for name, record in reversed(state.history):
        if name in {"initial", "resume"} and record.outcome is ValidatedRunOutcome.SUCCEEDED:
            return record
    raise RuntimeError("validator requires a successful initial or resume result")


async def _render(
    builder: PromptBuilder,
    state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
) -> AgentInput:
    return cast(AgentInput, await _maybe_await(builder(state.context, state)))


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await cast(Awaitable[object], value)
    return value
