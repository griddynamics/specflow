from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from llm.agent import AgentInput
from llm.coding import CodingAgent
from llm.dag import AgentDAG, DAGResult
from llm.file_formats import CheckpointStore, PlanPhase
from llm.permissions import AgentPermissions
from llm.progress_detection import GitHeadProgressDetector
from llm.result import AgentProgress, AgentResult
from llm.retry_policy import Failure, FailureDisposition
from llm.sequence import (
    PromptRenderContext,
    PromptSequence,
    PromptStep,
    PromptStepResult,
)
from llm.settings import AgentSettings
from llm.stream import MessageObserver
from llm.validated_agent_dag import (
    ValidatedRunContext,
    ValidatedRunOutcome,
    ValidatedRunPolicy,
    ValidatedRunPrompts,
    ValidatedRunRecord,
    ValidatedRunRole,
    build_validated_agent_dag,
)

_VALIDATOR_SCHEMA = {
    "type": "object",
    "properties": {"complete": {"type": "boolean"}},
    "required": ["complete"],
    "additionalProperties": False,
}

_DRY_RUN_MAX_RESUMES: Final = 0
_DRY_RUN_MAX_STEPS: Final = 2

type PhaseDAG = AgentDAG[ValidatedRunContext, ValidatedRunRecord]
type PhaseDAGFactory = Callable[[PlanPhase], PhaseDAG]
type PhaseStartCallback = Callable[[int, PlanPhase], None]
type MessageObserverFactory = Callable[
    [PlanPhase, ValidatedRunRole],
    MessageObserver | None,
]


class StructuredAgentRunner(Protocol):
    async def run(
        self,
        prompt: AgentInput,
        *,
        output_format: Mapping[str, Any],
    ) -> AgentResult: ...


@dataclass(frozen=True, slots=True)
class CodeFactoryContext:
    workspace: Path


@dataclass(slots=True)
class StructuredValidator:
    agent: StructuredAgentRunner

    async def run(self, prompt: AgentInput) -> AgentResult:
        return await self.agent.run(
            prompt,
            output_format={"type": "json_schema", "schema": _VALIDATOR_SCHEMA},
        )


def render_phase(
    phase: PlanPhase,
    context: PromptRenderContext[CodeFactoryContext],
) -> ValidatedRunContext:
    return ValidatedRunContext(
        f"""Implement one phase from the caller's Markdown plan.

Workspace: {context.context.workspace}
{phase.name}

{phase.instructions}

Implement only this phase and verify its result before reporting completion.
"""
    )


def validated_dag_succeeded(
    result: DAGResult[ValidatedRunContext, ValidatedRunRecord],
) -> bool:
    return bool(result.ended and result.history and result.history[-1][1].outcome is ValidatedRunOutcome.COMPLETED)


def _phase_renderer(
    phase: PlanPhase,
) -> Callable[[PromptRenderContext[CodeFactoryContext]], ValidatedRunContext]:
    def render(context: PromptRenderContext[CodeFactoryContext]) -> ValidatedRunContext:
        return render_phase(phase, context)

    return render


def build_code_factory_sequence(
    phases: Sequence[PlanPhase],
    dag_factory: PhaseDAGFactory,
) -> PromptSequence[CodeFactoryContext]:
    frozen_phases = tuple(phases)
    steps = tuple(
        PromptStep(
            phase.name,
            _phase_renderer(phase),
        )
        for phase in frozen_phases
    )
    return PromptSequence(
        steps,
        runner_factory=lambda context: dag_factory(frozen_phases[context.index]),
        is_success=validated_dag_succeeded,
    )


def resume_prompt(
    context: ValidatedRunContext,
    state: DAGResult[ValidatedRunContext, ValidatedRunRecord],
) -> str:
    validators = state.outputs.get("validator", ())
    latest = validators[-1] if validators else None
    feedback = (
        latest.result.output
        if latest is not None and latest.result is not None and latest.result.output is not None
        else "Validation is incomplete or unavailable."
    )
    return f"Continue this phase:\n{context.prompt}\n\nValidation:\n{feedback}"


def result_is_complete(result: AgentResult) -> bool:
    structured = result.structured_output
    return bool(structured is not None and structured.get("complete") is True)


def classify_failure(failure: Failure) -> FailureDisposition:
    message = failure.error.message if isinstance(failure, AgentResult) and failure.error is not None else str(failure)
    if "does not support tools" in message.lower():
        return FailureDisposition.TERMINAL
    return FailureDisposition.RETRY


def build_coding_dag_factory(
    *,
    workspace: Path,
    model: str,
    permissions: AgentPermissions,
    settings: AgentSettings,
    max_resumes: int,
    max_steps: int,
    message_observer_factory: MessageObserverFactory | None = None,
) -> PhaseDAGFactory:
    """Create fresh validated coding DAGs with caller-owned configuration."""

    def coding_agent(
        system_prompt: str,
        phase: PlanPhase,
        role: ValidatedRunRole,
    ) -> CodingAgent:
        return CodingAgent(
            model=model,
            system_prompt=system_prompt,
            cwd=workspace,
            permissions=permissions,
            message_observer=(message_observer_factory(phase, role) if message_observer_factory is not None else None),
            settings=settings,
        )

    def build(phase: PlanPhase) -> PhaseDAG:
        return build_validated_agent_dag(
            initial_agent=coding_agent(
                "Implement the caller-provided phase in the current workspace.",
                phase,
                ValidatedRunRole.INITIAL,
            ),
            validator_agent_factory=lambda: StructuredValidator(
                coding_agent(
                    "Inspect the workspace and determine whether the phase is complete.",
                    phase,
                    ValidatedRunRole.VALIDATOR,
                )
            ),
            resume_agent_factory=lambda: coding_agent(
                "Continue incomplete work for the caller-provided phase.",
                phase,
                ValidatedRunRole.RESUME,
            ),
            prompts=ValidatedRunPrompts(
                validator=lambda context, _: f"Validate the workspace against this phase:\n{context.prompt}",
                resume=resume_prompt,
            ),
            completion_predicate=result_is_complete,
            progress_detector=GitHeadProgressDetector(workspace),
            classify_error=classify_failure,
            policy=ValidatedRunPolicy(max_resumes=max_resumes, max_steps=max_steps),
        )

    return build


def build_dry_run_dag(_: PlanPhase) -> PhaseDAG:
    """Build a deterministic DAG that performs no model or network calls."""

    return build_validated_agent_dag(
        initial_agent=_DryRunAgent("phase rendered"),
        validator_agent_factory=lambda: _DryRunAgent("complete"),
        resume_agent_factory=lambda: _DryRunAgent("resumed"),
        prompts=ValidatedRunPrompts(validator=lambda context, _: str(context.prompt), resume=resume_prompt),
        completion_predicate=lambda result: result.output == "complete",
        progress_detector=_NoProgress(),
        classify_error=lambda _: FailureDisposition.RETRY,
        policy=ValidatedRunPolicy(max_resumes=_DRY_RUN_MAX_RESUMES, max_steps=_DRY_RUN_MAX_STEPS),
    )


async def run_code_factory(
    *,
    phases: Sequence[PlanPhase],
    workspace: Path,
    dag_factory: PhaseDAGFactory,
    checkpoint: CheckpointStore | None,
    on_phase_start: PhaseStartCallback | None = None,
) -> tuple[PromptStepResult[CodeFactoryContext], ...]:
    """Run ordered phase DAGs and checkpoint only successful results."""

    frozen_phases = tuple(phases)
    sequence = build_code_factory_sequence(frozen_phases, dag_factory)

    def before_step(context: PromptRenderContext[CodeFactoryContext]) -> None:
        if on_phase_start is not None:
            on_phase_start(context.index, frozen_phases[context.index])

    def save(result: PromptStepResult[CodeFactoryContext]) -> None:
        if checkpoint is not None:
            checkpoint.mark_completed(frozen_phases[result.index])

    return await sequence.run(
        CodeFactoryContext(workspace),
        resolve_start_index=((lambda _: checkpoint.start_index()) if checkpoint is not None else None),
        before_step=before_step,
        after_success=save,
    )


@dataclass(slots=True)
class _DryRunAgent:
    output: str

    async def run(self, prompt: AgentInput) -> AgentResult:
        del prompt
        return AgentResult(
            output=self.output,
            session_id="dry-run",
            is_error=False,
            progress=AgentProgress(num_messages=1),
        )


class _NoProgress:
    async def snapshot(self) -> None:
        return None

    def saved_progress(
        self,
        before: object,
        after: object,
        failure: Failure,
    ) -> bool:
        del before, after, failure
        return False
