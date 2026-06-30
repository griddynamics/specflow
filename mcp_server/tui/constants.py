"""Display constants for the TUI.

The checkpoint ordering and labels mirror the backend SSOT in
``backend/app/schemas/generation_workflow_enums.py`` (``CHECKPOINT_ORDER``).
That module is **not** part of the shipped ``specflow`` client, so a small
local mirror is unavoidable across the client/backend boundary. It is
display-only: which steps are *done* is driven entirely by the ``checkpoint``
and ``completed_phases`` fields the ``/status`` endpoint already returns, so a
drift here only mislabels a step — it never changes behaviour.

Keep this list in lockstep with the backend enum if checkpoints are added.
"""

from __future__ import annotations

from enum import Enum


class StepState(str, Enum):
    """Render state of a single pipeline step."""

    DONE = "done"
    ACTIVE = "active"
    PENDING = "pending"


# Mirror of backend CHECKPOINT_ORDER (string values of GenerationCheckpoint),
# paired with the human label shown in the pipeline stepper.
CHECKPOINT_STEPS: list[tuple[str, str]] = [
    ("files_uploaded", "Files uploaded"),
    ("contract_validated", "Contract validated"),
    ("kb_init_done", "KB init"),
    ("generation_started", "Generation started"),
    ("generation_done", "Generating code"),
    ("deploy_and_e2e_done", "Deploy & E2E"),
    ("outputs_archived", "Outputs archived"),
    ("estimation_done", "Estimation (P10Y)"),
]

# Order-only view of the checkpoint keys (index → progress comparison).
CHECKPOINT_ORDER: list[str] = [key for key, _ in CHECKPOINT_STEPS]

# Checkpoint that only runs for integration-test (non-local) generations. It is
# hidden from the pipeline for LOCAL_ONLY runs — the primary local-usage case.
DEPLOY_CHECKPOINT = "deploy_and_e2e_done"

# ``last_spec_readiness`` value (case-insensitive) marking a local-only run with
# no deploy/E2E phase. Mirror of SpecReadiness.LOCAL_ONLY in the backend.
LOCAL_ONLY_READINESS = "LOCAL_ONLY"

# Symbols for each step state in the stepper.
STEP_SYMBOLS: dict[StepState, str] = {
    StepState.DONE: "✔",
    StepState.ACTIVE: "●",
    StepState.PENDING: "▱",
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
