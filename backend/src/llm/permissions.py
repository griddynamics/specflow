from __future__ import annotations

import math
import re
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from claude_agent_sdk import (
    AgentDefinition,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
)
from claude_agent_sdk.types import HookEvent

PermissionMode = Literal[
    "default",
    "acceptEdits",
    "plan",
    "bypassPermissions",
    "dontAsk",
    "auto",
]

_UNSAFE_BARE_TOOLS = frozenset({"Bash", "Edit", "Glob", "Read", "Write"})
_UNSUPPORTED_SCOPED_TOOLS = ("Write(",)
_UNSAFE_PATH_CHARACTERS = frozenset("\n\r()[]*?!")
_MAX_SHELL_COMMAND_LENGTH = 65_536
_MAX_NESTED_SHELL_DEPTH = 8
_MAX_SHELL_TOKENS = 512
_UNSUPPORTED_SHELL_MARKERS = ("\x00", "\n", "\r", "`", "$(", "<<", "<(", ">(")
_UNSUPPORTED_SHELL_EXECUTABLES = frozenset(
    {
        "!",
        "[",
        "[[",
        "{",
        "}",
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "eval",
        "fi",
        "for",
        "function",
        "if",
        "select",
        "then",
        "until",
    }
)
_SERVER_EXECUTABLES = frozenset(
    {
        "daphne",
        "gunicorn",
        "http-server",
        "hypercorn",
        "nodemon",
        "uvicorn",
    }
)
_FRAMEWORK_EXECUTABLES = frozenset({"next", "remix", "vite"})
_FRAMEWORK_COMMANDS = frozenset({"dev", "preview", "serve", "start"})
_WATCH_CAPABLE_EXECUTABLES = frozenset(
    {
        "esbuild",
        "jest",
        "node",
        "npm",
        "npx",
        "parcel",
        "pytest",
        "rollup",
        "tsc",
        "vite",
        "vitest",
        "webpack",
        "yarn",
        "pnpm",
    }
)
_SPAWN_PATTERN = re.compile(
    r"(?:subprocess\.|from\s+subprocess\s+import\s+"
    r"(?:call|check_call|check_output|Popen|run)|os\.(?:exec|popen|system)|"
    r"pty\.spawn|child_process|execSync|spawnSync)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class AgentPermissions:
    """Explicit SDK tool grants and permission mode."""

    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    permission_mode: PermissionMode | None = None
    allow_unconstrained_tools: bool = False
    workspace_root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(self, "disallowed_tools", tuple(self.disallowed_tools))
        if self.workspace_root is not None:
            object.__setattr__(
                self,
                "workspace_root",
                Path(self.workspace_root).expanduser().resolve(),
            )
        validate_tool_grants(
            self.allowed_tools,
            field_name="allowed_tools",
            allow_unconstrained_tools=self.allow_unconstrained_tools,
        )
        _validate_tool_names(self.disallowed_tools, "disallowed_tools")
        _validate_supported_tool_rules(
            self.disallowed_tools,
            "disallowed_tools",
        )

    def validate_workspace(self, cwd: str | Path | None) -> None:
        """Require execution cwd to match this permission scope when present."""

        if self.workspace_root is None:
            return
        if cwd is None:
            raise ValueError("cwd is required when permissions are scoped to a workspace")
        actual = Path(cwd).expanduser().resolve()
        if actual != self.workspace_root:
            raise ValueError(f"cwd {actual!s} does not match permissions workspace {self.workspace_root!s}")


def conservative_coding_profile(
    cwd: str | Path,
    *,
    bash_tools: Iterable[str] = (),
) -> AgentPermissions:
    """Return an explicit coding profile with file access scoped to cwd."""

    if isinstance(cwd, str) and not cwd.strip():
        raise ValueError("cwd must not be empty")
    workspace = Path(cwd).expanduser().resolve()
    if workspace == Path(workspace.anchor):
        raise ValueError("cwd must not be the filesystem root")
    unsupported = sorted({character for character in str(workspace) if character in _UNSAFE_PATH_CHARACTERS})
    if unsupported:
        raise ValueError(
            "cwd contains characters unsupported by SDK permission rules: "
            + ", ".join(repr(character) for character in unsupported)
        )
    path_pattern = f"//{workspace.as_posix().lstrip('/')}/**"
    return AgentPermissions(
        allowed_tools=(
            f"Read({path_pattern})",
            f"Edit({path_pattern})",
            f"Glob({path_pattern})",
            *tuple(bash_tools),
        ),
        permission_mode="dontAsk",
        workspace_root=workspace,
    )


def check_bash_command(command: str) -> tuple[bool, str | None]:
    """Return whether the optional conservative Bash guard blocks a command."""

    return _check_bash_command(command, depth=0)


type _Verdict = tuple[bool, str | None]

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SEGMENT_SEPARATORS = frozenset({"&&", "||", ";", "|", "(", ")"})
_SHELL_EXECUTABLES = frozenset({"bash", "sh", "zsh"})
_VITE_SAFE_ARGUMENTS = frozenset({"--help", "--version", "build", "optimize"})
_HELP_ARGUMENTS = frozenset({"--help", "--version"})
_DEV_SERVER_REASON = "Development servers and watch tasks do not exit."
_DEV_SERVER_SELF_REASON = "Development servers do not exit on their own."
_MODULE_FLAG = "-m"


def _blocked(reason: str) -> _Verdict:
    return True, reason


def _screen_command(command: str, depth: int) -> _Verdict | None:
    """Reject commands that cannot be inspected safely before they are tokenized."""

    if not command.strip():
        return _blocked("The shell command must not be empty.")
    if len(command) > _MAX_SHELL_COMMAND_LENGTH:
        return _blocked("The shell command is too long to inspect safely.")
    if depth > _MAX_NESTED_SHELL_DEPTH:
        return _blocked("The shell command has too many nested wrappers.")
    if any(marker in command for marker in _UNSUPPORTED_SHELL_MARKERS):
        return _blocked("Multiline, substitution, and heredoc shell syntax is not supported.")
    return None


def _tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _split_segments(tokens: list[str]) -> list[list[str]] | None:
    """Split tokens at shell separators; ``None`` when a background ``&`` is present."""

    segments: list[list[str]] = [[]]
    for word in tokens:
        if word in _SEGMENT_SEPARATORS:
            segments.append([])
            continue
        if word == "&":
            return None
        segments[-1].append(word)
    return segments


def _check_bash_command(
    command: str,
    *,
    depth: int,
) -> _Verdict:
    screened = _screen_command(command, depth)
    if screened is not None:
        return screened
    try:
        tokens = _tokenize(command)
    except ValueError:
        return _blocked("The shell command could not be parsed safely.")
    if len(tokens) > _MAX_SHELL_TOKENS:
        return _blocked("The shell command is too complex to inspect safely.")

    segments = _split_segments(tokens)
    if segments is None:
        return _blocked("Background commands outlive the agent tool call.")

    for segment in segments:
        blocked, reason = _check_shell_segment(segment, depth=depth)
        if blocked:
            return blocked, reason
    return False, None


@dataclass(frozen=True, slots=True)
class _Segment:
    """One shell command after wrapper removal."""

    executable: str
    arguments: list[str]
    lowered: list[str]
    depth: int


def _check_shell_segment(
    tokens: list[str],
    *,
    depth: int,
) -> _Verdict:
    tokens = _unwrap_command(tokens)
    if not tokens:
        return False, None

    segment = _Segment(
        executable=Path(tokens[0]).name.lower(),
        arguments=tokens[1:],
        lowered=[argument.lower() for argument in tokens[1:]],
        depth=depth,
    )
    for rule in _SEGMENT_RULES:
        verdict = rule(segment)
        if verdict is not None:
            return verdict
    return False, None


def _rule_compound_shell(segment: _Segment) -> _Verdict | None:
    if segment.executable in _UNSUPPORTED_SHELL_EXECUTABLES:
        return _blocked("Compound shell syntax is not supported by this guard.")
    if segment.executable in {"nohup", "disown"}:
        return _blocked("Detached commands outlive the agent tool call.")
    if segment.executable == "while" and segment.lowered and segment.lowered[0] in {"true", ":"}:
        return _blocked("Infinite shell loops do not exit on their own.")
    return None


def _rule_follow_and_watch_commands(segment: _Segment) -> _Verdict | None:
    if segment.executable == "tail" and any(
        argument == "--follow"
        or argument.startswith("--follow=")
        or (argument.startswith("-") and not argument.startswith("--") and "f" in argument[1:])
        for argument in segment.lowered
    ):
        return _blocked("Follow commands do not exit on their own.")
    if segment.executable == "gh" and segment.lowered[:2] == ["run", "watch"]:
        return _blocked("Workflow watch commands do not exit reliably.")
    return None


def _lowered_package_arguments(segment: _Segment) -> list[str]:
    package_arguments = _strip_leading_options(
        segment.arguments,
        options_with_values={
            "--cache",
            "--prefix",
            "--userconfig",
            "--workspace",
            "-w",
        },
    )
    return [argument.lower() for argument in package_arguments]


def _rule_package_managers(segment: _Segment) -> _Verdict | None:
    lowered_npm = _lowered_package_arguments(segment)
    if segment.executable == "npm" and (
        lowered_npm[:1] in (["start"], ["serve"])
        or (lowered_npm[:1] == ["run"] and len(lowered_npm) > 1 and lowered_npm[1] in _FRAMEWORK_COMMANDS | {"watch"})
    ):
        return _blocked(_DEV_SERVER_REASON)
    if segment.executable in {"yarn", "pnpm"}:
        package_arguments = lowered_npm[1:] if lowered_npm[:1] == ["run"] else lowered_npm
        if package_arguments[:1] and package_arguments[0] in (_FRAMEWORK_COMMANDS | {"watch"}):
            return _blocked(_DEV_SERVER_REASON)
    return None


def _rule_npx(segment: _Segment) -> _Verdict | None:
    if segment.executable != "npx":
        return None
    npx_arguments = _strip_leading_options(
        segment.arguments,
        options_with_values={"-p", "--package"},
    )
    lowered_npx = [argument.lower() for argument in npx_arguments]
    if not lowered_npx:
        return False, None
    reason = _npx_target_reason(Path(lowered_npx[0]).name, lowered_npx[1:])
    return _blocked(reason) if reason is not None else None


def _npx_target_reason(nested: str, nested_arguments: list[str]) -> str | None:
    if nested in _SERVER_EXECUTABLES:
        return _DEV_SERVER_REASON
    if nested == "serve" and not any(argument in _HELP_ARGUMENTS for argument in nested_arguments):
        return _DEV_SERVER_REASON
    if nested == "vite" and (not nested_arguments or nested_arguments[0] not in _VITE_SAFE_ARGUMENTS):
        return _DEV_SERVER_SELF_REASON
    if (
        nested in _FRAMEWORK_EXECUTABLES - {"vite"}
        and nested_arguments[:1]
        and nested_arguments[0] in _FRAMEWORK_COMMANDS
    ):
        return _DEV_SERVER_SELF_REASON
    return None


def _rule_dev_servers(segment: _Segment) -> _Verdict | None:
    executable = segment.executable
    lowered = segment.lowered
    if executable == "vite" and (not lowered or lowered[0] not in _VITE_SAFE_ARGUMENTS):
        return _blocked("Framework development servers do not exit.")
    if executable in _FRAMEWORK_EXECUTABLES - {"vite"} and lowered[:1] and lowered[0] in _FRAMEWORK_COMMANDS:
        return _blocked("Framework development servers do not exit.")
    if executable == "ng" and lowered[:1] == ["serve"]:
        return _blocked(_DEV_SERVER_SELF_REASON)
    return _rule_plain_servers(segment)


def _rule_plain_servers(segment: _Segment) -> _Verdict | None:
    executable = segment.executable
    if executable == "serve" and not any(argument in _HELP_ARGUMENTS for argument in segment.lowered):
        return _blocked(_DEV_SERVER_SELF_REASON)
    if executable in _SERVER_EXECUTABLES:
        return _blocked("Server and watcher commands do not exit on their own.")
    if executable == "flask" and "run" in segment.lowered:
        return _blocked(_DEV_SERVER_SELF_REASON)
    return None


def _rule_interpreter(segment: _Segment) -> _Verdict | None:
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", segment.executable) or segment.executable == "node":
        blocked, reason = _check_interpreter(segment.executable, segment.arguments)
        if blocked:
            return blocked, reason
    return None


def _rule_nested_shell(segment: _Segment) -> _Verdict | None:
    if segment.executable in _SHELL_EXECUTABLES:
        nested_command = _shell_command_argument(segment.arguments)
        if nested_command is not None:
            return _check_bash_command(nested_command, depth=segment.depth + 1)
    return None


def _rule_build_watchers(segment: _Segment) -> _Verdict | None:
    executable = segment.executable
    lowered = segment.lowered
    if executable == "tsc" and any(
        argument == "--watch" or (argument.startswith("-") and not argument.startswith("--") and "w" in argument[1:])
        for argument in lowered
    ):
        return _blocked("TypeScript watch mode does not exit on its own.")
    if executable in {"gradle", "gradlew"} and any(argument in {"--continuous", "-t"} for argument in lowered):
        return _blocked("Gradle continuous mode does not exit on its own.")
    if executable in _WATCH_CAPABLE_EXECUTABLES and _has_option(
        lowered,
        "--watch",
        "--watchall",
    ):
        return _blocked("Watch modes do not exit on their own.")
    return None


_SEGMENT_RULES: tuple[Callable[[_Segment], _Verdict | None], ...] = (
    _rule_compound_shell,
    _rule_follow_and_watch_commands,
    _rule_package_managers,
    _rule_npx,
    _rule_dev_servers,
    _rule_interpreter,
    _rule_nested_shell,
    _rule_build_watchers,
)


def _skip_assignments(tokens: list[str], index: int) -> int:
    while index < len(tokens) and _ASSIGNMENT.fullmatch(tokens[index]):
        index += 1
    return index


def _skip_wrapper(tokens: list[str], index: int) -> int | None:
    """Return the index after the wrapper at ``index``, or ``None`` when it is the real executable."""

    executable = Path(tokens[index]).name.lower()
    if executable == "env":
        index += 1
        while index < len(tokens) and (tokens[index].startswith("-") or _ASSIGNMENT.fullmatch(tokens[index])):
            index += 1
        return index
    if executable in {"command", "exec", "time"}:
        index += 1
        while index < len(tokens) and (tokens[index] == "--" or tokens[index].startswith("-")):
            index += 1
        return index
    if executable in {"poetry", "uv"}:
        run_index = index + 1
        while run_index < len(tokens) and tokens[run_index].startswith("-"):
            run_index += 1
        if run_index < len(tokens) and tokens[run_index].lower() == "run":
            return run_index + 1
    if (
        executable in _SHELL_EXECUTABLES
        and index + 1 < len(tokens)
        and Path(tokens[index + 1]).name.lower() == "gradlew"
    ):
        return index + 1
    return None


def _unwrap_command(tokens: list[str]) -> list[str]:
    index = 0
    while index < len(tokens):
        index = _skip_assignments(tokens, index)
        if index >= len(tokens):
            return []
        advanced = _skip_wrapper(tokens, index)
        if advanced is None:
            return tokens[index:]
        index = advanced
    return []


def _strip_leading_options(
    arguments: list[str],
    *,
    options_with_values: set[str] | None = None,
) -> list[str]:
    options_with_values = options_with_values or set()
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        option = argument.split("=", 1)[0]
        if not argument.startswith("-"):
            break
        index += 2 if option in options_with_values and "=" not in argument else 1
    return arguments[index:]


def _has_option(arguments: list[str], *options: str) -> bool:
    return any(argument == option or argument.startswith(f"{option}=") for argument in arguments for option in options)


def _shell_command_argument(arguments: list[str]) -> str | None:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            continue
        if argument in {"-c", "--command"} or (
            argument.startswith("-") and not argument.startswith("--") and "c" in argument[1:]
        ):
            index += 1
            if index < len(arguments) and arguments[index] == "--":
                index += 1
            return arguments[index] if index < len(arguments) else ""
        index += 1
    return None


def _launches_python_server(lowered: list[str]) -> bool:
    if lowered[:2] == ["manage.py", "runserver"]:
        return True
    for index, argument in enumerate(lowered):
        module = None
        if argument == _MODULE_FLAG and index + 1 < len(lowered):
            module = lowered[index + 1]
        elif argument.startswith(_MODULE_FLAG) and len(argument) > len(_MODULE_FLAG):
            module = argument[len(_MODULE_FLAG) :]
        if module in _SERVER_EXECUTABLES | {"http.server"}:
            return True
    return False


def _inline_code(
    arguments: list[str],
    lowered: list[str],
    index: int,
    eval_flags: set[str],
) -> str | None:
    argument = lowered[index]
    if argument in eval_flags and index + 1 < len(arguments):
        return arguments[index + 1]
    for flag in eval_flags:
        if argument.startswith(f"{flag}="):
            return arguments[index][len(flag) + 1 :]
        if (
            flag.startswith("-")
            and not flag.startswith("--")
            and argument.startswith(flag)
            and len(argument) > len(flag)
        ):
            return arguments[index][len(flag) :]
    return None


def _check_interpreter(
    executable: str,
    arguments: list[str],
) -> _Verdict:
    lowered = [argument.lower() for argument in arguments]
    if executable.startswith("python"):
        if _launches_python_server(lowered):
            return _blocked(_DEV_SERVER_SELF_REASON)
        eval_flags = {"-c"}
    else:
        eval_flags = {"-e", "-p", "--eval", "--print"}

    for index in range(len(lowered)):
        code = _inline_code(arguments, lowered, index, eval_flags)
        if code is not None and _SPAWN_PATTERN.search(code):
            return _blocked("Inline interpreter code must not launch external programs.")
    return False, None


async def _conservative_bash_guard(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> HookJSONOutput:
    del tool_use_id, context
    if input_data.get("hook_event_name") != "PreToolUse":
        return {}
    if input_data.get("tool_name") != "Bash":
        return {}

    tool_input = input_data.get("tool_input", {})
    if not isinstance(tool_input, Mapping):
        return _bash_guard_denial("Bash tool input must be an object.")
    command: Any = tool_input.get("command")
    if not isinstance(command, str):
        return _bash_guard_denial("Bash tool input must contain a command string.")

    blocked, reason = check_bash_command(command)
    if not blocked:
        return {}
    return _bash_guard_denial(reason or "The Bash command was denied.")


def _bash_guard_denial(reason: str) -> HookJSONOutput:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def conservative_bash_guard_hooks() -> dict[HookEvent, list[HookMatcher]]:
    """Return caller-selectable hooks for the conservative Bash guard."""

    return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_conservative_bash_guard])]}


def conservative_bash_timeout_environment(
    *,
    default_seconds: int = 600,
    maximum_seconds: int = 1800,
    run_timeout_seconds: float | None = 3600.0,
) -> dict[str, str]:
    """Return validated SDK Bash timeout environment values."""

    checked_values: tuple[tuple[object, str], ...] = (
        (default_seconds, "default_seconds"),
        (maximum_seconds, "maximum_seconds"),
    )
    for value, name in checked_values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if default_seconds > maximum_seconds:
        raise ValueError("default_seconds must not exceed maximum_seconds")
    if run_timeout_seconds is not None:
        run_timeout: object = run_timeout_seconds
        if (
            isinstance(run_timeout, bool)
            or not isinstance(run_timeout, (int, float))
            or not math.isfinite(run_timeout)
            or run_timeout <= 0
        ):
            raise ValueError("run_timeout_seconds must be positive")
        if maximum_seconds >= run_timeout:
            raise ValueError("maximum_seconds must be less than run_timeout_seconds")
    return {
        "BASH_DEFAULT_TIMEOUT_MS": str(default_seconds * 1000),
        "BASH_MAX_TIMEOUT_MS": str(maximum_seconds * 1000),
    }


def validate_subagent_tool_grants(
    subagents: Mapping[str, AgentDefinition],
    *,
    allow_unconstrained_tools: bool,
) -> None:
    """Apply parent tool-grant safety rules to every subagent."""

    for name, subagent in subagents.items():
        validate_tool_grants(
            tuple(subagent.tools or ()),
            field_name=f"subagent {name!r} tools",
            allow_unconstrained_tools=allow_unconstrained_tools,
        )


def validate_tool_grants(
    tools: tuple[str, ...],
    *,
    field_name: str,
    allow_unconstrained_tools: bool,
) -> None:
    """Reject bare file and shell tool grants unless explicitly allowed."""

    _validate_tool_names(tools, field_name)
    _validate_supported_tool_rules(tools, field_name)
    unsafe = sorted(_UNSAFE_BARE_TOOLS.intersection(tools))
    if not unsafe or allow_unconstrained_tools:
        return

    location = "" if field_name == "allowed_tools" else f" in {field_name}"
    joined = ", ".join(unsafe)
    override = (
        "allow_unconstrained_tools=True" if field_name == "allowed_tools" else "allow_unconstrained_subagent_tools=True"
    )
    raise ValueError(
        f"Unconstrained tool grants{location} are unsafe: {joined}. "
        "Use scoped grants such as 'Read(//workspace/**)' or "
        f"'Bash(git status:*)', or explicitly set {override}."
    )


def _validate_supported_tool_rules(
    tools: tuple[str, ...],
    field_name: str,
) -> None:
    unsupported = [tool for tool in tools if tool.startswith(_UNSUPPORTED_SCOPED_TOOLS)]
    if unsupported:
        raise ValueError(
            f"{field_name} contains unsupported scoped rules: {unsupported!r}. "
            "Use Edit(path) to govern both Edit and Write operations."
        )


def validate_subagent_models(
    parent_model: str,
    subagents: Mapping[str, AgentDefinition],
    *,
    allow_mismatch: bool,
) -> None:
    """Require subagents to inherit or match the parent model by default."""

    mismatches = {
        name: subagent.model
        for name, subagent in subagents.items()
        if subagent.model not in (None, "inherit", parent_model)
    }
    if not mismatches or allow_mismatch:
        return

    details = ", ".join(f"{name}={model!r}" for name, model in sorted(mismatches.items()))
    raise ValueError(
        f"Subagent models must inherit or match parent model {parent_model!r}; "
        f"mismatches: {details}. Use model='inherit', use the parent model, or "
        "explicitly set allow_subagent_model_mismatch=True."
    )


def _validate_tool_names(tools: tuple[str, ...], field_name: str) -> None:
    invalid = [tool for tool in tools if not tool or tool != tool.strip()]
    if invalid:
        raise ValueError(f"{field_name} entries must be non-empty and have no surrounding whitespace: {invalid!r}")
