"""
Tests for GenerationLifecyclePolicy ordering invariants.

Verifies that _check_lifecycle_policy raises ValueError when any cross-cutting
constraint is violated, and that the actual class constants satisfy all invariants.
"""
import pytest

from app.core.ttl_config import GenerationLifecyclePolicy, _check_lifecycle_policy


def _valid_kwargs() -> dict:
    return {
        "session_analysis_minutes": GenerationLifecyclePolicy.SESSION_ANALYSIS_MINUTES,
        "session_planning_minutes": GenerationLifecyclePolicy.SESSION_PLANNING_MINUTES,
        "session_generation_minutes": GenerationLifecyclePolicy.SESSION_GENERATION_MINUTES,
        "stuck_running_minutes": GenerationLifecyclePolicy.STUCK_RUNNING_MINUTES,
        "stuck_cleaning_hours": GenerationLifecyclePolicy.STUCK_CLEANING_HOURS,
        "agent_phase_timeout_seconds": GenerationLifecyclePolicy.AGENT_PHASE_TIMEOUT_SECONDS,
        "workspace_failed_retention_days": GenerationLifecyclePolicy.WORKSPACE_FAILED_RETENTION_DAYS,
    }


def test_default_constants_pass_validation() -> None:
    """The shipped constants must satisfy every invariant."""
    _check_lifecycle_policy(**_valid_kwargs())  # must not raise


def test_boundary_generation_ttl_one_minute_above_stuck_passes() -> None:
    """Strict > between generation session TTL and stuck-running (not >=)."""
    base = _valid_kwargs()
    kwargs = {**base, "session_generation_minutes": base["stuck_running_minutes"] + 1}
    _check_lifecycle_policy(**kwargs)


def test_boundary_agent_phase_one_minute_below_stuck_passes() -> None:
    """Agent phase timeout in whole minutes must be strictly < stuck-running."""
    stuck = _valid_kwargs()["stuck_running_minutes"]
    kwargs = {
        **_valid_kwargs(),
        "agent_phase_timeout_seconds": (stuck - 1) * 60,
    }
    _check_lifecycle_policy(**kwargs)


@pytest.mark.parametrize("override,match_fragment", [
    (
        {"session_analysis_minutes": 90, "session_planning_minutes": 90},
        "Analysis TTL",
    ),
    (
        {"session_planning_minutes": 2880, "session_generation_minutes": 90},
        "Planning TTL",
    ),
    (
        {"session_generation_minutes": 700, "stuck_running_minutes": 720},
        "Generation session TTL",
    ),
    (
        # agent timeout (720 min) >= stuck_running (720 min) → violates invariant 4
        {"agent_phase_timeout_seconds": 720 * 60, "stuck_running_minutes": 720},
        "Agent phase timeout",
    ),
    (
        # 1 day retention = 1440 min < 2880 min generation TTL → violates invariant 5
        {"workspace_failed_retention_days": 1, "session_generation_minutes": 2880},
        "Workspace retention",
    ),
    (
        # stuck_running (100 min) <= stuck_cleaning (2 h = 120 min) → violates the
        # stuck-running/cleaning ordering. Set agent_phase_timeout_seconds to 50 min so
        # the earlier check (agent phase < stuck) passes first.
        {
            "stuck_running_minutes": 100,
            "stuck_cleaning_hours": 2,
            "agent_phase_timeout_seconds": 50 * 60,
        },
        "Stuck-running threshold",
    ),
])
def test_violated_invariant_raises_value_error(override: dict, match_fragment: str) -> None:
    """Each broken invariant must produce a ValueError naming the violated constraint."""
    kwargs = {**_valid_kwargs(), **override}
    with pytest.raises(ValueError, match=match_fragment):
        _check_lifecycle_policy(**kwargs)
