"""Unit tests for the env-var helpers wired into agent_query."""
from app.core.tool_usage import BASH_DEFAULT_TIMEOUT_MS, BASH_MAX_TIMEOUT_MS
from app.services.claude_code import setup_claude_code_bash_timeouts


def test_setup_claude_code_bash_timeouts_returns_policy_values() -> None:
    env = setup_claude_code_bash_timeouts()
    assert env == {
        "BASH_DEFAULT_TIMEOUT_MS": str(BASH_DEFAULT_TIMEOUT_MS),
        "BASH_MAX_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
    }


def test_setup_claude_code_bash_timeouts_values_are_string_ints() -> None:
    env = setup_claude_code_bash_timeouts()
    # Subprocess env vars must be strings, but they have to round-trip to int.
    for key, value in env.items():
        assert isinstance(value, str), f"{key} must be a string for the subprocess env"
        int(value)  # raises if not int-parseable
