from __future__ import annotations

import os
from collections.abc import Mapping

REDACTED_ENV_VALUE = "redacted"

_SECRET_NAME_PARTS = frozenset({"CREDENTIAL", "KEY", "PASSWORD", "SECRET", "TOKEN", "WEBHOOK"})


def is_secret_environment_name(name: str) -> bool:
    """Return whether an environment name probably carries a credential."""

    upper_name = name.upper()
    return any(part in upper_name for part in _SECRET_NAME_PARTS)


def build_redacted_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Shadow ambient secret-looking variables before launching the SDK."""

    source = os.environ if environ is None else environ
    return {name: REDACTED_ENV_VALUE for name in source if is_secret_environment_name(name)}


def build_agent_environment(
    *overlays: Mapping[str, str] | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge redaction first, then intentional caller overlays in order."""

    result = build_redacted_environment(environ)
    for overlay in overlays:
        if overlay is None:
            continue
        invalid = {
            name: value
            for name, value in overlay.items()
            if not name or name != name.strip() or not isinstance(value, str)
        }
        if invalid:
            raise ValueError("Environment overlays require non-empty names and string values")
        result.update(overlay)
    return result
