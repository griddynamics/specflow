"""
Generation workflow enums for type safety and preventing typos.

All enums inherit from str and Enum to be JSON-serializable and work with
database operations while providing type safety.
"""

from enum import Enum
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GenerationStatus(str, Enum):
    """Generation session lifecycle status values.

    Lifecycle:
      PENDING → INITIALIZING → RUNNING → COMPLETED | FAILED

    Spec analysis and planning run locally in the IDE before ``run_generation``.
    """
    PENDING = "pending"
    INITIALIZING = "initializing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class GenerationCheckpoint(str, Enum):
    """Generation workflow checkpoint values.

    Checkpoints progress in this order (see CHECKPOINT_ORDER below). Users do
    spec/plan work locally, then ``run_generation`` uploads artifacts and the
    backend walks forward through these checkpoints.

    1. FILES_UPLOADED - Specs/src/outputs uploaded to primary workspace
    2. CONTRACT_VALIDATED - Required files at canonical paths, plans converted to JSON
    3. KB_INIT_DONE - Knowledge base context fetched (Rosetta MCP)
    4. GENERATION_STARTED - Code generation started (telemetry continuity)
    5. GENERATION_DONE - All execution phases completed for all workspaces
    6. DEPLOY_AND_E2E_DONE - Deploy + e2e loop (INTEGRATION_TESTS_READY only)
    7. OUTPUTS_ARCHIVED - Outputs archived (Steel Commandment XI gate)
    8. ESTIMATION_DONE - P10Y metrics and estimation reports completed
    """
    FILES_UPLOADED = "files_uploaded"
    CONTRACT_VALIDATED = "contract_validated"
    KB_INIT_DONE = "kb_init_done"
    GENERATION_STARTED = "generation_started"
    GENERATION_DONE = "generation_done"
    DEPLOY_AND_E2E_DONE = "deploy_and_e2e_done"
    OUTPUTS_ARCHIVED = "outputs_archived"
    ESTIMATION_DONE = "estimation_done"

    @classmethod
    def get_next_checkpoint(cls, current: "GenerationCheckpoint") -> Optional["GenerationCheckpoint"]:
        """Get the next checkpoint in sequence, or None if already at the last one."""
        try:
            current_index = CHECKPOINT_ORDER.index(current)
            if current_index < len(CHECKPOINT_ORDER) - 1:
                return CHECKPOINT_ORDER[current_index + 1]
        except ValueError:
            pass
        return None

    @classmethod
    def is_valid_transition(cls, from_checkpoint: "GenerationCheckpoint", to_checkpoint: "GenerationCheckpoint") -> bool:
        """Check if transitioning from one checkpoint to another is valid (strictly forward)."""
        try:
            from_index = CHECKPOINT_ORDER.index(from_checkpoint)
            to_index = CHECKPOINT_ORDER.index(to_checkpoint)
            return to_index > from_index
        except ValueError:
            return False

    @classmethod
    def is_at_or_after(cls, checkpoint: "GenerationCheckpoint | str | None", minimum: "GenerationCheckpoint") -> bool:
        """Return True if checkpoint is at or after minimum in CHECKPOINT_ORDER.

        Use this for readiness guards (e.g. "has planning finished?"), NOT for
        validating state-machine transitions.  is_valid_transition is strict-forward
        only and will return False for the same-checkpoint case.
        """
        parsed = parse_checkpoint(checkpoint)
        if parsed is None:
            return False
        try:
            return CHECKPOINT_ORDER.index(parsed) >= CHECKPOINT_ORDER.index(minimum)
        except ValueError:
            return False


# Canonical checkpoint ordering — single source of truth.
# Every component that needs checkpoint ordering must reference this list.
# DEPLOY_AND_E2E_DONE is intentionally skipped for LOCAL_ONLY generation sessions.
# OUTPUTS_ARCHIVED (coding/deploy artifact protection) must precede ESTIMATION_DONE (P10Y).
CHECKPOINT_ORDER: list[GenerationCheckpoint] = [
    GenerationCheckpoint.FILES_UPLOADED,
    GenerationCheckpoint.CONTRACT_VALIDATED,
    GenerationCheckpoint.KB_INIT_DONE,
    GenerationCheckpoint.GENERATION_STARTED,
    GenerationCheckpoint.GENERATION_DONE,
    GenerationCheckpoint.DEPLOY_AND_E2E_DONE,
    GenerationCheckpoint.OUTPUTS_ARCHIVED,
    GenerationCheckpoint.ESTIMATION_DONE,
]


def parse_checkpoint(value: GenerationCheckpoint | str | None) -> GenerationCheckpoint | None:
    """Parse a stored checkpoint field into ``GenerationCheckpoint``, or None if invalid."""
    if value is None:
        return None
    if isinstance(value, GenerationCheckpoint):
        return value
    try:
        return GenerationCheckpoint(value)
    except ValueError:
        logger.warning("Unknown generation checkpoint value %r", value)
        return None


def parse_status(value: GenerationStatus | str | None) -> GenerationStatus | None:
    """Parse a stored status field into ``GenerationStatus``, or None if invalid."""
    if value is None:
        return None
    if isinstance(value, GenerationStatus):
        return value
    try:
        return GenerationStatus(value)
    except ValueError:
        logger.warning("Unknown generation status value %r", value)
        return None


class WorkflowStepName(str, Enum):
    """Canonical names for generation workflow steps (SSOT: GENERATION_WORKFLOW_STEPS).

    Values match the ``name`` field of each ``WorkflowStep`` in workflow_orchestrator.py.
    Use these members anywhere a step name is compared or stored — never bare strings.
    """

    VALIDATE_CONTRACT = "validate_contract"
    KB_INIT = "kb_init"
    GENERATION = "generation"
    DEPLOY_AND_E2E = "deploy_and_e2e"
    ARCHIVE_OUTPUTS = "archive_outputs"
    ESTIMATION = "estimation"


class WorkspacePhaseStatus(str, Enum):
    """Workspace phase execution status."""
    PENDING = "pending"
    PLANNING = "planning"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkspaceStatus(str, Enum):
    """Workspace lifecycle status values."""
    AVAILABLE = "available"
    ALLOCATED = "allocated"
    CLEANING = "cleaning"
    STUCK = "stuck"
