from __future__ import annotations

import os

from settings import LangfuseSettings

LANGFUSE_BASE_URL_VARIABLE = "LANGFUSE_BASE_URL"
LANGFUSE_HOST_VARIABLE = "LANGFUSE_HOST"


def langfuse_settings_from_environment() -> LangfuseSettings:
    """Return the Langfuse settings of the process environment.

    The environment is snapshotted once and mapped through `LangfuseSettings`;
    `LANGFUSE_HOST` is the fallback for an unset `LANGFUSE_BASE_URL`.
    """

    environment = dict(os.environ)
    return LangfuseSettings.model_validate(
        {
            "LANGFUSE_BASE_URL": environment.get(LANGFUSE_BASE_URL_VARIABLE) or environment.get(LANGFUSE_HOST_VARIABLE),
            "LANGFUSE_PUBLIC_KEY": environment.get("LANGFUSE_PUBLIC_KEY"),
            "LANGFUSE_SECRET_KEY": environment.get("LANGFUSE_SECRET_KEY"),
        }
    )


def langfuse_base_url() -> str | None:
    """Return the configured Langfuse base URL, without a trailing slash.

    Reads `LANGFUSE_BASE_URL`, falling back to `LANGFUSE_HOST`; `None` when
    neither is set. Pure and network-free -- safe to call from a log
    statement's hot path.
    """

    base = langfuse_settings_from_environment().base_url
    return base.rstrip("/") if base else None


def langfuse_trace_url(trace_id: str | None) -> str | None:
    """Return the Langfuse UI URL for `trace_id`, or `None` when either is unset."""

    if trace_id is None:
        return None
    base = langfuse_base_url()
    return f"{base}/trace/{trace_id}" if base else None
