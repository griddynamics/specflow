"""
WorkflowOrchestrator — drives the generation workflow.

Defines GENERATION_WORKFLOW_STEPS (the authoritative step registry)
and WorkflowOrchestrator.run() which:
  - Reads current checkpoint
  - Skips already-completed steps
  - Calls each remaining step implementation
  - Advances checkpoint on success
  - Calls GenerationSessionStateMachine.fail on any step failure

The caller provides only a dict of step_name → async callable.
The caller does NOT handle failures, does NOT check checkpoints,
does NOT call complete() or fail().
"""
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable, Any

from app.schemas.generation_workflow_enums import (
    CHECKPOINT_ORDER,
    GenerationCheckpoint,
    WorkflowStepName,
    parse_checkpoint,
)
from app.services.contract_validator import ContractRejection
from app.state.cancellation import raise_if_cancelled
from app.state.exceptions import GenerationCancelledError, InvalidGenerationSessionStateError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowStep:
    name: WorkflowStepName
    advances_to: GenerationCheckpoint
    skip_if_at_or_past: GenerationCheckpoint   # When to skip this step


# Guard rules: defines when each step should be skipped.
# Key principle: skip_if_at_or_past must be at or before the checkpoint the step produces.
# For all current steps: skip if already at the checkpoint that step produces.
STEP_GUARD_RULES: dict[WorkflowStepName, GenerationCheckpoint] = {
    WorkflowStepName.VALIDATE_CONTRACT: GenerationCheckpoint.CONTRACT_VALIDATED,
    WorkflowStepName.KB_INIT: GenerationCheckpoint.KB_INIT_DONE,
    WorkflowStepName.GENERATION: GenerationCheckpoint.GENERATION_DONE,
    WorkflowStepName.DEPLOY_AND_E2E: GenerationCheckpoint.DEPLOY_AND_E2E_DONE,
    WorkflowStepName.ARCHIVE_OUTPUTS: GenerationCheckpoint.OUTPUTS_ARCHIVED,
    WorkflowStepName.ESTIMATION: GenerationCheckpoint.ESTIMATION_DONE,
}


def create_workflow_step(
    name: WorkflowStepName,
    advances_to: GenerationCheckpoint,
) -> WorkflowStep:
    """Create a workflow step with guard from STEP_GUARD_RULES.

    The skip guard is NOT a parameter—it comes from the specification rules.
    This makes it impossible to use the wrong guard.

    Args:
        name: Step identifier (looked up in STEP_GUARD_RULES)
        advances_to: Checkpoint this step produces when successful

    Returns:
        WorkflowStep with guard automatically set from rules

    Raises:
        KeyError: If step name is not in STEP_GUARD_RULES (catches new steps without defined guards)
    """
    skip_guard = STEP_GUARD_RULES[name]  # Raises KeyError if step not configured
    return WorkflowStep(
        name=name,
        advances_to=advances_to,
        skip_if_at_or_past=skip_guard,
    )


def _validate_step_guard_rules() -> None:
    """Validate STEP_GUARD_RULES at module load time.

    Enforces invariants:
    1. Every WorkflowStepName has a guard rule
    2. Guard checkpoint is at or before advances_to checkpoint (can't skip past completion)
    """
    rank = {cp: i for i, cp in enumerate(CHECKPOINT_ORDER)}

    for step_name in WorkflowStepName:
        if step_name not in STEP_GUARD_RULES:
            raise ValueError(
                f"STEP_GUARD_RULES missing entry for {step_name.value}. "
                f"Every step must have a defined guard."
            )

    for step in GENERATION_WORKFLOW_STEPS:
        guard_rank = rank.get(step.skip_if_at_or_past)
        advances_rank = rank.get(step.advances_to)

        if guard_rank is None:
            raise ValueError(
                f"{step.name.value}: guard {step.skip_if_at_or_past.value} "
                f"not in CHECKPOINT_ORDER"
            )
        if advances_rank is None:
            raise ValueError(
                f"{step.name.value}: advances_to {step.advances_to.value} "
                f"not in CHECKPOINT_ORDER"
            )
        if guard_rank > advances_rank:
            raise ValueError(
                f"{step.name.value}: guard {step.skip_if_at_or_past.value} "
                f"comes AFTER advances_to {step.advances_to.value} in CHECKPOINT_ORDER. "
                f"This violates the invariant that we can't skip past a step's completion."
            )

    logger.info("✓ STEP_GUARD_RULES validated: all invariants satisfied")


# Authoritative ordered sequence of generation workflow steps.
# This is the single source of truth for what an generation run does.
# VALIDATE_CONTRACT runs first — it normalizes uploaded files to canonical paths and
# converts the user-provided markdown plans to JSON. Without it, downstream steps have
# no structured plan data to operate on.
GENERATION_WORKFLOW_STEPS: list[WorkflowStep] = [
    create_workflow_step(WorkflowStepName.VALIDATE_CONTRACT, GenerationCheckpoint.CONTRACT_VALIDATED),
    create_workflow_step(WorkflowStepName.KB_INIT, GenerationCheckpoint.KB_INIT_DONE),
    create_workflow_step(WorkflowStepName.GENERATION, GenerationCheckpoint.GENERATION_DONE),
    # Optional step — only included by callers when integration_readiness == INTEGRATION_TESTS_READY.
    create_workflow_step(WorkflowStepName.DEPLOY_AND_E2E, GenerationCheckpoint.DEPLOY_AND_E2E_DONE),
    create_workflow_step(WorkflowStepName.ARCHIVE_OUTPUTS, GenerationCheckpoint.OUTPUTS_ARCHIVED),
    create_workflow_step(WorkflowStepName.ESTIMATION, GenerationCheckpoint.ESTIMATION_DONE),
]

# Validate rules at module import time (fail fast if violated)
_validate_step_guard_rules()

# Ordered for skip-logic comparison
_CHECKPOINT_RANK: dict[str, int] = {
    cp.advances_to: i for i, cp in enumerate(GENERATION_WORKFLOW_STEPS)
}

# GENERATION_STARTED is a checkpoint name in the enum but no workflow step advances to it
# (it's a marker we kept for telemetry continuity). For skip-logic purposes, treat it
# like KB_INIT_DONE — earlier steps stay skipped, the generation step re-runs.
_CHECKPOINT_RANK[GenerationCheckpoint.GENERATION_STARTED] = _CHECKPOINT_RANK[
    GenerationCheckpoint.KB_INIT_DONE
]


class WorkflowOrchestrator:
    """
    Drives generation workflow execution. Injected into workflow files.

    Usage:
        await orchestrator.run(
            generation_id=generation_id,
            step_implementations={
                WorkflowStepName.VALIDATE_CONTRACT: lambda: run_contract_validator(ctx),
                WorkflowStepName.KB_INIT:           lambda: run_kb_init_agent(ctx),
                WorkflowStepName.GENERATION:        lambda: run_generation_phases(ctx),
                WorkflowStepName.ARCHIVE_OUTPUTS:   lambda: run_archive_outputs_step(ctx),
            },
            triggered_by="run_generation_session",
        )
    """

    def __init__(self, db, generation_session_state_machine, workspace_archive_service=None):
        self._db = db
        self._esm = generation_session_state_machine
        self._was = workspace_archive_service

    async def run(
        self,
        generation_id: str,
        step_implementations: dict[str, Callable[[], Awaitable[Any]]],
        triggered_by: str,
        result_builder: Callable | None = None,
        complete_on_finish: bool = True,
    ) -> None:
        """
        Execute all steps that haven't been completed yet.
        On step failure: calls GenerationSessionStateMachine.fail and raises.
        On full completion: calls GenerationSessionStateMachine.complete (unless complete_on_finish=False).

        Args:
            complete_on_finish: If False, skip the complete() call at the end.
                Use when this orchestrator.run() handles only a partial set of steps
                and completion is managed externally.
        """
        doc = await self._db.get_generation_session(generation_id)
        current_checkpoint = doc.get("checkpoint")

        for step in GENERATION_WORKFLOW_STEPS:
            impl = step_implementations.get(step.name)
            if impl is None:
                # Step has no implementation provided — skip silently
                # (allows callers to omit optional steps)
                continue

            if self._should_skip(current_checkpoint, step):
                logger.info(
                    "generation %s skip step '%s' (already at %s)",
                    generation_id, step.name, current_checkpoint
                )
                continue

            # Cooperative cancellation: stop before starting the next step if the user
            # cancelled (cross-pod-safe fallback to the local task.cancel()).
            await raise_if_cancelled(self._db, generation_id)

            logger.info("generation %s running step '%s'", generation_id, step.name)
            try:
                await impl()
            except ContractRejection:
                # Pre-flight rejection — caller handles cleanup without fail().
                # See CLAUDE.md: "A rejection is NOT a state-machine fail()".
                logger.info(
                    "generation %s step '%s' raised ContractRejection — propagating without fail()",
                    generation_id, step.name,
                )
                raise
            except GenerationCancelledError:
                # User cancellation — session is already CANCELLED. Propagate without
                # fail() (mirrors the ContractRejection passthrough); the shared workflow
                # exception handler treats it as a silent no-op.
                logger.info(
                    "generation %s step '%s' observed cancellation — propagating without fail()",
                    generation_id, step.name,
                )
                raise
            except Exception as exc:
                logger.error(
                    "generation %s step '%s' failed: %s",
                    generation_id, step.name, exc
                )
                try:
                    await self._esm.fail(
                        generation_id=generation_id,
                        reason=str(exc),
                        triggered_by=f"{triggered_by}:{step.name.value}",
                    )
                except InvalidGenerationSessionStateError:
                    # Inner orchestrator (e.g. generate_poc) already called fail().
                    # Log and continue — the generation is already FAILED.
                    logger.debug(
                        "generation %s already FAILED (by inner orchestrator); "
                        "skipping redundant fail() call",
                        generation_id,
                    )
                raise

            await self._esm.advance_checkpoint(
                generation_id=generation_id,
                checkpoint=step.advances_to,
                triggered_by=f"{triggered_by}:{step.name.value}",
            )

        # All steps complete — call complete() unless deferred
        if complete_on_finish:
            result = await result_builder() if result_builder else None
            await self._esm.complete(
                generation_id=generation_id,
                triggered_by=triggered_by,
                workspace_archive_service=self._was,
                result=result,
            )

    def _should_skip(
        self, current_checkpoint: str | None, step: WorkflowStep
    ) -> bool:
        normalized = parse_checkpoint(current_checkpoint)
        if normalized is None:
            return False
        current_rank = _CHECKPOINT_RANK.get(normalized, -1)
        step_rank = _CHECKPOINT_RANK.get(step.skip_if_at_or_past, 0)
        return current_rank >= step_rank
