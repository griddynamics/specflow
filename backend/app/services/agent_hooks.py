import logging
import re
import shlex
from pathlib import Path
from typing import Any, List, Pattern, Tuple

from claude_agent_sdk import (
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PreToolUseHookInput,
)
from claude_agent_sdk.types import HookEvent


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blocklist
# ---------------------------------------------------------------------------
# Each entry is (pattern, human-readable reason returned to the agent).
# Patterns are matched against the raw `command` string of a Bash tool call,
# case-insensitively, with re.search (so the offending verb anywhere in a
# compound command — `cd app && npm start` — still trips the rule).
#
# Keep these focused on commands whose *purpose is to stay running*.
# Builds, installs, tests, and one-shot scripts must not be matched here.
#
# The blocklist is assembled from per-tool helpers below so each family can be
# reviewed, extended, and tested independently.


def _npm_blocklist() -> List[Tuple[str, str]]:
    """npm / yarn / pnpm / npx commands that launch dev servers or watchers."""
    return [
        (
            r"\bnpm\s+(?:start|serve)\b",
            "`npm start` / `npm serve` runs a dev server that never exits. "
            "Use `npm run build` to verify build output, or `npm test -- --run` to run tests once.",
        ),
        (
            r"\bnpm\s+run\s+(?:dev|serve|start|watch)\b",
            "`npm run dev|serve|start|watch` runs a long-lived server/watcher. "
            "Use `npm run build` or `npm test -- --run` instead.",
        ),
        (
            r"\b(?:yarn|pnpm)\s+(?:start|dev|serve|watch)\b",
            "yarn/pnpm dev|start|serve|watch runs a long-lived process. "
            "Use the equivalent build/test script instead.",
        ),
        (
            r"\bnpx\s+(?:http-server|serve|next\s+dev|next\s+start|vite|nodemon)\b",
            "This npx command launches a long-lived server/watcher. Use a one-shot equivalent.",
        ),
    ]


def _framework_blocklist() -> List[Tuple[str, str]]:
    """Framework dev/serve/watch commands invoked directly (no npm wrapper).

    ``nodemon`` is anchored to command-verb position (start of string or after
    a shell separator) so dependency installs like ``npm install -D nodemon``
    are not falsely flagged.
    """
    return [
        (
            r"\b(?:next|vite|remix)\s+(?:dev|start|serve|preview)\b",
            "Framework dev/serve commands stay running. Use the framework's `build` command instead.",
        ),
        (r"\bng\s+serve\b", "`ng serve` runs an Angular dev server. Use `ng build` instead."),
        (
            r"(?:^|[;&|]\s*)nodemon\b",
            "`nodemon` watches for changes and never exits. Run the target script directly.",
        ),
    ]


def _py_blocklist() -> List[Tuple[str, str]]:
    """Python dev/web servers.

    The ASGI/WSGI server-name rule is anchored to command-verb position so
    ``pip install uvicorn`` / ``poetry add gunicorn`` / ``uv pip install
    hypercorn`` and other dependency installs are not falsely flagged.
    """
    return [
        (r"\bflask\s+run\b", "`flask run` is a long-lived dev server."),
        (
            r"\bpython\s+manage\.py\s+runserver\b",
            "`manage.py runserver` is a long-lived dev server.",
        ),
        (
            r"\bpython\s+-m\s+http\.server\b",
            "`python -m http.server` is a long-lived static server.",
        ),
        (
            r"(?:^|[;&|]\s*)(?:uvicorn|gunicorn|hypercorn|daphne)\b",
            "ASGI/WSGI servers run forever. Don't start them from agent commands.",
        ),
    ]


def _node_static_blocklist() -> List[Tuple[str, str]]:
    """Stand-alone Node static servers.

    Anchored to command-verb position (start of string or after a shell
    separator: ``;``, ``&&``, ``||``, ``|``) so non-command occurrences are
    not falsely flagged:
      - ``node_modules/.bin/serve`` (local binary path)
      - ``vite build --base=/serve/`` (URL base path)
      - ``--set serve.enabled=true`` (Helm-style value)

    If a local-bin path is actually used to launch a server, the universal
    10 min Bash timeout catches it.
    """
    return [
        (
            r"(?:^|[;&|]\s*)http-server\b",
            "`http-server` is a long-lived static server.",
        ),
        (
            r"(?:^|[;&|]\s*)serve\b(?!\s*--help)",
            "`serve` is a long-lived static server.",
        ),
    ]


def _watch_flag_blocklist() -> List[Tuple[str, str]]:
    """Watch / hot-reload flags on otherwise-allowed commands.

    A generic ``-w`` rule was removed because it produced false positives
    against ``grep -w`` (whole-word), ``curl -w`` (write-out format), and
    ``python -w`` (warning control). ``--watch`` / ``--watchAll`` are still
    blocked universally; ``-w`` is narrowed to ``tsc`` only.
    """
    return [
        (
            r"--watch(?:All)?\b",
            "Watch modes run forever. Drop the `--watch` flag so the command exits when it's done.",
        ),
        (
            r"\btsc\s+(?:[^|;&]*\s)?-[a-zA-Z]*w\b",
            "`tsc -w` / `tsc --watch` runs forever. Use `tsc --noEmit` instead.",
        ),
    ]


def _gradle_blocklist() -> List[Tuple[str, str]]:
    """Gradle flags that intentionally keep runs alive forever."""
    gradle_prefix = r"(?:\./gradlew\b|gradlew\b|gradle\b|sh\s+\./?gradlew\b|bash\s+\./?gradlew\b)"
    return [
        (
            rf"{gradle_prefix}[^\n]*\s--continuous\b",
            "Gradle `--continuous` keeps watching for changes and does not exit. "
            "Use one-shot Gradle commands for unattended runs.",
        ),
        (
            rf"{gradle_prefix}[^\n]*\s-t\b",
            "Gradle `-t` (continuous mode) does not exit. "
            "Use one-shot Gradle commands for unattended runs.",
        ),
    ]


def _android_sdk_blocklist() -> List[Tuple[str, str]]:
    """Android SDK package-management commands owned by operators, not agents."""
    return [
        (
            r"(?:^|[;&|]\s*)(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:\S*/)?sdkmanager\b",
            "`sdkmanager` is operator-only. The shared Android SDK is provisioned once "
            "with `init-mobile-sdk.sh`; agents must not install SDK packages into a workspace.",
        ),
    ]


def _shell_blocklist() -> List[Tuple[str, str]]:
    """Generic shell constructs that detach, follow, or loop forever."""
    return [
        (
            r"&\s*$",
            "Background (`&`) leaves a process running after the tool returns. "
            "Run the command in the foreground.",
        ),
        (
            r"\bnohup\b",
            "`nohup` is for detaching processes — exactly what this pipeline must never do.",
        ),
        (
            r"\bdisown\b",
            "`disown` detaches a process — exactly what this pipeline must never do.",
        ),
        (
            r"\btail\s+(?:-[a-zA-Z]*f|--follow)\b",
            "`tail -f` follows the file forever. Use `tail` with `-n` instead.",
        ),
        # Infinite shell loops. ``:`` is not a word char so we can't anchor
        # with \b after it; require whitespace or `;` to terminate.
        (
            r"\bwhile\s+(?:true\b|:(?=[\s;]))",
            "`while true` / `while :` is an infinite loop. Don't.",
        ),
    ]


def _ci_blocklist() -> List[Tuple[str, str]]:
    """CI polling commands known to hang in our environment."""
    return [
        (
            r"\bgh\s+run\s+watch\b",
            "`gh run watch` does not exit on workflow completion in some cases. "
            "Use the polling pattern with `gh run view --json status`.",
        ),
    ]


_BLOCKLIST: List[Tuple[str, str]] = (
    _npm_blocklist()
    + _framework_blocklist()
    + _py_blocklist()
    + _node_static_blocklist()
    + _watch_flag_blocklist()
    + _gradle_blocklist()
    + _android_sdk_blocklist()
    + _shell_blocklist()
    + _ci_blocklist()
)


# Interpreter-escape guard: `python -c "subprocess.run([...])"` / `node -e "...execSync..."`
# bypass the command allowlist because only the interpreter prefix is checked, not its
# inline code. We block the combination of an inline-eval flag (-c/-e/-p/--eval/--print)
# followed by a subprocess-spawn token. Known limit: `python script.py` is not caught —
# the hook sees only the filename; the real boundary is the sandbox (no credentials/egress).
# node includes `-p`/`--print` (print-eval) — same escape as `-e`, must also be listed.
_COMPILED_BLOCKLIST: List[Tuple[Pattern[str], str]] = [
    (re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in _BLOCKLIST
] + [
    (
        re.compile(
            r"\b(?:python3?|node)\b[^\n]*?\s(?:-c|-e|-p|--eval|--print)\b[^\n]*?"
            r"(?:subprocess\.|os\.system|os\.popen|os\.exec|pty\.spawn"
            r"|child_process|execSync|spawnSync)",
            re.IGNORECASE,
        ),
        (
            "Launching an external program from inline interpreter code "
            "(e.g. `python -c \"subprocess.run([...])\"` or `node -e \"...execSync...\"`) "
            "side-steps the command allowlist. Run the program directly as its own Bash "
            "command so the pipeline's tool policy applies. Use the interpreter for "
            "computation, not to shell out — if a tool is gated, it is gated on purpose."
        ),
    ),
]

_SCRIPT_SDKMANAGER_REASON = (
    "Running a workspace shell script that invokes `sdkmanager` side-steps the command "
    "allowlist. The shared Android SDK is operator-provisioned with `init-mobile-sdk.sh`; "
    "agents must report a missing SDK as an infrastructure blocker instead of installing one."
)


def _script_candidates(command: str) -> list[str]:
    """Return shell script tokens that are being executed, not merely read or chmod'd."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []

    candidates: list[str] = []
    for idx, token in enumerate(tokens):
        previous = Path(tokens[idx - 1]).name if idx > 0 else ""
        starts_command = idx == 0 or previous in {"&&", "||", ";", "|"}
        if token.endswith(".sh") and (
            previous in {"bash", "sh"} or token.startswith("./") or starts_command
        ):
            candidates.append(token)
    return candidates


def _script_path(token: str, cwd: str | None) -> Path | None:
    if not cwd:
        return None
    path = Path(token)
    if not path.is_absolute():
        path = Path(cwd) / path
    return path


def _script_invokes_sdkmanager(command: str, cwd: str | None) -> bool:
    for token in _script_candidates(command):
        path = _script_path(token, cwd)
        if path is None:
            continue
        try:
            if path.is_file() and re.search(r"\bsdkmanager\b", path.read_text(encoding="utf-8")):
                return True
        except OSError:
            continue
        except UnicodeDecodeError:
            continue
    return False


def _is_bash_tool_input(tool_name: str, tool_input: dict[str, Any]) -> bool:
    return tool_name == "Bash" and isinstance(tool_input.get("command"), str)


def check_bash_command(command: str, cwd: str | None = None) -> Tuple[bool, str | None]:
    """Inspect a Bash command string.

    Returns (is_blocked, reason_or_none). Side-effect-free — safe to unit-test
    without spinning up the SDK.
    """
    for pattern, reason in _COMPILED_BLOCKLIST:
        if pattern.search(command):
            return True, reason
    if _script_invokes_sdkmanager(command, cwd):
        return True, _SCRIPT_SDKMANAGER_REASON
    return False, None


async def _pre_tool_use_hook(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> HookJSONOutput:
    if input_data.get("hook_event_name") != "PreToolUse":
        return {}
    pre: PreToolUseHookInput = input_data  # type: ignore[assignment]
    tool_name = pre.get("tool_name", "")
    tool_input = pre.get("tool_input", {}) or {}

    if not _is_bash_tool_input(tool_name, tool_input):
        return {}

    command: str = tool_input["command"]
    cwd = input_data.get("cwd") if isinstance(input_data.get("cwd"), str) else None
    blocked, reason = check_bash_command(command, cwd=cwd)
    if not blocked:
        return {}

    deny_reason = (
        f"This command is forbidden by the generation pipeline: {reason} "
        "The pipeline runs unattended with a scoped tool allowlist — keep to the "
        "approved tools and let each command exit on its own. For long-running "
        "observation, capture a snapshot (one curl, one log dump) and exit."
    )
    logger.warning(
        "[agent-hook] blocked bash command: %r (tool_use_id=%s)",
        command,
        tool_use_id,
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": deny_reason,
        },
    }


def get_bash_guard_hooks() -> dict[HookEvent, list[HookMatcher]]:
    """Return the `hooks=` argument for ClaudeAgentOptions that installs the
    PreToolUse Bash guard.
    """
    return {
        "PreToolUse": [HookMatcher(matcher="Bash", hooks=[_pre_tool_use_hook])],
    }
