"""Display constants for the TUI.

The checkpoint ordering and labels mirror the backend SSOT in
``backend/app/schemas/generation_workflow_enums.py`` (``CHECKPOINT_ORDER``).
That module is **not** part of the shipped ``specflow`` client, so a small
local mirror is unavoidable across the client/backend boundary. It is
display-only: which steps are *done* is driven entirely by the ``checkpoint``
and ``completed_phases`` fields the ``/status`` endpoint already returns, so a
drift here only mislabels a step — it never changes behaviour.

The pipeline the user *sees* (``CHECKPOINT_STEPS``) is a curated, collapsed view
of the raw backend checkpoints (``CHECKPOINT_ORDER``): several backend
checkpoints can map to one displayed row. Progress is always computed against
``CHECKPOINT_ORDER`` (the full mirror) so a step is DONE only once its backend
completion checkpoint is reached. Keep ``CHECKPOINT_ORDER`` in lockstep with the
backend enum if checkpoints are added.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StepState(str, Enum):
    """Render state of a single pipeline step."""

    DONE = "done"
    ACTIVE = "active"
    PENDING = "pending"
    # Known-not-applicable to this run (e.g. Deploy & E2E on a local-only run).
    # Shown struck-through rather than hidden, so the user sees the full shape
    # of the pipeline and understands *why* a stage was omitted.
    SKIPPED = "skipped"


# Full mirror of backend CHECKPOINT_ORDER (string values of GenerationCheckpoint),
# in order. Used to locate the current checkpoint's position — NOT rendered
# directly. The poller also uses this to detect checkpoint advances, so it must
# list every backend checkpoint, including ones the displayed pipeline collapses.
CHECKPOINT_ORDER: list[str] = [
    "files_uploaded",
    "contract_validated",
    "kb_init_done",
    "generation_started",
    "generation_done",
    "deploy_and_e2e_done",
    "outputs_archived",
    "estimation_done",
]


@dataclass(frozen=True)
class PipelineStepDef:
    """One row in the displayed pipeline stepper.

    ``completed_at`` is the backend checkpoint (a value in ``CHECKPOINT_ORDER``)
    that marks this row DONE. A row may span several backend checkpoints — e.g.
    "Generating code" covers both ``generation_started`` and ``generation_done``
    — in which case ``completed_at`` is the *last* of them. The live phase and
    usage detail for the in-flight row is available by opening the workspace
    drill-in (``o`` / ``↵``), so intermediate checkpoints need no separate row.
    """

    label: str
    completed_at: str


# The displayed pipeline: a collapsed, human-facing view of CHECKPOINT_ORDER.
# "Generation started" and "Generating code" were adjacent rows that meant the
# same thing to a user ("code is being generated"); they are now one row, with
# the per-workspace phase/usage shown in the drill-in.
CHECKPOINT_STEPS: list[PipelineStepDef] = [
    PipelineStepDef("Files uploaded", "files_uploaded"),
    PipelineStepDef("Contract validated", "contract_validated"),
    PipelineStepDef("KB init", "kb_init_done"),
    PipelineStepDef("Generating code", "generation_done"),
    PipelineStepDef("Deploy & E2E", "deploy_and_e2e_done"),
    PipelineStepDef("Outputs archived", "outputs_archived"),
    PipelineStepDef("Estimation (P10Y)", "estimation_done"),
]

# Checkpoint that only runs for integration-test (non-local) generations. On a
# LOCAL_ONLY run this row is rendered SKIPPED (struck-through) rather than hidden.
DEPLOY_CHECKPOINT = "deploy_and_e2e_done"

# ``last_spec_readiness`` value (case-insensitive) marking a local-only run with
# no deploy/E2E phase. Mirror of SpecReadiness.LOCAL_ONLY in the backend.
LOCAL_ONLY_READINESS = "LOCAL_ONLY"

# Symbols for each step state in the stepper.
STEP_SYMBOLS: dict[StepState, str] = {
    StepState.DONE: "✔",
    StepState.ACTIVE: "●",
    StepState.PENDING: "▱",
    StepState.SKIPPED: "⊘",
}

# Terminal statuses — mirror of services.cli_service._TERMINAL_STATUSES.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed"})

# Status pill text + Rich style per lifecycle status (lowercased).
STATUS_PILLS: dict[str, tuple[str, str]] = {
    "pending": ("◌ PENDING", "dim"),
    "initializing": ("◍ INITIALIZING", "yellow"),
    "running": ("⬤ RUNNING", "green"),
    "completed": ("✓ COMPLETED", "bold green"),
    "failed": ("✗ FAILED", "bold red"),
    "unknown": ("? UNKNOWN", "dim"),
}

# Default poll cadence (seconds). The backend recommends 2–5s for /status.
DEFAULT_POLL_INTERVAL = 3
