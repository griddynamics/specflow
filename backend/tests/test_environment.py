from __future__ import annotations

import pytest

from llm.environment import (
    REDACTED_ENV_VALUE,
    build_agent_environment,
    build_redacted_environment,
    is_secret_environment_name,
)


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "github_token",
        "DATABASE_PASSWORD",
        "CLIENT_SECRET",
        "SERVICE_CREDENTIAL",
        "DEPLOY_WEBHOOK_URL",
    ],
)
def test_secret_environment_names_are_case_insensitive(name: str) -> None:
    assert is_secret_environment_name(name)


def test_non_secret_environment_names_are_ignored() -> None:
    assert not is_secret_environment_name("PATH")
    assert not is_secret_environment_name("HOME")


def test_ambient_secrets_are_redacted_without_copying_other_values() -> None:
    environment = build_redacted_environment(
        {
            "PATH": "/bin",
            "GITHUB_TOKEN": "ambient-secret",
            "API_KEY": "ambient-key",
        }
    )

    assert environment == {
        "GITHUB_TOKEN": REDACTED_ENV_VALUE,
        "API_KEY": REDACTED_ENV_VALUE,
    }


def test_intentional_overlays_win_in_order() -> None:
    environment = build_agent_environment(
        {"ANTHROPIC_API_KEY": "provider-key", "SHARED": "provider"},
        {"SHARED": "extra", "EXTRA_TOKEN": "intentional-token"},
        environ={
            "ANTHROPIC_API_KEY": "ambient-key",
            "EXTRA_TOKEN": "ambient-token",
            "UNRELATED_SECRET": "ambient-secret",
        },
    )

    assert environment == {
        "ANTHROPIC_API_KEY": "provider-key",
        "EXTRA_TOKEN": "intentional-token",
        "UNRELATED_SECRET": REDACTED_ENV_VALUE,
        "SHARED": "extra",
    }


@pytest.mark.parametrize(
    "overlay",
    [
        {"": "value"},
        {" PADDED ": "value"},
        {"VALID": 1},
    ],
)
def test_invalid_environment_overlay_is_rejected(
    overlay: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="Environment overlays"):
        build_agent_environment(overlay)
