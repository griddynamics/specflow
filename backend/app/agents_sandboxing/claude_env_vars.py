import os

from app.core.config import Settings


# Sentinel written into the agent subprocess env in place of any secret-looking
# variable. Distinct from "" so it is easy to grep in logs.
REDACTED_PLACEHOLDER = "redacted"

# Conservative substring heuristic for "this env var probably carries a credential".
# Matched case-insensitively against the variable name. See docs/agents/env-vars-leak.md.
_SECRET_NAME_SUBSTRINGS: frozenset[str] = frozenset(
    {"TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "WEBHOOK"}
)


def _is_secret_env_name(name: str) -> bool:
    upper = name.upper()
    return any(sub in upper for sub in _SECRET_NAME_SUBSTRINGS)


def build_redacted_env_overlay() -> dict[str, str]:
    """Shadow secret-looking env vars so the agent subprocess cannot read real values.

    Callers MUST merge this overlay BEFORE intentional real values so those win:
    ``{**build_redacted_env_overlay(), **env_config}``. See docs/agents/env-vars-leak.md.
    """
    return {
        name: REDACTED_PLACEHOLDER
        for name in (*Settings.model_fields, *os.environ)
        if _is_secret_env_name(name)
    }
