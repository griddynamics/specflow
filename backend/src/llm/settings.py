from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    HookMatcher,
)
from claude_agent_sdk.types import HookEvent

from llm.environment import build_agent_environment
from llm.permissions import (
    AgentPermissions,
    validate_subagent_models,
    validate_subagent_tool_grants,
)
from llm.sandbox import NativeSandboxPolicy, SandboxConfiguration, resolve_sandbox
from logger.logger import DEBUG, get_logger, log_event
from logger.payload import summarise_payload

SettingSource = Literal["user", "project", "local"]


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """Stable caller-controlled settings for one or more agent runs."""

    permissions: AgentPermissions = field(default_factory=AgentPermissions)
    fallback_model: str | None = None
    max_turns: int | None = None
    max_buffer_size: int | None = None
    max_budget_usd: float | None = None
    subagents: Mapping[str, AgentDefinition] = field(
        default_factory=dict,
        repr=False,
    )
    mcp_servers: Mapping[str, Any] | str | Path | None = field(
        default=None,
        repr=False,
    )
    strict_mcp_config: bool = False
    setting_sources: tuple[SettingSource, ...] | None = None
    hooks: Mapping[HookEvent, list[HookMatcher]] | None = field(
        default=None,
        repr=False,
    )
    sandbox: SandboxConfiguration = field(default_factory=NativeSandboxPolicy)
    extra_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: `None` (the default) does not mean "no stderr capture": `_build_sdk_options`
    #: substitutes `_log_cli_stderr_line` below, so the CLI subprocess's stderr
    #: is always captured and logged unless the caller supplies its own callback.
    stderr: Callable[[str], None] | None = field(default=None, repr=False)
    can_use_tool: Callable[..., Awaitable[Any]] | None = field(
        default=None,
        repr=False,
    )
    add_dirs: tuple[str | Path, ...] = ()
    include_partial_messages: bool = False
    include_hook_events: bool = False
    forward_subagent_text: bool = False
    output_format: Mapping[str, Any] | None = field(default=None, repr=False)
    allow_unconstrained_subagent_tools: bool = False
    allow_subagent_model_mismatch: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "subagents", dict(self.subagents))
        object.__setattr__(self, "extra_env", dict(self.extra_env))
        object.__setattr__(self, "add_dirs", tuple(self.add_dirs))
        if self.sandbox is not False and not isinstance(self.sandbox, (NativeSandboxPolicy, Mapping)):
            raise TypeError("sandbox must be NativeSandboxPolicy, SDK sandbox settings, or False")
        if self.output_format is not None:
            object.__setattr__(self, "output_format", dict(self.output_format))
        if self.setting_sources is not None:
            object.__setattr__(
                self,
                "setting_sources",
                tuple(self.setting_sources),
            )

        _validate_optional_text(self.fallback_model, "fallback_model")
        _validate_positive(self.max_turns, "max_turns")
        _validate_positive(self.max_buffer_size, "max_buffer_size")
        _validate_positive(self.max_budget_usd, "max_budget_usd")

        invalid_names = [name for name in self.subagents if not name or name != name.strip()]
        if invalid_names:
            raise ValueError(f"subagent names must be non-empty and have no surrounding whitespace: {invalid_names!r}")


def _log_cli_stderr_line(line: str) -> None:
    """Default `AgentSettings.stderr` callback: log one CLI subprocess stderr line.

    The Claude Agent SDK only pipes the CLI subprocess's stderr at all when
    `ClaudeAgentOptions.stderr` is not `None` (it calls this callback once
    per line otherwise); before this, an unset `AgentSettings.stderr` meant
    that stream was never captured anywhere, so the real diagnostic behind a
    provider/model/auth failure (e.g. `[claude-code:unrecognized_model] ...`)
    was invisible to the central logger -- see the central-logger-e2e iter0
    report. DEBUG level: diagnostic only,
    so it stays silent under the default `LOGS_LEVEL` and only surfaces when
    a caller turns on DEBUG logging for `llm.*` to investigate exactly this
    kind of gap.
    """

    log_event(
        get_logger(__name__),
        DEBUG,
        "agent.cli.stderr",
        **summarise_payload(line, prefix="line"),
    )


def _build_sdk_options(
    *,
    model: str,
    system_prompt: str | None,
    cwd: str | Path | None,
    resume_session_id: str | None,
    fallback_model: str | None,
    subagents: Mapping[str, AgentDefinition],
    settings: AgentSettings,
) -> ClaudeAgentOptions:
    validate_subagent_tool_grants(
        subagents,
        allow_unconstrained_tools=settings.allow_unconstrained_subagent_tools,
    )
    validate_subagent_models(
        model,
        subagents,
        allow_mismatch=settings.allow_subagent_model_mismatch,
    )
    sandbox, sandbox_write_rules = resolve_sandbox(settings.sandbox)
    permissions = settings.permissions
    if sandbox_write_rules:
        permissions = replace(
            permissions,
            allowed_tools=(
                *permissions.allowed_tools,
                *(rule for rule in sandbox_write_rules if rule not in permissions.allowed_tools),
            ),
        )
    permissions.validate_workspace(cwd)

    mcp_servers: Mapping[str, Any] | str | Path
    if settings.mcp_servers is None:
        mcp_servers = {}
    elif isinstance(settings.mcp_servers, Mapping):
        mcp_servers = dict(settings.mcp_servers)
    else:
        mcp_servers = settings.mcp_servers

    hooks = None if settings.hooks is None else {event: list(matchers) for event, matchers in settings.hooks.items()}

    return ClaudeAgentOptions(
        allowed_tools=list(permissions.allowed_tools),
        disallowed_tools=list(permissions.disallowed_tools),
        permission_mode=permissions.permission_mode,
        system_prompt=system_prompt,
        mcp_servers=mcp_servers,
        strict_mcp_config=settings.strict_mcp_config,
        resume=resume_session_id,
        max_turns=settings.max_turns,
        max_budget_usd=settings.max_budget_usd,
        model=model,
        fallback_model=fallback_model,
        cwd=cwd,
        add_dirs=list(settings.add_dirs),
        env=build_agent_environment(settings.extra_env),
        max_buffer_size=settings.max_buffer_size,
        stderr=(settings.stderr if settings.stderr is not None else _log_cli_stderr_line),
        can_use_tool=settings.can_use_tool,
        hooks=hooks,
        include_partial_messages=settings.include_partial_messages,
        include_hook_events=settings.include_hook_events,
        forward_subagent_text=settings.forward_subagent_text,
        output_format=(None if settings.output_format is None else dict(settings.output_format)),
        agents=dict(subagents) or None,
        setting_sources=(None if settings.setting_sources is None else list(settings.setting_sources)),
        sandbox=sandbox,
    )


def _validate_optional_text(value: str | None, field_name: str) -> None:
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _validate_positive(value: int | float | None, field_name: str) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{field_name} must be positive")
