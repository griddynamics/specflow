"""Tests for tool_usage.py — focused on the Bash-timeout invariants that
previously lived in ttl_config.py.
"""
import pytest

from app.core.tool_usage import (
    BASH_DEFAULT_TIMEOUT_MS,
    BASH_MAX_TIMEOUT_MS,
    _validate_bash_timeouts,
)
from app.core.ttl_config import GenerationLifecyclePolicy


def test_shipped_bash_timeouts_satisfy_invariant() -> None:
    """The shipped constants must clear the invariant on the live phase timeout."""
    _validate_bash_timeouts(
        default_ms=BASH_DEFAULT_TIMEOUT_MS,
        max_ms=BASH_MAX_TIMEOUT_MS,
        phase_timeout_seconds=GenerationLifecyclePolicy.AGENT_PHASE_TIMEOUT_SECONDS,
    )  # must not raise


def test_shipped_bash_timeouts_have_sane_values() -> None:
    """Sanity-check the units (ms, not seconds) and rough ordering of magnitude."""
    assert BASH_DEFAULT_TIMEOUT_MS >= 60_000          # >= 1 minute
    assert BASH_MAX_TIMEOUT_MS >= BASH_DEFAULT_TIMEOUT_MS
    assert BASH_MAX_TIMEOUT_MS < GenerationLifecyclePolicy.AGENT_PHASE_TIMEOUT_SECONDS * 1000


@pytest.mark.parametrize("default_ms,max_ms,phase_seconds,match_fragment", [
    # default > max → invariant 1
    (60 * 60 * 1000, 30 * 60 * 1000, 5 * 3600, "BASH_DEFAULT_TIMEOUT_MS"),
    # max >= phase timeout (in ms) → invariant 2
    (5 * 60 * 1000, 30 * 60 * 1000, 30 * 60, "BASH_MAX_TIMEOUT_MS"),
])
def test_invariant_raises_on_bad_values(
    default_ms: int,
    max_ms: int,
    phase_seconds: int,
    match_fragment: str,
) -> None:
    with pytest.raises(ValueError, match=match_fragment):
        _validate_bash_timeouts(
            default_ms=default_ms,
            max_ms=max_ms,
            phase_timeout_seconds=phase_seconds,
        )


def test_invariant_allows_boundary_equal_default_max() -> None:
    """default == max is allowed (the agent then has no headroom to ask for more)."""
    _validate_bash_timeouts(default_ms=600_000, max_ms=600_000, phase_timeout_seconds=3600)
