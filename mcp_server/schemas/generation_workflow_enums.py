"""MCP-side generation status and checkpoint enums.

These mirror backend/app/schemas/generation_workflow_enums.py but are defined independently
because the MCP server package cannot import from the backend package.
Keep values in sync with the backend when the backend enums change.
"""

from enum import Enum


class GenerationStatus(str, Enum):
    PENDING = "pending"
    INITIALIZING = "initializing"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class GenerationCheckpoint(str, Enum):
    FILES_UPLOADED = "files_uploaded"
    CONTRACT_VALIDATED = "contract_validated"
    KB_INIT_DONE = "kb_init_done"
    GENERATION_STARTED = "generation_started"
    GENERATION_DONE = "generation_done"
    DEPLOY_AND_E2E_DONE = "deploy_and_e2e_done"
    OUTPUTS_ARCHIVED = "outputs_archived"
    ESTIMATION_DONE = "estimation_done"
