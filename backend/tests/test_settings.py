from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AgentDefinition, HookMatcher, ResultMessage

import llm.agent as agent_module
from llm import (
    Agent,
    AgentPermissions,
    AgentSettings,
    conservative_coding_profile,
)
from llm.environment import REDACTED_ENV_VALUE
from logger.logger import get_logger
from logger_harness import events, fields


def result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session",
        result="done",
        usage=None,
    )


def capture_options(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def stream() -> Any:
        yield result_message()

    def fake_query(*, prompt: object, options: object) -> object:
        captured["prompt"] = prompt
        captured["options"] = options
        return stream()

    monkeypatch.setattr(agent_module, "query", fake_query)
    return captured


@pytest.mark.asyncio
async def test_settings_map_stable_controls_to_sdk_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "ambient-secret")
    captured = capture_options(monkeypatch)
    subagent = AgentDefinition(
        description="Review work",
        prompt="Caller-owned reviewer prompt",
        model="claude-test",
    )
    hook = HookMatcher(matcher="Read")
    sandbox = {"enabled": True}

    async def can_use_tool(*args: object) -> object:
        return object()

    stderr_lines: list[str] = []
    stderr_callback = stderr_lines.append
    output_format = {
        "type": "json_schema",
        "schema": {"type": "object"},
    }
    settings = AgentSettings(
        permissions=AgentPermissions(
            allowed_tools=("Read(//workspace/**)", "WebSearch"),
            disallowed_tools=("Bash(rm:*)",),
            permission_mode="acceptEdits",
        ),
        fallback_model="claude-fallback",
        max_turns=20,
        max_buffer_size=4096,
        max_budget_usd=2.5,
        subagents={"reviewer": subagent},
        mcp_servers={
            "docs": {
                "type": "http",
                "url": "https://mcp.example",
            }
        },
        strict_mcp_config=True,
        setting_sources=("project", "local"),
        hooks={"PreToolUse": [hook]},
        sandbox=sandbox,
        extra_env={
            "ANTHROPIC_API_KEY": "extra-key",
            "ANTHROPIC_AUTH_TOKEN": "gateway-token",
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            "EXTRA_VALUE": "caller-value",
        },
        stderr=stderr_callback,
        can_use_tool=can_use_tool,
        add_dirs=(tmp_path / "shared",),
        include_partial_messages=True,
        include_hook_events=True,
        forward_subagent_text=True,
        output_format=output_format,
    )

    await Agent(
        model="claude-test",
        system_prompt="Caller-owned system prompt",
        cwd=tmp_path,
        settings=settings,
    ).run("hello", resume_session_id="resume")

    options = captured["options"]
    assert options.model == "claude-test"
    assert options.system_prompt == "Caller-owned system prompt"
    assert options.cwd == tmp_path
    assert options.resume == "resume"
    assert options.allowed_tools == ["Read(//workspace/**)", "WebSearch"]
    assert options.disallowed_tools == ["Bash(rm:*)"]
    assert options.permission_mode == "acceptEdits"
    assert options.fallback_model == "claude-fallback"
    assert options.max_turns == 20
    assert options.max_buffer_size == 4096
    assert options.max_budget_usd == 2.5
    assert options.agents == {"reviewer": subagent}
    assert options.mcp_servers["docs"]["url"] == "https://mcp.example"
    assert options.strict_mcp_config is True
    assert options.setting_sources == ["project", "local"]
    assert options.hooks == {"PreToolUse": [hook]}
    assert options.sandbox == {
        "enabled": True,
        "allowUnsandboxedCommands": False,
    }
    assert options.stderr is stderr_callback
    assert options.can_use_tool is can_use_tool
    assert options.add_dirs == [tmp_path / "shared"]
    assert options.include_partial_messages is True
    assert options.include_hook_events is True
    assert options.forward_subagent_text is True
    assert options.output_format == output_format
    assert options.output_format is not output_format
    assert options.env["ANTHROPIC_API_KEY"] == "extra-key"
    assert options.env.get("ANTHROPIC_AUTH_TOKEN") == "gateway-token"
    assert options.env["ANTHROPIC_BASE_URL"] == ("https://gateway.example/anthropic")
    assert options.env["EXTRA_VALUE"] == "caller-value"
    assert options.env["UNRELATED_SECRET"] == REDACTED_ENV_VALUE


@pytest.mark.asyncio
async def test_stderr_defaults_to_logging_the_cli_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression test for the E2E-found gap (central-logger-e2e iter0/1).

    When the caller leaves `AgentSettings.stderr` at its default (`None`),
    `_build_sdk_options` must still supply the SDK a real callback -- the
    SDK only pipes the CLI subprocess's stderr at all when `options.stderr`
    is not `None` -- so that each captured line is logged via a DEBUG
    `agent.cli.stderr` event rather than being silently discarded (or, per
    the SDK's own behavior, not even piped).
    """

    captured = capture_options(monkeypatch)
    # Trigger the central-logger bootstrap (which pins "llm"'s level once)
    # before raising caplog's captured level, matching the established
    # pattern in tests/test_tracing_logging_sink.py: otherwise the first
    # bootstrap triggered later -- when `options.stderr(...)` below calls
    # `get_logger` for the first time -- would clobber caplog's DEBUG
    # override back down to `cfg.level` (INFO).
    get_logger("llm.settings")
    caplog.set_level(logging.DEBUG, logger="llm")

    await Agent(model="claude-test").run("hello")

    options = captured["options"]
    assert options.stderr is not None

    options.stderr('[claude-code:unrecognized_model] {"model":"bad-model"}')

    records = events(caplog, "agent.cli.stderr")
    assert len(records) == 1
    assert records[0].levelname == "DEBUG"
    record_fields = fields(records[0])
    # Summary only: the raw line itself is never logged.
    assert "line" not in record_fields
    assert record_fields["line_preview"] == '[claude-code:unrecognized_model] {"model":"bad-model"}'


@pytest.mark.asyncio
async def test_explicit_stderr_callback_is_not_overridden_by_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)
    lines: list[str] = []

    await Agent(
        model="claude-test",
        settings=AgentSettings(stderr=lines.append),
    ).run("hello")

    options = captured["options"]
    options.stderr("caller-owned line")

    assert lines == ["caller-owned line"]


@pytest.mark.asyncio
async def test_fallback_model_is_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)

    await Agent(
        model="anthropic/claude-sonnet-4.6",
        settings=AgentSettings(
            fallback_model="anthropic/claude-haiku-4.5",
        ),
    ).run("hello")

    options = captured["options"]
    assert options.fallback_model == "anthropic/claude-haiku-4.5"


@pytest.mark.asyncio
async def test_run_output_format_overrides_agent_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)
    default_format = {"type": "json_schema", "schema": {"title": "default"}}
    run_format = {"type": "json_schema", "schema": {"title": "run"}}
    agent = Agent(
        model="claude-test",
        settings=AgentSettings(output_format=default_format),
    )

    await agent.run("hello", output_format=run_format)

    assert captured["options"].output_format == run_format
    assert agent.settings.output_format == default_format


@pytest.mark.asyncio
async def test_subagent_unconstrained_tools_are_rejected_before_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)
    settings = AgentSettings(
        subagents={
            "unsafe": AgentDefinition(
                description="Run commands",
                prompt="Work",
                tools=["Bash", "Write"],
            )
        }
    )

    with pytest.raises(
        ValueError,
        match=r"subagent 'unsafe' tools are unsafe: Bash, Write",
    ):
        await Agent(model="parent-model", settings=settings).run("hello")

    assert captured == {}


@pytest.mark.asyncio
async def test_subagent_unconstrained_tools_require_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)
    subagent = AgentDefinition(
        description="Run commands",
        prompt="Work",
        tools=["Bash"],
    )
    settings = AgentSettings(
        subagents={"explicit": subagent},
        allow_unconstrained_subagent_tools=True,
    )

    await Agent(model="parent-model", settings=settings).run("hello")

    assert captured["options"].agents == {"explicit": subagent}


@pytest.mark.asyncio
async def test_coding_profile_does_not_relax_subagent_tool_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured = capture_options(monkeypatch)
    settings = AgentSettings(
        permissions=conservative_coding_profile(tmp_path),
        subagents={
            "unsafe": AgentDefinition(
                description="Run commands",
                prompt="Work",
                tools=["Read", "Write", "Bash"],
            )
        },
    )

    with pytest.raises(
        ValueError,
        match=r"subagent 'unsafe' tools are unsafe: Bash, Read, Write",
    ):
        await Agent(
            model="parent-model",
            cwd=tmp_path,
            settings=settings,
        ).run("hello")

    assert captured == {}


@pytest.mark.asyncio
async def test_parent_unconstrained_opt_in_does_not_apply_to_subagents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)
    settings = AgentSettings(
        permissions=AgentPermissions(allow_unconstrained_tools=True),
        subagents={
            "unsafe": AgentDefinition(
                description="Run commands",
                prompt="Work",
                tools=["Bash"],
            )
        },
    )

    with pytest.raises(
        ValueError,
        match="allow_unconstrained_subagent_tools=True",
    ):
        await Agent(model="parent-model", settings=settings).run("hello")

    assert captured == {}


@pytest.mark.asyncio
async def test_model_mismatch_is_rejected_before_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)
    settings = AgentSettings(
        subagents={
            "reviewer": AgentDefinition(
                description="Review work",
                prompt="Review",
                model="different-model",
            )
        }
    )

    with pytest.raises(ValueError, match="Subagent models must inherit"):
        await Agent(model="parent-model", settings=settings).run("hello")

    assert captured == {}


@pytest.mark.asyncio
async def test_model_mismatch_requires_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = capture_options(monkeypatch)
    subagent = AgentDefinition(
        description="Review work",
        prompt="Review",
        model="different-model",
    )

    await Agent(
        model="parent-model",
        settings=AgentSettings(
            subagents={"reviewer": subagent},
            allow_subagent_model_mismatch=True,
        ),
    ).run("hello")

    assert captured["options"].agents == {"reviewer": subagent}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"fallback_model": ""}, "fallback_model must not be empty"),
        ({"max_turns": 0}, "max_turns must be positive"),
        ({"max_buffer_size": -1}, "max_buffer_size must be positive"),
        ({"max_budget_usd": 0}, "max_budget_usd must be positive"),
        (
            {"subagents": {" ": AgentDefinition(description="d", prompt="p")}},
            "subagent names must be non-empty",
        ),
    ],
)
def test_invalid_settings_are_rejected(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        AgentSettings(**kwargs)


def test_sensitive_settings_are_hidden_from_repr() -> None:
    settings = AgentSettings(
        subagents={
            "reviewer": AgentDefinition(
                description="d",
                prompt="private prompt body",
            )
        },
        mcp_servers={
            "private": {
                "type": "http",
                "url": "https://example.test",
                "headers": {"Authorization": "secret-header"},
            }
        },
        extra_env={"API_KEY": "secret-key"},
    )

    rendered = repr(settings)
    assert "private prompt body" not in rendered
    assert "secret-header" not in rendered
    assert "secret-key" not in rendered
