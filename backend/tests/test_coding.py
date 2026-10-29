from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import HookMatcher

import llm.coding as coding_module
from llm import (
    AgentPermissions,
    AgentSettings,
    CodingAgent,
    check_bash_command,
    conservative_bash_guard_hooks,
    conservative_bash_timeout_environment,
    conservative_coding_profile,
)


def test_coding_agent_requires_explicit_permissions() -> None:
    with pytest.raises(TypeError, match="permissions"):
        CodingAgent(model="test-model")

    with pytest.raises(TypeError, match="AgentPermissions"):
        CodingAgent(model="test-model", permissions=None)


def test_conservative_profile_scopes_file_tools_to_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    permissions = conservative_coding_profile(workspace)

    resolved = workspace.resolve()
    assert permissions.allowed_tools == (
        f"Read(//{resolved.as_posix().lstrip('/')}/**)",
        f"Edit(//{resolved.as_posix().lstrip('/')}/**)",
        f"Glob(//{resolved.as_posix().lstrip('/')}/**)",
    )
    assert permissions.permission_mode == "dontAsk"
    assert permissions.allow_unconstrained_tools is False
    assert permissions.workspace_root == resolved


def test_conservative_profile_accepts_only_scoped_bash_grants(
    tmp_path: Path,
) -> None:
    permissions = conservative_coding_profile(
        tmp_path,
        bash_tools=("Bash(git:*)", "Bash(pytest:*)"),
    )

    assert permissions.allowed_tools[-2:] == ("Bash(git:*)", "Bash(pytest:*)")
    with pytest.raises(ValueError, match="Unconstrained tool grants"):
        conservative_coding_profile(tmp_path, bash_tools=("Bash",))


def test_conservative_profile_rejects_empty_cwd() -> None:
    with pytest.raises(ValueError, match="cwd must not be empty"):
        conservative_coding_profile(" ")


def test_conservative_profile_rejects_root_and_rule_metacharacters(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        conservative_coding_profile(Path(Path.cwd().anchor))
    with pytest.raises(ValueError, match="unsupported by SDK permission rules"):
        conservative_coding_profile(tmp_path / "bad)path")


def test_coding_agent_rejects_missing_or_mismatched_profile_cwd(
    tmp_path: Path,
) -> None:
    permissions = conservative_coding_profile(tmp_path)

    with pytest.raises(ValueError, match="cwd is required"):
        CodingAgent(model="test-model", permissions=permissions)
    with pytest.raises(ValueError, match="does not match"):
        CodingAgent(
            model="test-model",
            cwd=tmp_path / "other",
            permissions=permissions,
        )


def test_coding_agent_accepts_equivalent_resolved_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = tmp_path / "workspace-link"
    link.symlink_to(workspace, target_is_directory=True)

    agent = CodingAgent(
        model="test-model",
        cwd=link,
        permissions=conservative_coding_profile(workspace),
    )

    assert agent.agent.cwd == link


def test_conservative_bash_timeout_environment_validates_invariants() -> None:
    assert conservative_bash_timeout_environment() == {
        "BASH_DEFAULT_TIMEOUT_MS": "600000",
        "BASH_MAX_TIMEOUT_MS": "1800000",
    }
    with pytest.raises(ValueError, match="must not exceed"):
        conservative_bash_timeout_environment(
            default_seconds=20,
            maximum_seconds=10,
        )
    with pytest.raises(ValueError, match="less than run_timeout_seconds"):
        conservative_bash_timeout_environment(
            default_seconds=30,
            maximum_seconds=60,
            run_timeout_seconds=60,
        )
    with pytest.raises(ValueError, match="positive integer"):
        conservative_bash_timeout_environment(default_seconds=True)
    with pytest.raises(ValueError, match="run_timeout_seconds must be positive"):
        conservative_bash_timeout_environment(run_timeout_seconds=float("nan"))


@pytest.mark.parametrize(
    "command",
    [
        "npm start",
        "npm --silent start",
        "npm --prefix /tmp start",
        "npm serve",
        "npm run dev",
        "npm run watch",
        "yarn dev",
        "pnpm run dev",
        "npx http-server",
        "npx next dev",
        "npx --yes vite dev",
        "npx nodemon",
        "npx vite",
        "next start",
        "vite preview",
        "vite --host 0.0.0.0",
        "ng serve",
        "nodemon server.js",
        "flask run --debug",
        "flask --app app run",
        "pytest --watch",
        "jest --watch=true",
        "npm run test -- --watchAll=true",
        "tsc -w",
        "tsc src/ -w",
        "./gradlew test --continuous",
        "sh gradlew test -t",
        "nohup worker &",
        "./worker >/tmp/worker.log 2>&1 & echo $!",
        "worker & >/tmp/worker.log",
        "gh run watch 1234",
        "tail -f /tmp/app.log",
        "while true; do echo hi; done",
        "python -m http.server 8000",
        "python -m uvicorn app:app",
        "python manage.py runserver",
        "./node_modules/.bin/vite dev",
        "cd app && npm start",
        'bash -c "npm run dev"',
        "bash -lc 'npm run dev'",
        "bash -c -- 'npm run dev'",
        "env X=1 vite dev",
        "env -i X=1 npm run dev",
        "(npm run dev)",
        "time npm run dev",
        "time -p npm run dev",
        "command -- npm run dev",
        "printf ok\nnpm run dev",
        "echo `npm run dev`",
        "! npm run dev",
        "if true; then npm run dev; fi",
        "bash <<'EOF'\nnpm run dev\nEOF",
        "python -c \"import subprocess; subprocess.run(['tool'])\"",
        "python -c \"from subprocess import run; run(['tool'])\"",
        "python3.12 -m http.server",
        "python -c\"import subprocess; subprocess.run(['tool'])\"",
        "node -e \"require('child_process').execSync('tool')\"",
    ],
)
def test_conservative_bash_guard_blocks_long_lived_and_escape_commands(
    command: str,
) -> None:
    blocked, reason = check_bash_command(command)

    assert blocked is True
    assert reason


@pytest.mark.parametrize(
    "command",
    [
        "npm install",
        "npm ci",
        "npm run build",
        "npm run test",
        "yarn install",
        "yarn build",
        "pnpm install",
        "npx eslint .",
        "npx prettier --check .",
        "npx vite build",
        "npx --yes vite build",
        "npx serve --help",
        "next build",
        "vite build",
        "vite --help",
        "tsc --noEmit",
        "pytest -q",
        "pytest -q >/tmp/test.log 2>&1",
        "pytest -q &>/tmp/test.log",
        "./gradlew test --no-daemon",
        "grep -w pattern file",
        'curl -s -w "%{time_total}" https://example.com',
        "python -w default script.py",
        "pip install uvicorn",
        "npm install nodemon",
        "poetry add gunicorn",
        "grep -r uvicorn .",
        "serve --help",
        "vite build --base=/serve/",
        "node_modules/.bin/serve --version",
        "ls node_modules/.bin/serve",
        'python -c "print(2 + 2)"',
        'node -e "console.log(process.version)"',
        "python manage.py migrate",
        "node server-build.js",
        "git status && git diff",
        'echo "npm run dev"',
        'git commit -m "document npm run dev"',
        'git commit -m "--watch"',
    ],
)
def test_conservative_bash_guard_allows_one_shot_commands(
    command: str,
) -> None:
    assert check_bash_command(command) == (False, None)


def test_conservative_bash_guard_fails_closed_on_excessive_wrappers() -> None:
    blocked, reason = check_bash_command("command " * 1000 + "npm run dev")

    assert blocked is True
    assert reason == "The shell command is too complex to inspect safely."


@pytest.mark.asyncio
async def test_bash_guard_hook_denies_only_matching_bash_commands() -> None:
    hooks = conservative_bash_guard_hooks()
    matcher = hooks["PreToolUse"][0]
    hook = matcher.hooks[0]

    denied = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "npm run dev"},
            "session_id": "session",
            "cwd": "/workspace",
        },
        "tool-use",
        None,
    )
    allowed = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "npm run build"},
            "session_id": "session",
            "cwd": "/workspace",
        },
        "tool-use",
        None,
    )
    ignored = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/workspace/file"},
            "session_id": "session",
            "cwd": "/workspace",
        },
        "tool-use",
        None,
    )

    output = denied["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert output["permissionDecisionReason"]
    assert allowed == {}
    assert ignored == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_input",
    [{}, {"command": None}, {"command": ["npm run dev"]}, []],
)
async def test_bash_guard_hook_denies_malformed_bash_input(
    tool_input: object,
) -> None:
    hook = conservative_bash_guard_hooks()["PreToolUse"][0].hooks[0]

    denied = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": tool_input,
            "session_id": "session",
            "cwd": "/workspace",
        },
        "tool-use",
        None,
    )

    output = denied["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "Bash tool input" in output["permissionDecisionReason"]


@pytest.mark.asyncio
async def test_coding_agent_preserves_caller_settings_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    delegated_result = object()

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def run(
            self,
            prompt_or_messages: object,
            *,
            resume_session_id: str | None = None,
            output_format: dict[str, Any] | None = None,
        ) -> object:
            captured["prompt"] = prompt_or_messages
            captured["resume_session_id"] = resume_session_id
            captured["output_format"] = output_format
            return delegated_result

    monkeypatch.setattr(coding_module, "Agent", FakeAgent)
    caller_hook = HookMatcher(matcher="Read")
    sandbox = {"enabled": True}
    settings_permissions = AgentPermissions()
    explicit_permissions = conservative_coding_profile(tmp_path)
    settings = AgentSettings(
        permissions=settings_permissions,
        hooks={"PreToolUse": [caller_hook]},
        sandbox=sandbox,
        max_turns=12,
    )

    agent = CodingAgent(
        model="test-model",
        permissions=explicit_permissions,
        cwd=tmp_path,
        settings=settings,
    )
    output_format = {"type": "json_schema", "schema": {"type": "object"}}
    result = await agent.run(
        "implement",
        resume_session_id="resume",
        output_format=output_format,
    )

    effective_settings = captured["settings"]
    assert effective_settings.permissions is explicit_permissions
    assert effective_settings.hooks == {"PreToolUse": [caller_hook]}
    assert effective_settings.sandbox is sandbox
    assert effective_settings.max_turns == 12
    assert captured["cwd"] == tmp_path
    assert captured["prompt"] == "implement"
    assert captured["resume_session_id"] == "resume"
    assert captured["output_format"] == output_format
    assert result is delegated_result
