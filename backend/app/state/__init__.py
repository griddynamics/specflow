"""
State Controller Layer — the only place in the codebase that may write
status, checkpoint, or workspace_phases fields to Firestore.

Exports:
    GenerationSessionStateMachine
    WorkspaceStateMachine
    WorkflowOrchestrator
    InvalidGenerationSessionStateError
    InvalidWorkspaceStateError
    GenerationCancelledError
    raise_if_cancelled
"""
from .generation_session_state_machine import GenerationSessionStateMachine
from .workspace_state_machine import WorkspaceStateMachine
from .workflow_orchestrator import WorkflowOrchestrator
from .exceptions import (
    InvalidGenerationSessionStateError,
    InvalidWorkspaceStateError,
    GenerationCancelledError,
)
from .cancellation import raise_if_cancelled

__all__ = [
    "GenerationSessionStateMachine",
    "WorkspaceStateMachine",
    "WorkflowOrchestrator",
    "InvalidGenerationSessionStateError",
    "InvalidWorkspaceStateError",
    "GenerationCancelledError",
    "raise_if_cancelled",
]
