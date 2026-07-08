"""
Tests for path-related Settings fields (Task 2.4).

Validates that:
- CLAUDE_CODE_TMPDIR_PATH defaults to {WORKSPACE_BASE_PATH}/claude_code_tmpdir
- Changing WORKSPACE_BASE_PATH derives the correct tmpdir when not explicitly set
- Explicit CLAUDE_CODE_TMPDIR_PATH env value wins over the derivation
- WORKSPACE_DIR still honours the WORKSPACE_PATH alias
"""

import pytest


class TestClaudeCodeTmpdirDerivation:
    def test_default_tmpdir(self):
        """Default tmpdir = /workspaces/claude_code_tmpdir."""
        from app.core.config import Settings

        s = Settings()
        assert s.CLAUDE_CODE_TMPDIR_PATH == "/workspaces/claude_code_tmpdir"

    def test_custom_workspace_base_derives_tmpdir(self):
        """Custom WORKSPACE_BASE_PATH → tmpdir follows the new base."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("WORKSPACE_BASE_PATH", "/ws")
            mp.delenv("CLAUDE_CODE_TMPDIR_PATH", raising=False)
            s = Settings()
        assert s.CLAUDE_CODE_TMPDIR_PATH == "/ws/claude_code_tmpdir"

    def test_explicit_tmpdir_wins(self):
        """Explicit CLAUDE_CODE_TMPDIR_PATH overrides the derivation."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("WORKSPACE_BASE_PATH", "/ws")
            mp.setenv("CLAUDE_CODE_TMPDIR_PATH", "/custom/tmp")
            s = Settings()
        assert s.CLAUDE_CODE_TMPDIR_PATH == "/custom/tmp"


class TestWorkspaceDirAlias:
    def test_workspace_path_env_sets_workspace_dir(self):
        """WORKSPACE_PATH env var populates WORKSPACE_DIR (AliasChoices)."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("WORKSPACE_PATH", "/my/workdir")
            mp.delenv("WORKSPACE_DIR", raising=False)
            s = Settings()
        assert s.WORKSPACE_DIR == "/my/workdir"

    def test_workspace_dir_env_sets_workspace_dir(self):
        """WORKSPACE_DIR env var also populates WORKSPACE_DIR."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("WORKSPACE_DIR", "/my/workdir2")
            mp.delenv("WORKSPACE_PATH", raising=False)
            s = Settings()
        assert s.WORKSPACE_DIR == "/my/workdir2"


class TestAgentSdkmanagerPolicy:
    def test_hosted_default_disallows_agent_sdkmanager(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("ALLOW_AGENT_SDKMANAGER", raising=False)
            s = Settings()
        assert s.ALLOW_AGENT_SDKMANAGER is False

    def test_env_can_enable_local_quickstart_agent_sdkmanager(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ALLOW_AGENT_SDKMANAGER", "true")
            s = Settings()
        assert s.ALLOW_AGENT_SDKMANAGER is True


class TestExcludedArtifactPatternsAlias:
    def test_legacy_env_name_still_populates_field(self):
        """Legacy CODE_ARCHIVE_EXCLUDE_PATTERNS env still populates EXCLUDED_ARTIFACT_PATTERNS."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("CODE_ARCHIVE_EXCLUDE_PATTERNS", '["foo","bar"]')
            mp.delenv("EXCLUDED_ARTIFACT_PATTERNS", raising=False)
            s = Settings()
        assert s.EXCLUDED_ARTIFACT_PATTERNS == ["foo", "bar"]

    def test_default_includes_git(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("CODE_ARCHIVE_EXCLUDE_PATTERNS", raising=False)
            mp.delenv("EXCLUDED_ARTIFACT_PATTERNS", raising=False)
            s = Settings()
        assert ".git" in s.EXCLUDED_ARTIFACT_PATTERNS

    @pytest.mark.parametrize(
        "pattern",
        [
            "android-sdk-local",
            "cmdline-tools.zip",
            "setup-sdk.sh",
        ],
    )
    def test_default_excludes_workspace_local_android_sdk_artifacts(self, pattern):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("CODE_ARCHIVE_EXCLUDE_PATTERNS", raising=False)
            mp.delenv("EXCLUDED_ARTIFACT_PATTERNS", raising=False)
            s = Settings()
        assert pattern in s.EXCLUDED_ARTIFACT_PATTERNS


class TestWorkspaceExcludePatternsParsing:
    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("", []),
            ("   ", []),
            ("[]", []),
            ('[".vscode", ".idea"]', [".vscode", ".idea"]),
            ("['.log', '.data']", [".log", ".data"]),
        ],
    )
    def test_parses_blank_and_python_list(self, env_value, expected):
        """Blank -> []; a Python list with single or double quotes parses to a list."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("WORKSPACE_EXCLUDE_PATTERNS", env_value)
            s = Settings()
        assert s.WORKSPACE_EXCLUDE_PATTERNS == expected

    def test_unset_defaults_to_empty_list(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("WORKSPACE_EXCLUDE_PATTERNS", raising=False)
            s = Settings()
        assert s.WORKSPACE_EXCLUDE_PATTERNS == []

    @pytest.mark.parametrize("bad", [".log,.data", "not_a_list", "{'a': 1}"])
    def test_non_list_value_is_rejected(self, bad):
        """A non-list value (e.g. the old comma form) is rejected with a clear error."""
        import pydantic

        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("WORKSPACE_EXCLUDE_PATTERNS", bad)
            with pytest.raises(pydantic.ValidationError):
                Settings()
