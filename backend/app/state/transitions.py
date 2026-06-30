"""
Transition tables for GenerationSessionStateMachine and WorkspaceStateMachine.
These are plain data — no logic, no side effects. Each entry is the
authoritative definition of what is allowed.
"""
from dataclasses import dataclass
from app.schemas.generation_workflow_enums import GenerationStatus, WorkspaceStatus


@dataclass(frozen=True)
class GenerationSessionTransition:
    name: str
    from_states: frozenset
    to_state: str
    required_fields: tuple = ()       # caller must provide these kwargs
    forbidden_in_test: bool = False   # safety-sensitive; opt-in in unit tests


@dataclass(frozen=True)
class WorkspaceTransition:
    name: str
    from_states: frozenset
    to_state: str
    requires_transaction: bool = False
    required_fields: tuple = ()


# Gap 5 fix: standardised triggered_by strings.
# Every caller uses these constants. Every state_history entry is then queryable
# by prefix (e.g. triggered_by.startswith("job:") finds all background-job actions).
# Do NOT use bare string literals for triggered_by anywhere in the codebase.
class TriggeredBy:
    # API / service layer
    CREATE              = "api:create_generation_session"
    START               = "api:start_generation_session"
    MANUAL_RETRY        = "api:manual_retry"
    RUN_GENERATION      = "api:run_generation"
    RESEND_EMAIL        = "api:resend_email"
    FORCE_RELEASE       = "admin:force_release"
    VALIDATE_CONTRACT      = "api:validate_contract"

    # Admin / operator workspace commands
    ADMIN_CLEAN         = "admin:clean_available"
    ADMIN_DEALLOCATE    = "admin:deallocate"
    ADMIN_RELEASE_STUCK = "admin:release_stuck"

    # Orchestrator — step name appended: e.g. "orchestrator:generation"
    ORCHESTRATOR_PREFIX = "orchestrator"

    # Background jobs
    STUCK_RUNNING       = "job:stuck_running_detector"
    STUCK_INITIALIZING  = "job:stuck_initializing_detector"
    STUCK_CLEANING      = "job:stuck_cleaning_recovery"
    SCHEDULED_WIPE      = "job:scheduled_wipe"

    # Server lifecycle (graceful shutdown / boot recovery)
    SHUTDOWN            = "system:server_shutdown"
    SHUTDOWN_RECOVERY   = "system:server_boot_recovery"

    # Orchestrator — model fallback due to routing failure
    MODEL_FALLBACK      = "orchestrator:phase_agent_model_fallback"

    @staticmethod
    def orchestrator_step(step_name: str) -> str:
        """Returns e.g. 'orchestrator:generation' for the generation step."""
        return f"orchestrator:{step_name}"


GENERATION_SESSION_TRANSITIONS: dict[str, GenerationSessionTransition] = {
    "create": GenerationSessionTransition(
        name="create",
        from_states=frozenset(),   # no prior state required
        to_state=GenerationStatus.PENDING,
    ),
    "begin_allocation": GenerationSessionTransition(
        name="begin_allocation",
        from_states=frozenset([GenerationStatus.PENDING]),
        to_state=GenerationStatus.INITIALIZING,
    ),
    "allocation_succeeded": GenerationSessionTransition(
        name="allocation_succeeded",
        from_states=frozenset([GenerationStatus.INITIALIZING]),
        to_state=GenerationStatus.RUNNING,
    ),
    "allocation_failed": GenerationSessionTransition(
        name="allocation_failed",
        from_states=frozenset([GenerationStatus.INITIALIZING]),
        to_state=GenerationStatus.PENDING,
    ),
    "advance_checkpoint": GenerationSessionTransition(
        name="advance_checkpoint",
        from_states=frozenset([GenerationStatus.RUNNING]),
        to_state=GenerationStatus.RUNNING,   # status stays RUNNING
        required_fields=("checkpoint", "triggered_by"),
    ),
    "complete": GenerationSessionTransition(
        name="complete",
        from_states=frozenset([GenerationStatus.RUNNING]),
        to_state=GenerationStatus.COMPLETED,
        forbidden_in_test=True,  # involves git ops + email
    ),
    "fail": GenerationSessionTransition(
        name="fail",
        from_states=frozenset([
            GenerationStatus.PENDING,
            GenerationStatus.INITIALIZING,
            GenerationStatus.RUNNING,
        ]),
        to_state=GenerationStatus.FAILED,
        required_fields=("reason", "triggered_by"),
    ),
    "stuck_detected": GenerationSessionTransition(
        name="stuck_detected",
        from_states=frozenset([GenerationStatus.RUNNING]),
        to_state=GenerationStatus.FAILED,
        required_fields=("triggered_by",),
    ),
    "reset_for_retry": GenerationSessionTransition(
        name="reset_for_retry",
        from_states=frozenset([GenerationStatus.FAILED]),
        to_state=GenerationStatus.PENDING,
        required_fields=("triggered_by",),
    ),
    "reject_contract": GenerationSessionTransition(
        name="reject_contract",
        from_states=frozenset([GenerationStatus.RUNNING]),
        to_state=GenerationStatus.PENDING,
        required_fields=("reason", "triggered_by"),
    ),
}


WORKSPACE_TRANSITIONS: dict[str, WorkspaceTransition] = {
    "allocate": WorkspaceTransition(
        name="allocate",
        from_states=frozenset([WorkspaceStatus.AVAILABLE]),
        to_state=WorkspaceStatus.ALLOCATED,
        requires_transaction=True,
        required_fields=("locked_by",),
    ),
    "allocation_rollback": WorkspaceTransition(
        name="allocation_rollback",
        from_states=frozenset([WorkspaceStatus.ALLOCATED]),
        to_state=WorkspaceStatus.CLEANING,   # HR-4 Option A: go through CLEANING
        required_fields=("locked_by",),
    ),
    "archive_and_release": WorkspaceTransition(
        name="archive_and_release",
        from_states=frozenset([WorkspaceStatus.ALLOCATED]),
        to_state=WorkspaceStatus.CLEANING,
        requires_transaction=True,
        required_fields=("locked_by",),       # ownership checked inside
    ),
    "mark_clean": WorkspaceTransition(
        name="mark_clean",
        from_states=frozenset([WorkspaceStatus.CLEANING]),
        to_state=WorkspaceStatus.AVAILABLE,
    ),
    "mark_stuck": WorkspaceTransition(
        name="mark_stuck",
        from_states=frozenset([WorkspaceStatus.CLEANING]),  # NOT from ALLOCATED (Invariant 2)
        to_state=WorkspaceStatus.STUCK,
    ),
    "begin_recovery": WorkspaceTransition(
        name="begin_recovery",
        from_states=frozenset([WorkspaceStatus.STUCK]),
        to_state=WorkspaceStatus.CLEANING,
    ),
    "schedule_wipe": WorkspaceTransition(
        name="schedule_wipe",
        from_states=frozenset([WorkspaceStatus.ALLOCATED]),   # stays ALLOCATED
        to_state=WorkspaceStatus.ALLOCATED,                   # status unchanged
        required_fields=("scheduled_for_wipe_at",),
    ),
    "force_release": WorkspaceTransition(
        name="force_release",
        from_states=frozenset([WorkspaceStatus.ALLOCATED]),
        to_state=WorkspaceStatus.CLEANING,
        required_fields=("reason", "confirmed_by"),           # operator must explain
    ),
    # Admin / operator escape hatches — all require explicit reason for audit trail
    "admin_clean_available": WorkspaceTransition(
        name="admin_clean_available",
        from_states=frozenset([WorkspaceStatus.AVAILABLE]),
        to_state=WorkspaceStatus.CLEANING,
    ),
    "admin_deallocate": WorkspaceTransition(
        name="admin_deallocate",
        from_states=frozenset([WorkspaceStatus.ALLOCATED]),
        to_state=WorkspaceStatus.AVAILABLE,
    ),
    "admin_release_stuck": WorkspaceTransition(
        name="admin_release_stuck",
        from_states=frozenset([WorkspaceStatus.STUCK]),
        to_state=WorkspaceStatus.AVAILABLE,
    ),
}
