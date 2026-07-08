"""Unit tests for the PreToolUse Bash guard."""
import asyncio

import pytest

from app.services.agent_hooks import (
    _pre_tool_use_hook,
    check_bash_command,
    get_bash_guard_hooks,
)


# Commands that MUST be blocked. Each entry is (command, fragment-of-reason).
BLOCKED_COMMANDS = [
    ("npm start", "npm start"),
    ("npm serve", "npm start"),
    ("npm run dev", "npm run dev"),
    ("npm run serve", "npm run dev"),
    ("npm run start", "npm run dev"),
    ("npm run watch", "npm run dev"),
    ("yarn start", "yarn"),
    ("yarn dev", "yarn"),
    ("pnpm dev", "yarn"),
    ("npx http-server", "npx"),
    ("npx next dev", "npx"),
    ("npx nodemon", "npx"),
    ("next dev", "Framework dev/serve"),
    ("next start", "Framework dev/serve"),
    ("vite dev", "Framework dev/serve"),
    ("vite serve", "Framework dev/serve"),
    ("vite preview", "Framework dev/serve"),
    ("ng serve", "ng serve"),
    ("nodemon server.js", "nodemon"),
    ("flask run --debug", "flask run"),
    ("python manage.py runserver", "manage.py runserver"),
    ("python -m http.server 8080", "http.server"),
    ("uvicorn main:app", "ASGI"),
    ("gunicorn -w 4 app:app", "ASGI"),
    ("http-server -p 8080", "http-server"),
    ("npm run build -- --watch", "Watch"),
    # tsc -w is now matched by the narrower tsc-only rule (the generic -w was
    # removed to avoid false positives on grep -w / curl -w / python -w).
    # tsc --watch is still caught by the generic --watch rule (fires first).
    ("tsc -w", "tsc -w"),
    ("tsc --watch", "watch"),
    ("tsc src/ -w", "tsc -w"),
    ("./gradlew testDebugUnitTest --continuous", "continuous"),
    ("./gradlew testDebugUnitTest -t", "continuous"),
    ("sh gradlew test -t", "continuous"),
    ("bash ./gradlew :app:test --continuous", "continuous"),
    ("npm install &", "Background"),
    ("nohup python long.py", "nohup"),
    ("gh run watch 1234", "gh run watch"),
    ("tail -f /var/log/app.log", "tail -f"),
    ("tail --follow=name file.log", "tail -f"),
    ("while true; do echo hi; done", "infinite loop"),
    ("while :; do sleep 1; done", "infinite loop"),
    ("sdkmanager --version", "operator-only"),
    (
        "JAVA_HOME=/usr/lib/jvm/java-21-openjdk-arm64 /workspace/android/cmdline-tools/bin/sdkmanager --licenses",
        "operator-only",
    ),
    # Compound commands — the offending verb anywhere in the chain trips
    ("cd app && npm start", "npm start"),
    ("npm install && npm run dev", "npm run dev"),
    # Command-verb-anchored serve / http-server still trip when used as verbs.
    ("cd app && serve", "serve"),
    ("cmd | http-server -p 8080", "http-server"),
    # Command-verb-anchored nodemon / ASGI servers still trip after a shell separator.
    ("cd app && nodemon server.js", "nodemon"),
    ("cd app && uvicorn main:app", "ASGI"),
    ("foo; gunicorn app:app", "ASGI"),
    # Interpreter-escape guard: spawning a gated program from inline interpreter code
    # bypasses the command allowlist and must be denied (regardless of the inner program).
    (
        "python3 -c \"import subprocess; subprocess.run(['./gradlew', 'build'])\"",
        "side-steps the command allowlist",
    ),
    (
        "python -c \"import os; os.system('gh auth status')\"",
        "side-steps the command allowlist",
    ),
    (
        "node -e \"require('child_process').execSync('adb devices')\"",
        "side-steps the command allowlist",
    ),
    (
        "node --eval \"const {spawnSync}=require('child_process'); spawnSync('kubectl',['get','pods'])\"",
        "side-steps the command allowlist",
    ),
    # node's print-eval flags (-p / --print) are inline-eval too and must trip the guard.
    (
        "node -p \"require('child_process').execSync('adb devices').toString()\"",
        "side-steps the command allowlist",
    ),
    (
        "node --print \"require('child_process').execSync('./gradlew build')\"",
        "side-steps the command allowlist",
    ),
    # Even when wrapped behind a cd, the escape still trips.
    (
        "cd app && python3 -c \"import subprocess; subprocess.Popen(['sdkmanager','--list'])\"",
        "side-steps the command allowlist",
    ),
]


# Commands that MUST be allowed (legitimate one-shot operations).
ALLOWED_COMMANDS = [
    "npm install",
    "npm ci",
    "npm run build",
    "npm run build:prod",
    "npm run test",
    "npm test -- --run",
    "npm run lint",
    "npm run typecheck:strict",
    "yarn install",
    "yarn build",
    "pnpm install",
    "npx eslint .",
    "npx prettier --check .",
    "next build",
    "vite build",
    "tsc --noEmit",
    "pytest -q",
    "pytest tests/",
    "go test ./...",
    "cargo test",
    "./gradlew testDebugUnitTest --no-daemon --console=plain",
    "sh gradlew :app:test --no-daemon --console=plain",
    "bash ./gradlew build --no-daemon",
    "ruff check .",
    "mypy backend",
    "make check",
    "git status",
    "ls -la",
    "cat package.json",
    "echo hi",
    "tail -n 100 /var/log/app.log",
    "gh run view 1234 --json status",
    "gh run list --limit 5",
    # Previously false-positive on the generic -w rule — now allowed.
    "grep -w pattern file.txt",
    "grep -rw foo src/",
    'curl -s -w "%{time_total}" https://example.com',
    "curl -sw '%{http_code}' https://example.com",
    "python -w default script.py",
    # Previously false-positive on the serve lookbehind — now allowed.
    "vite build --base=/serve/",
    "node_modules/.bin/serve --version",
    "ls node_modules/.bin/serve",
    "--set serve.enabled=true",
    "echo path/to/serve",
    # http-server in a non-verb position is also allowed.
    "ls node_modules/.bin/http-server",
    "cat docs/http-server.md",
    # Dependency installs that mention server names — must not be blocked.
    "npm install nodemon",
    "npm install -D nodemon",
    "pnpm add -D nodemon",
    "yarn add nodemon",
    "pip install uvicorn",
    "pip install uvicorn fastapi",
    "pip install -r requirements.txt",
    "poetry add gunicorn",
    "uv pip install hypercorn",
    "pip install daphne",
    # Reading docs / grep / lockfile contents that mention the names — allowed.
    "cat package.json | grep nodemon",
    "grep -r uvicorn .",
    "ls node_modules/.bin/nodemon",
    # Interpreter-escape guard must NOT fire on legitimate interpreter use:
    #   - inline code doing pure computation (no subprocess spawn)
    'python3 -c "import json,sys; print(json.load(sys.stdin)[\'k\'])"',
    'python -c "print(2 + 2)"',
    'node -e "console.log(process.version)"',
    #   - searching/reading source that merely mentions the spawn APIs (no inline -c/-e)
    'grep -rn "subprocess.run" .',
    "cat scripts/run.py | grep os.system",
    "rg child_process src/",
    "grep -rn sdkmanager docs/",
    "cat setup-sdk.sh | grep sdkmanager",
    "ensure-android-sdk-package 'platforms;android-33'",
    #   - running a script file (not inline) is governed by the allowlist, not flagged here
    "python manage.py migrate",
    "node server-build.js",
    #   - the `-c` flag on a non-interpreter, or without a spawn token, is fine
    "npm run build -c prod.config.js",
]


@pytest.mark.parametrize("command,reason_fragment", BLOCKED_COMMANDS)
def test_blocked_commands_match(command: str, reason_fragment: str) -> None:
    blocked, reason = check_bash_command(command)
    assert blocked, f"expected {command!r} to be blocked"
    assert reason is not None
    assert reason_fragment.lower() in reason.lower(), (
        f"expected reason for {command!r} to mention {reason_fragment!r}, got {reason!r}"
    )


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_allowed_commands_pass(command: str) -> None:
    blocked, reason = check_bash_command(command)
    assert not blocked, f"expected {command!r} to be allowed but was blocked: {reason!r}"


def test_hook_returns_deny_decision_for_blocked_bash_call() -> None:
    input_data = {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "transcript_path": "/tmp/t",
        "cwd": "/tmp",
        "tool_name": "Bash",
        "tool_input": {"command": "npm start"},
        "tool_use_id": "tu_1",
    }
    output = asyncio.run(_pre_tool_use_hook(input_data, "tu_1", {"signal": None}))  # type: ignore[arg-type]
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "forbidden" in reason.lower()
    assert "npm start" in reason


def test_hook_passes_through_allowed_bash_call() -> None:
    input_data = {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "transcript_path": "/tmp/t",
        "cwd": "/tmp",
        "tool_name": "Bash",
        "tool_input": {"command": "npm install"},
        "tool_use_id": "tu_1",
    }
    output = asyncio.run(_pre_tool_use_hook(input_data, "tu_1", {"signal": None}))  # type: ignore[arg-type]
    assert output == {}


def test_blocks_shell_script_that_invokes_sdkmanager(tmp_path) -> None:
    script = tmp_path / "setup-sdk.sh"
    script.write_text("#!/bin/sh\nsdkmanager 'platforms;android-34'\n", encoding="utf-8")

    blocked, reason = check_bash_command("chmod +x setup-sdk.sh && bash setup-sdk.sh", cwd=str(tmp_path))

    assert blocked
    assert reason is not None
    assert "side-steps the command allowlist" in reason


def test_allows_shell_script_without_sdkmanager(tmp_path) -> None:
    script = tmp_path / "run-tests.sh"
    script.write_text("#!/bin/sh\n./gradlew testDebugUnitTest\n", encoding="utf-8")

    blocked, reason = check_bash_command("bash run-tests.sh", cwd=str(tmp_path))

    assert not blocked
    assert reason is None


def test_allows_reading_shell_script_that_mentions_sdkmanager(tmp_path) -> None:
    script = tmp_path / "setup-sdk.sh"
    script.write_text("#!/bin/sh\nsdkmanager 'platforms;android-34'\n", encoding="utf-8")

    blocked, reason = check_bash_command(f"cat {script}", cwd=str(tmp_path))

    assert not blocked
    assert reason is None


def test_hook_ignores_non_bash_tools() -> None:
    input_data = {
        "hook_event_name": "PreToolUse",
        "session_id": "s",
        "transcript_path": "/tmp/t",
        "cwd": "/tmp",
        "tool_name": "Read",
        "tool_input": {"file_path": "/etc/passwd"},
        "tool_use_id": "tu_1",
    }
    output = asyncio.run(_pre_tool_use_hook(input_data, "tu_1", {"signal": None}))  # type: ignore[arg-type]
    assert output == {}


def test_get_bash_guard_hooks_registers_pretooluse_bash_matcher() -> None:
    hooks = get_bash_guard_hooks()
    assert list(hooks.keys()) == ["PreToolUse"]
    matchers = hooks["PreToolUse"]
    assert len(matchers) == 1
    assert matchers[0].matcher == "Bash"
    assert len(matchers[0].hooks) == 1
