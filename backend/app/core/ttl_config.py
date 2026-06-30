"""
Single source of truth for all time-based lifecycle constants.

Three independent mechanisms govern an generation's lifetime:
  1. Session TTL   — orphan cleanup for API key concurrency slots
                     (for crashed pods that never called end(), NOT for active jobs)
  2. Stuck detection — marks stale RUNNING/INITIALIZING/CLEANING generations as FAILED
  3. Workspace retention — schedules filesystem wipe after FAILED retention window

Import-time checks enforce the ordering invariants that keep these mechanisms coherent.
Without them a local edit to any one constant can silently violate a cross-cutting constraint.

Required ordering:
  SESSION_GENERATION > STUCK_RUNNING
    Slot must outlive stuck detection; a healthy long job must not lose its concurrency slot
    before the stuck detector could ever fire.

  AGENT_PHASE_TIMEOUT < STUCK_RUNNING
    Each individual phase must time out before the stuck detector marks the whole generation
    FAILED; otherwise a single hung phase permanently blocks the key.

  WORKSPACE_FAILED_RETENTION > SESSION_GENERATION
    Workspace code must survive longer than the longest possible orphaned slot so that wipe
    is never the first thing that happens to code that still has an active-looking slot.
"""


def _check_lifecycle_policy(
    *,
    session_analysis_minutes: int,
    session_planning_minutes: int,
    session_generation_minutes: int,
    stuck_running_minutes: int,
    stuck_cleaning_hours: int,
    agent_phase_timeout_seconds: int,
    workspace_failed_retention_days: int,
) -> None:
    """Validate cross-cutting ordering invariants. Raises ValueError on violation."""
    if session_analysis_minutes >= session_planning_minutes:
        raise ValueError(
            f"Analysis TTL ({session_analysis_minutes}m) must be < "
            f"planning TTL ({session_planning_minutes}m)"
        )
    if session_planning_minutes >= session_generation_minutes:
        raise ValueError(
            f"Planning TTL ({session_planning_minutes}m) must be < "
            f"generation TTL ({session_generation_minutes}m)"
        )
    if session_generation_minutes <= stuck_running_minutes:
        raise ValueError(
            f"Generation session TTL ({session_generation_minutes}m) must exceed "
            f"stuck-running threshold ({stuck_running_minutes}m)"
        )
    if agent_phase_timeout_seconds // 60 >= stuck_running_minutes:
        raise ValueError(
            f"Agent phase timeout ({agent_phase_timeout_seconds // 60}m) must be < "
            f"stuck-running threshold ({stuck_running_minutes}m)"
        )
    if workspace_failed_retention_days * 24 * 60 <= session_generation_minutes:
        raise ValueError(
            f"Workspace retention ({workspace_failed_retention_days}d) must exceed "
            f"generation session TTL ({session_generation_minutes}m)"
        )
    if stuck_running_minutes <= stuck_cleaning_hours * 60:
        raise ValueError(
            f"Stuck-running threshold ({stuck_running_minutes}m) must exceed "
            f"stuck-cleaning threshold ({stuck_cleaning_hours * 60}m)"
        )


class GenerationLifecyclePolicy:
    # ------------------------------------------------------------------
    # Session TTL (orphan cleanup — how long before a slot is treated as
    # abandoned by a crashed process that never called end())
    # ------------------------------------------------------------------
    SESSION_ANALYSIS_MINUTES: int = 60
    SESSION_PLANNING_MINUTES: int = 90
    SESSION_GENERATION_MINUTES: int = 48 * 60  # 2 days — far longer than any realistic job

    # ------------------------------------------------------------------
    # Stuck-state detection thresholds
    # ------------------------------------------------------------------
    STUCK_RUNNING_MINUTES: int = 720          # 12 h — generation phases can run 90+ min each
    STUCK_INITIALIZING_MINUTES: int = 15
    STUCK_CLEANING_HOURS: int = 2
    ORPHANED_WORKSPACE_MINUTES: int = 60

    # ------------------------------------------------------------------
    # Per-phase agent hard timeout (prevents permanently hung processes)
    # ------------------------------------------------------------------
    AGENT_PHASE_TIMEOUT_SECONDS: int = 5 * 3600  # 5 h per phase

    # ------------------------------------------------------------------
    # Workspace retention after FAILED
    # ------------------------------------------------------------------
    WORKSPACE_FAILED_RETENTION_DAYS: int = 7
    WIPE_WARNING_HOURS: int = 24

    # ------------------------------------------------------------------
    # Ordering invariants — validated at class-definition time (import time)
    # ------------------------------------------------------------------
    _check_lifecycle_policy(
        session_analysis_minutes=SESSION_ANALYSIS_MINUTES,
        session_planning_minutes=SESSION_PLANNING_MINUTES,
        session_generation_minutes=SESSION_GENERATION_MINUTES,
        stuck_running_minutes=STUCK_RUNNING_MINUTES,
        stuck_cleaning_hours=STUCK_CLEANING_HOURS,
        agent_phase_timeout_seconds=AGENT_PHASE_TIMEOUT_SECONDS,
        workspace_failed_retention_days=WORKSPACE_FAILED_RETENTION_DAYS,
    )
