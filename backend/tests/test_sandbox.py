from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage

import llm.agent as agent_module
import llm.sandbox as sandbox_module
from llm import (
    DEFAULT_SANDBOX_ALLOWED_DOMAINS,
    Agent,
    AgentPermissions,
    AgentSettings,
    NativeSandboxPolicy,
    SandboxUnavailableError,
    check_native_sandbox_available,
)
from llm.sandbox import resolve_sandbox


def test_default_policy_builds_fail_closed_allow_only_settings() -> None:
    settings, write_rules = resolve_sandbox(NativeSandboxPolicy())

    assert settings == {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": ["docker"],
        "network": {
            "allowedDomains": list(DEFAULT_SANDBOX_ALLOWED_DOMAINS),
        },
    }
    assert write_rules == ()


def test_policy_accepts_caller_overrides_and_extra_write_roots(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    settings, write_rules = resolve_sandbox(
        NativeSandboxPolicy(
            allowed_domains=("packages.example",),
            excluded_commands=("docker", "podman"),
            additional_write_roots=(shared,),
        )
    )

    assert settings["network"] == {"allowedDomains": ["packages.example"]}
    assert settings["excludedCommands"] == ["docker", "podman"]
    assert write_rules == (f"Edit(//{shared.as_posix().lstrip('/')}/**)",)


def test_false_is_the_explicit_unconfined_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_preflight() -> None:
        raise AssertionError("disabled sandbox must not preflight")

    monkeypatch.setattr(
        sandbox_module,
        "check_native_sandbox_available",
        unexpected_preflight,
    )

    assert resolve_sandbox(configuration=False) == (None, ())


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": False},
        {"enabled": True, "allowUnsandboxedCommands": True},
    ],
)
def test_custom_settings_cannot_silently_weaken_isolation(
    settings: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="sandbox"):
        resolve_sandbox(settings)


def test_macos_preflight_requires_sandbox_exec() -> None:
    available = check_native_sandbox_available(
        platform="darwin",
        which=lambda name: f"/usr/bin/{name}",
    )
    unavailable = check_native_sandbox_available(
        platform="darwin",
        which=lambda _: None,
    )

    assert available is None
    assert unavailable is not None
    assert unavailable.dependencies == ("sandbox-exec",)


def test_linux_preflight_reports_all_missing_dependencies() -> None:
    unavailable = check_native_sandbox_available(
        platform="linux",
        which=lambda _: None,
    )

    assert unavailable is not None
    assert unavailable.dependencies == ("bwrap", "socat")
    assert "bubblewrap" in unavailable.message


def test_unsupported_platform_fails_closed() -> None:
    unavailable = check_native_sandbox_available(
        platform="win32",
        which=lambda _: None,
    )

    assert unavailable is not None
    assert unavailable.platform == "win32"
    assert "sandbox=False" in unavailable.message


@pytest.mark.asyncio
async def test_default_sandbox_is_applied_before_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def stream() -> Any:
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result="done",
            usage=None,
        )

    def fake_query(*, prompt: object, options: object) -> object:
        captured["options"] = options
        return stream()

    monkeypatch.setattr(agent_module, "query", fake_query)

    await Agent(model="test").run("hello")

    sandbox = captured["options"].sandbox
    assert sandbox["enabled"] is True
    assert sandbox["allowUnsandboxedCommands"] is False


@pytest.mark.asyncio
async def test_unavailable_sandbox_fails_before_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_query(*, prompt: object, options: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("query must not start")

    monkeypatch.setattr(agent_module, "query", fake_query)
    monkeypatch.setattr(
        sandbox_module,
        "check_native_sandbox_available",
        lambda: sandbox_module.SandboxUnavailable(
            platform="linux",
            dependencies=("bwrap",),
            message="bwrap is unavailable",
        ),
    )

    with pytest.raises(SandboxUnavailableError, match="bwrap is unavailable"):
        await Agent(model="test").run("hello")

    assert called is False


@pytest.mark.asyncio
async def test_additional_write_roots_become_explicit_edit_grants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def stream() -> Any:
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="session",
            result="done",
            usage=None,
        )

    def fake_query(*, prompt: object, options: object) -> object:
        captured["options"] = options
        return stream()

    monkeypatch.setattr(agent_module, "query", fake_query)
    shared = tmp_path / "shared"
    settings = AgentSettings(
        permissions=AgentPermissions(allowed_tools=("WebSearch",)),
        sandbox=NativeSandboxPolicy(additional_write_roots=(shared,)),
    )

    await Agent(model="test", settings=settings).run("hello")

    assert captured["options"].allowed_tools == [
        "WebSearch",
        f"Edit(//{shared.as_posix().lstrip('/')}/**)",
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_domains": ("",)},
        {"excluded_commands": (" docker",)},
        {"additional_write_roots": (Path("/"),)},
    ],
)
def test_invalid_policy_values_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"non-empty strings|filesystem roots"):
        NativeSandboxPolicy(**kwargs)
