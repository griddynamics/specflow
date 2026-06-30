"""Tests for tool usage configuration."""

import re

from app.core.tool_usage import (
    ANDROID_SDK_BASH_USAGE,
    bash_usage,
    get_disallowed_tools,
    get_rosetta_allowed_tools,
    get_workspace_rm_bash_allowlist,
    GH_CLI_USAGE,
)


class TestGetDisallowedTools:
    """Tests for get_disallowed_tools function."""

    def test_returns_list(self):
        result = get_disallowed_tools()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_blocks_version_control(self):
        result = get_disallowed_tools()
        assert "Read(.git)" in result
        assert "Glob(.git)" in result
        # `LS` was removed from the Claude Code CLI; it must not appear in rules.
        assert not any(t.startswith("LS(") for t in result)

    def test_blocks_python_venvs(self):
        result = get_disallowed_tools()
        assert "Read(.venv)" in result
        assert "Read(venv)" in result
        assert "Read(__pycache__)" in result

    def test_blocks_node_modules(self):
        result = get_disallowed_tools()
        assert "Read(node_modules)" in result
        assert "Glob(node_modules)" in result

    def test_blocks_ide_directories(self):
        result = get_disallowed_tools()
        assert "Read(.idea)" in result
        assert "Read(.vscode)" in result

    def test_blocks_dangerous_bash_commands(self):
        result = get_disallowed_tools()
        assert "Bash(sudo:*)" in result
        assert "Bash(chown:*)" in result
        assert "Bash(rm:*)" not in result
        assert "Bash(chmod:*)" not in result

    def test_workspace_rm_allowlist_scoped_to_path(self):
        ws = "/workspaces/ws-01-1"
        tools = get_workspace_rm_bash_allowlist(ws)
        assert tools == ["Bash(rm:/workspaces/ws-01-1/**)"]
        # Must not return a wildcard-root grant that would allow rm anywhere
        assert "Bash(rm:*)" not in tools


class TestGetRosettaAllowedTools:
    """Tests for get_rosetta_allowed_tools function."""

    def test_generates_all_operations(self):
        """Scenario: Generates Read, Write, Edit, StrReplace, Glob operations for rosetta/ directory."""
        workspace_path = "/workspaces/test-ws"
        rosetta_dir = "rosetta"
        rosetta_path = f"{workspace_path}/{rosetta_dir}"

        result = get_rosetta_allowed_tools(workspace_path, rosetta_dir)

        assert f"Read({rosetta_path}/**)" in result
        assert f"Write({rosetta_path}/**)" in result
        assert f"Edit({rosetta_path}/**)" in result
        assert f"StrReplace({rosetta_path}/**)" in result
        assert f"Glob({rosetta_path}/**)" in result
        # No .claude/ entries — agent writes to rosetta/agents/ (no sensitive path)
        assert not any(".claude" in t for t in result)

    def test_handles_different_workspace_paths(self):
        """Scenario: Works with different workspace paths."""
        workspace_path = "/workspaces/another-workspace"
        rosetta_dir = "kb_output"
        rosetta_path = f"{workspace_path}/{rosetta_dir}"

        result = get_rosetta_allowed_tools(workspace_path, rosetta_dir)

        assert f"Read({rosetta_path}/**)" in result
        assert f"Write({rosetta_path}/**)" in result

    def test_includes_write_operations_for_claude_directories(self):
        """Scenario: KB init agent can write to rosetta/agents/ (remapped to .claude/agents/ on unpack)."""
        workspace_path = "/workspaces/test"
        rosetta_dir = "rosetta"
        rosetta_path = f"{workspace_path}/{rosetta_dir}"

        result = get_rosetta_allowed_tools(workspace_path, rosetta_dir)

        # Recursive wildcard covers all nested paths including rosetta/agents/
        assert f"Write({rosetta_path}/**)" in result
        assert f"StrReplace({rosetta_path}/**)" in result
        # No .claude/ paths — agent stages to rosetta/agents/, unpack remaps to .claude/agents/
        assert not any(".claude" in t for t in result)


class TestBashUsage:
    """Tests for the bash_usage allowed-tools list."""

    def test_gh_cli_not_in_bash_usage(self):
        """gh must not be in bash_usage — it is deploy-only and lives in GH_CLI_USAGE."""
        assert "Bash(gh:*)" not in bash_usage

    def test_git_allowed(self):
        assert "Bash(git:*)" in bash_usage

    def test_python_allowed(self):
        assert "Bash(python3:*)" in bash_usage

    def test_npm_allowed(self):
        assert "Bash(npm:*)" in bash_usage

    def test_gradle_wrapper_allowed(self):
        """Multiple spellings are granted because each is a distinct literal prefix to the matcher."""
        for spelling in (
            "Bash(./gradlew:*)",
            "Bash(sh gradlew:*)",
            "Bash(bash gradlew:*)",
            "Bash(sh ./gradlew:*)",
            "Bash(bash ./gradlew:*)",
        ):
            assert spelling in bash_usage

    def test_chmod_execute_allowed_but_not_general(self):
        assert "Bash(chmod +x:*)" in bash_usage
        assert "Bash(chmod:*)" not in bash_usage

    def test_kotlin_allowed(self):
        assert "Bash(kotlin:*)" in bash_usage
        assert "Bash(kotlinc:*)" in bash_usage

    def test_flutter_and_dart_allowed(self):
        """Cross-platform mobile (Flutter) builds/tests via flutter + dart CLIs."""
        assert "Bash(flutter:*)" in bash_usage
        assert "Bash(dart:*)" in bash_usage

    def test_android_device_tools_not_in_bash_usage(self):
        """Emulator/device tools are deploy/QA-only — they live in ANDROID_SDK_BASH_USAGE."""
        for tool in ANDROID_SDK_BASH_USAGE:
            assert tool not in bash_usage

    def test_sdkmanager_is_operator_only(self):
        """sdkmanager is operator-only; agents installing packages would race on the shared SDK."""
        assert "Bash(sdkmanager:*)" not in bash_usage
        assert "Bash(sdkmanager:*)" not in ANDROID_SDK_BASH_USAGE


_BASH_RULE_RE = re.compile(r"^Bash\((?P<prefix>.+):\*\)$")


def _bash_prefixes(rules):
    """Extract the literal command prefix from each ``Bash(<prefix>:*)`` rule."""
    return [m.group("prefix") for m in (_BASH_RULE_RE.match(r) for r in rules) if m]


def _is_allowed(command, rules):
    """True iff some rule in ``rules`` covers ``command`` by literal prefix."""
    return any(command.startswith(prefix) for prefix in _bash_prefixes(rules))


class TestBashAllowlistCoversRealCommands:
    """Verify that rules cover the actual command spellings agents use and reject look-alikes."""

    # Real commands agents/generated docs issue — each MUST be covered by bash_usage.
    ALLOWED_COMMANDS = [
        "chmod +x gradlew",
        "chmod +x ./gradlew",
        "./gradlew testDebugUnitTest",
        "sh gradlew assembleDebug",
        "bash gradlew build",
        "sh ./gradlew :app:test",
        "bash ./gradlew clean",
        "flutter build apk",
        "flutter pub get",
        "dart pub get",
        "kotlinc Main.kt -include-runtime -d main.jar",
        "kotlin -version",
    ]

    # Sensitive look-alikes that must stay OUTSIDE the generation allowlist:
    # broader chmod modes, SDK package installs, and deploy-only device tools.
    DENIED_COMMANDS = [
        "chmod 4755 gradlew",
        "chmod -x gradlew",
        "chmod 777 secret",
        "chmod -R 777 .",
        "sdkmanager --install 'platforms;android-30'",
        "adb devices",
        "emulator -avd test",
    ]

    def test_real_commands_are_covered(self):
        for command in self.ALLOWED_COMMANDS:
            assert _is_allowed(command, bash_usage), (
                f"{command!r} should be covered by a bash_usage prefix rule"
            )

    def test_sensitive_commands_not_covered(self):
        for command in self.DENIED_COMMANDS:
            assert not _is_allowed(command, bash_usage), (
                f"{command!r} must NOT be covered by any bash_usage prefix rule"
            )

    def test_device_tools_covered_only_in_deploy_list(self):
        """adb/avdmanager/emulator are denied in bash_usage but covered once the
        deploy-only ANDROID_SDK_BASH_USAGE is added (the deployment-phase tool set)."""
        deploy_tools = bash_usage + ANDROID_SDK_BASH_USAGE
        for command in ("adb devices", "emulator -avd test", "avdmanager list"):
            assert not _is_allowed(command, bash_usage)
            assert _is_allowed(command, deploy_tools)


class TestGhCliUsage:
    """GH_CLI_USAGE is a separate list granted only to deploy/QA agents."""

    def test_gh_cli_in_gh_cli_usage(self):
        assert "Bash(gh:*)" in GH_CLI_USAGE

    def test_gh_cli_usage_does_not_overlap_bash_usage(self):
        overlap = set(GH_CLI_USAGE) & set(bash_usage)
        assert overlap == set(), f"Unexpected overlap between GH_CLI_USAGE and bash_usage: {overlap}"
