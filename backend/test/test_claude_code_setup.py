"""Unit tests for claude_code.py environment setup helpers and CLAUDE_CODE_TMPDIR injection."""

import asyncio
import os
from pathlib import Path
from unittest.mock import patch, MagicMock


import app.services.claude_code as cc
from app.agents_sandboxing.claude_env_vars import build_redacted_env_overlay, REDACTED_PLACEHOLDER
from app.schemas.model_token_usage import FLAT_AGGREGATE_MODEL_NAME, ModelTokenUsage
from app.services.claude_code import (
    clear_workspace_caches,
    setup_claude_code_max_output_tokens,
    setup_claude_code_tmpdir,
    setup_rosetta_plugin_env,
    setup_workspace_cache_directories,
)


# ---------------------------------------------------------------------------
# setup_rosetta_plugin_env
# ---------------------------------------------------------------------------

def test_rosetta_plugin_env_points_at_plugin_path(tmp_path: Path) -> None:
    """Existing ROSETTA_PLUGIN_PATH -> CLAUDE_PLUGIN_ROOT = that path."""
    with patch.object(cc.settings, "ROSETTA_PLUGIN_PATH", str(tmp_path)):
        assert setup_rosetta_plugin_env() == {"CLAUDE_PLUGIN_ROOT": str(tmp_path)}


def test_rosetta_plugin_env_empty_when_path_unset_or_missing(tmp_path: Path) -> None:
    """Path unset / missing on disk -> no env var set."""
    with patch.object(cc.settings, "ROSETTA_PLUGIN_PATH", None):
        assert setup_rosetta_plugin_env() == {}
    with patch.object(cc.settings, "ROSETTA_PLUGIN_PATH", str(tmp_path / "does-not-exist")):
        assert setup_rosetta_plugin_env() == {}


def test_rosetta_plugin_env_reevaluates_disk_state_each_call(tmp_path: Path) -> None:
    """The is_dir() check is NOT cached: a path that appears/disappears is reflected each call.

    Guards against the prior lru_cache, which pinned the first result for the process and could
    desync CLAUDE_PLUGIN_ROOT from a lazily-mounted (or removed) plugin dir.
    """
    plugin = tmp_path / "plugin-probe"
    with patch.object(cc.settings, "ROSETTA_PLUGIN_PATH", str(plugin)):
        # Not yet on disk -> no env var.
        assert setup_rosetta_plugin_env() == {}
        # Appears later (e.g. lazy mount) -> picked up without any cache clear.
        plugin.mkdir()
        assert setup_rosetta_plugin_env() == {"CLAUDE_PLUGIN_ROOT": str(plugin)}
        # Removed again -> reflected immediately.
        plugin.rmdir()
        assert setup_rosetta_plugin_env() == {}


# ---------------------------------------------------------------------------
# setup_workspace_cache_directories
# ---------------------------------------------------------------------------

EXPECTED_PATH_KEYS = {
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "PIP_CACHE_DIR",
    "npm_config_cache",
    "YARN_CACHE_FOLDER",
    "COMPOSER_CACHE_DIR",
    "GOMODCACHE",
    "GOPATH",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "GRADLE_USER_HOME",
    "ANDROID_USER_HOME",
    "ANDROID_AVD_HOME",
    "ANDROID_EMULATOR_HOME",
    "PUB_CACHE",
    "NUGET_PACKAGES",
}

NON_PATH_KEYS = {
    "MAVEN_OPTS",
    "DO_NOT_TRACK",
    "FLUTTER_NO_ANALYTICS",
    "DOTNET_CLI_TELEMETRY_OPTOUT",
    "DOTNET_NOLOGO",
    "NEXT_TELEMETRY_DISABLED",
    "GATSBY_TELEMETRY_DISABLED",
    "ASTRO_TELEMETRY_DISABLED",
    "STORYBOOK_DISABLE_TELEMETRY",
    "HOMEBREW_NO_ANALYTICS",
}

COMMON_PATH_KEYS = {"ANDROID_SDK_ROOT", "ANDROID_HOME"}

# Provisioned out-of-band (FLUTTER_ROOT is copied from the shared template by wrapper scripts).
PER_WORKSPACE_UNCREATED_PATH_KEYS = {"FLUTTER_ROOT"}


def _call_with_mock_base(workspace_path: str, caches_base: Path):
    """Call setup_workspace_cache_directories with settings.WORKSPACE_BASE_PATH mocked."""
    mock_settings = MagicMock()
    mock_settings.WORKSPACE_BASE_PATH = str(caches_base)
    with patch("app.services.claude_code.settings", mock_settings):
        return setup_workspace_cache_directories(workspace_path)


class TestSetupWorkspaceCacheDirectories:
    """setup_workspace_cache_directories returns the right env vars and creates dirs."""

    def test_returns_all_expected_env_vars(self, tmp_path):
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        all_keys = (
            EXPECTED_PATH_KEYS
            | NON_PATH_KEYS
            | COMMON_PATH_KEYS
            | PER_WORKSPACE_UNCREATED_PATH_KEYS
        )
        for key in all_keys:
            assert key in result, f"Missing expected key: {key}"

    def test_path_values_are_under_caches_not_workspace(self, tmp_path):
        """Caches must be outside the workspace git repo to prevent accidental commits."""
        workspace_path = str(tmp_path / "ws-01-1")
        result = _call_with_mock_base(workspace_path, tmp_path)
        cache_root = str(tmp_path / "caches" / "ws-01-1")
        for key in EXPECTED_PATH_KEYS:
            assert result[key].startswith(cache_root), (
                f"{key}={result[key]!r} not under cache root {cache_root!r}"
            )
        # Must NOT be inside the workspace repo itself
        for key in EXPECTED_PATH_KEYS:
            assert not result[key].startswith(workspace_path), (
                f"{key}={result[key]!r} is inside workspace repo — would be committed"
            )

    def test_creates_directories_on_disk(self, tmp_path):
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        for key in EXPECTED_PATH_KEYS:
            assert os.path.isdir(result[key]), f"{key}={result[key]!r} was not created"

    def test_maven_opts_not_created_as_directory(self, tmp_path):
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        assert "MAVEN_OPTS" in result
        assert result["MAVEN_OPTS"].startswith("-D")

    def test_xdg_config_home_redirected_off_root(self, tmp_path):
        """XDG_CONFIG_HOME must land on the per-workspace NFS cache, not the pod rootfs."""
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        cache_root = str(tmp_path / "caches" / "ws-01-1")
        assert result["XDG_CONFIG_HOME"].startswith(cache_root)
        assert os.path.isdir(result["XDG_CONFIG_HOME"])

    def test_telemetry_opt_outs_present_and_not_dirs(self, tmp_path):
        """Scalar opt-out flags must be returned but never created as directories."""
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        expected = {
            "DO_NOT_TRACK": "1",
            "FLUTTER_NO_ANALYTICS": "1",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
            "NEXT_TELEMETRY_DISABLED": "1",
            "GATSBY_TELEMETRY_DISABLED": "1",
            "ASTRO_TELEMETRY_DISABLED": "1",
            "STORYBOOK_DISABLE_TELEMETRY": "1",
            "HOMEBREW_NO_ANALYTICS": "1",
        }
        for key, value in expected.items():
            assert result.get(key) == value, f"{key} should be {value!r}, got {result.get(key)!r}"
            assert not os.path.exists(key), f"{key} must not be created as a directory"

    def test_idempotent_when_dirs_already_exist(self, tmp_path):
        _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        assert len(result) == len(
            EXPECTED_PATH_KEYS
            | NON_PATH_KEYS
            | COMMON_PATH_KEYS
            | PER_WORKSPACE_UNCREATED_PATH_KEYS
        )

    def test_npm_key_is_lowercase(self, tmp_path):
        """npm only reads env vars matching the lowercase pattern npm_config_<key>."""
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        assert "npm_config_cache" in result
        assert "NPM_CONFIG_CACHE" not in result

    def test_xdg_base_dirs_set(self, tmp_path):
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        cache_root = tmp_path / "caches" / "ws-01-1"
        assert result["XDG_CACHE_HOME"] == str(cache_root / ".cache")
        assert result["XDG_DATA_HOME"] == str(cache_root / ".local" / "share")

    def test_uv_python_install_dir_under_data_home(self, tmp_path):
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        assert result["UV_PYTHON_INSTALL_DIR"].startswith(result["XDG_DATA_HOME"])

    def test_workspace_name_used_as_cache_subdirectory(self, tmp_path):
        """Different workspaces get isolated cache directories."""
        result_a = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        result_b = _call_with_mock_base(str(tmp_path / "ws-01-2"), tmp_path)
        assert "ws-01-1" in result_a["XDG_CACHE_HOME"]
        assert "ws-01-2" in result_b["XDG_CACHE_HOME"]
        assert result_a["XDG_CACHE_HOME"] != result_b["XDG_CACHE_HOME"]

    def test_android_sdk_root_is_common_not_per_workspace(self, tmp_path):
        """ANDROID_SDK_ROOT must point to the shared common path, identical across workspaces."""
        result_a = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        result_b = _call_with_mock_base(str(tmp_path / "ws-01-2"), tmp_path)
        common = str(tmp_path / "caches" / "common" / "android")
        assert result_a["ANDROID_SDK_ROOT"] == common
        assert result_b["ANDROID_SDK_ROOT"] == common

    def test_android_sdk_root_not_created_by_makedirs(self, tmp_path):
        """ANDROID_SDK_ROOT is owned by init-mobile-sdk.sh; this function must not create it."""
        _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        sdk_root = tmp_path / "caches" / "common" / "android"
        assert not sdk_root.exists(), "ANDROID_SDK_ROOT must not be auto-created by setup_workspace_cache_directories"

    def test_android_home_matches_sdk_root(self, tmp_path):
        """ANDROID_HOME and ANDROID_SDK_ROOT must resolve to the same shared path.

        Tools read one or the other; a divergence (e.g. a stale Dockerfile ENV) would point
        Gradle/AGP at a different location than the one init-mobile-sdk.sh populated.
        """
        result = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        assert result["ANDROID_HOME"] == result["ANDROID_SDK_ROOT"]

    def test_android_home_not_created_by_makedirs(self, tmp_path):
        """ANDROID_HOME shares the SDK root; this function must not create it either."""
        _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        assert not (tmp_path / "caches" / "common" / "android").exists()

    def test_flutter_root_is_per_workspace_and_not_created_by_makedirs(self, tmp_path):
        """FLUTTER_ROOT must be per-workspace and not auto-created — the wrapper owns provisioning.

        A shared FLUTTER_ROOT would race because Flutter self-mutates bin/cache at runtime.
        """
        result_a = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        result_b = _call_with_mock_base(str(tmp_path / "ws-01-2"), tmp_path)
        assert result_a["FLUTTER_ROOT"] == str(tmp_path / "caches" / "ws-01-1" / "flutter")
        assert result_b["FLUTTER_ROOT"] == str(tmp_path / "caches" / "ws-01-2" / "flutter")
        assert result_a["FLUTTER_ROOT"] != result_b["FLUTTER_ROOT"]
        assert not (tmp_path / "caches" / "ws-01-1" / "flutter").exists()

    def test_pub_cache_is_per_workspace(self, tmp_path):
        """PUB_CACHE must be per-workspace so Dart/Flutter package pulls stay isolated."""
        result_a = _call_with_mock_base(str(tmp_path / "ws-01-1"), tmp_path)
        result_b = _call_with_mock_base(str(tmp_path / "ws-01-2"), tmp_path)
        assert "ws-01-1" in result_a["PUB_CACHE"]
        assert "ws-01-2" in result_b["PUB_CACHE"]
        assert result_a["PUB_CACHE"] != result_b["PUB_CACHE"]


# ---------------------------------------------------------------------------
# clear_workspace_caches
# ---------------------------------------------------------------------------

def _make_cache_root(base: Path, ws_id: str) -> Path:
    """Create a non-empty cache dir and return its path."""
    cache_root = base / "caches" / ws_id
    (cache_root / "pip" / "http").mkdir(parents=True)
    (cache_root / "pip" / "http" / "somefile.bin").write_bytes(b"data")
    return cache_root


class TestClearWorkspaceCaches:
    def _call(self, workspace_ids, base_path):
        mock_settings = MagicMock()
        mock_settings.WORKSPACE_BASE_PATH = str(base_path)
        with patch("app.services.claude_code.settings", mock_settings):
            asyncio.run(clear_workspace_caches(workspace_ids))

    def test_removes_existing_cache_dirs(self, tmp_path):
        cache_root = _make_cache_root(tmp_path, "ws-01-1")
        assert cache_root.exists()
        self._call(["ws-01-1"], tmp_path)
        assert not cache_root.exists()

    def test_clears_multiple_workspaces(self, tmp_path):
        roots = [_make_cache_root(tmp_path, ws) for ws in ("ws-01-1", "ws-01-2", "ws-01-3")]
        self._call(["ws-01-1", "ws-01-2", "ws-01-3"], tmp_path)
        for root in roots:
            assert not root.exists()

    def test_no_error_when_cache_dir_missing(self, tmp_path):
        """Non-fatal: missing cache directory should not raise."""
        self._call(["ws-01-1"], tmp_path)  # dir was never created

    def test_sibling_workspace_cache_untouched(self, tmp_path):
        """Only the targeted workspace cache is removed; others survive."""
        _make_cache_root(tmp_path, "ws-01-1")
        kept = _make_cache_root(tmp_path, "ws-01-2")
        self._call(["ws-01-1"], tmp_path)
        assert kept.exists()

    def test_empty_list_is_noop(self, tmp_path):
        _make_cache_root(tmp_path, "ws-01-1")
        self._call([], tmp_path)
        assert (tmp_path / "caches" / "ws-01-1").exists()


# ---------------------------------------------------------------------------
# CLAUDE_CODE_TMPDIR_PATH config default
# ---------------------------------------------------------------------------

class TestClaudeCodeTmpdirConfig:
    """CLAUDE_CODE_TMPDIR_PATH default is derived from WORKSPACE_BASE_PATH, not hardcoded."""

    def test_default_is_under_workspace_base_path(self):
        from app.core.config import Settings
        s = Settings()
        assert s.CLAUDE_CODE_TMPDIR_PATH.startswith(s.WORKSPACE_BASE_PATH)

    def test_default_dirname_is_claude_code_tmpdir(self):
        from app.core.config import Settings
        s = Settings()
        assert Path(s.CLAUDE_CODE_TMPDIR_PATH).name == "claude_code_tmpdir"

    def test_custom_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_TMPDIR_PATH", "/custom/tmpdir")
        from app.core.config import Settings
        s = Settings()
        assert s.CLAUDE_CODE_TMPDIR_PATH == "/custom/tmpdir"

    def test_tmpdir_is_child_of_workspace_base_path(self):
        """CLAUDE_CODE_TMPDIR_PATH is a direct child of WORKSPACE_BASE_PATH."""
        from app.core.config import Settings
        s = Settings()
        assert Path(s.CLAUDE_CODE_TMPDIR_PATH).parent == Path(s.WORKSPACE_BASE_PATH)


# ---------------------------------------------------------------------------
# CLAUDE_CODE_TMPDIR injected into agent env
# ---------------------------------------------------------------------------

class TestSetupClaudeCodeTmpdir:
    """setup_claude_code_tmpdir creates the dir and returns the CLAUDE_CODE_TMPDIR env var."""

    def test_returns_claude_code_tmpdir_key(self, tmp_path):
        tmpdir_path = str(tmp_path / "claude_code_tmpdir")
        with patch("app.services.claude_code.settings") as mock_settings:
            mock_settings.CLAUDE_CODE_TMPDIR_PATH = tmpdir_path
            result = setup_claude_code_tmpdir()
        assert "CLAUDE_CODE_TMPDIR" in result
        assert result["CLAUDE_CODE_TMPDIR"] == tmpdir_path

    def test_creates_directory_on_disk(self, tmp_path):
        tmpdir_path = str(tmp_path / "claude_code_tmpdir")
        with patch("app.services.claude_code.settings") as mock_settings:
            mock_settings.CLAUDE_CODE_TMPDIR_PATH = tmpdir_path
            setup_claude_code_tmpdir()
        assert os.path.isdir(tmpdir_path)

    def test_idempotent_when_dir_already_exists(self, tmp_path):
        tmpdir_path = str(tmp_path / "claude_code_tmpdir")
        os.makedirs(tmpdir_path)
        with patch("app.services.claude_code.settings") as mock_settings:
            mock_settings.CLAUDE_CODE_TMPDIR_PATH = tmpdir_path
            result = setup_claude_code_tmpdir()  # must not raise
        assert result["CLAUDE_CODE_TMPDIR"] == tmpdir_path


# ---------------------------------------------------------------------------
# setup_claude_code_max_output_tokens
# ---------------------------------------------------------------------------

class TestSetupClaudeCodeMaxOutputTokens:
    """setup_claude_code_max_output_tokens returns the correct env var."""

    def _call(self, max_tokens):
        mock_settings = MagicMock()
        mock_settings.CLAUDE_CODE_MAX_OUTPUT_TOKENS = max_tokens
        with patch("app.services.claude_code.settings", mock_settings):
            return setup_claude_code_max_output_tokens()

    def test_returns_env_var_when_configured(self):
        result = self._call(60000)
        assert result == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "60000"}

    def test_returns_empty_dict_when_none(self):
        result = self._call(None)
        assert result == {}

    def test_value_is_string(self):
        result = self._call(32000)
        assert isinstance(result.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"), str)

    def test_default_setting_is_60000(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_MAX_OUTPUT_TOKENS", raising=False)
        from app.core.config import Settings
        s = Settings()
        assert s.CLAUDE_CODE_MAX_OUTPUT_TOKENS == 60000


# ---------------------------------------------------------------------------
# ModelTokenUsage.from_sdk_usage
# ---------------------------------------------------------------------------

class TestSdkUsageToModelTokenUsage:
    """from_sdk_usage maps SDK usage dict + num_turns to aggregate ModelTokenUsage."""

    def test_maps_all_fields(self):
        usage = {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 100,
        }
        mu = ModelTokenUsage.from_sdk_usage(usage, num_turns=5)
        assert mu.model_name == FLAT_AGGREGATE_MODEL_NAME
        assert mu.num_turns == 5
        assert mu.input_tokens == 1000
        assert mu.output_tokens == 500
        assert mu.cache_write_tokens == 200
        assert mu.cache_read_tokens == 100

    def test_missing_cache_keys_default_to_zero(self):
        mu = ModelTokenUsage.from_sdk_usage(
            {"input_tokens": 300, "output_tokens": 100}, num_turns=1
        )
        assert mu.cache_write_tokens == 0
        assert mu.cache_read_tokens == 0

    def test_empty_usage_returns_zero_model_usage(self):
        mu = ModelTokenUsage.from_sdk_usage({}, num_turns=0)
        assert mu.total_tokens == 0
        assert mu.num_turns == 0


# ---------------------------------------------------------------------------
# build_redacted_env_overlay — prevents pod secrets from leaking into the
# Claude Code agent subprocess. See docs/agents/env-vars-leak.md.
# ---------------------------------------------------------------------------

class TestBuildRedactedEnvOverlay:
    """Overlay shadows secret-looking env vars from the agent subprocess."""

    def test_settings_backed_secrets_are_redacted(self):
        """Every Settings field whose name matches the heuristic is overlaid as redacted."""
        overlay = build_redacted_env_overlay()
        # Representative secret-backed Settings fields — see app/core/config.py.
        for field in (
            "ANTHROPIC_API_KEY",
            "OPENROUTER_API_KEY",
            "P10Y_API_KEY",
            "FIGMA_API_KEY",
            "FIGMA_ACCESS_TOKEN",
            "POSTHOG_API_KEY",
            "GITHUB_TOKEN_DEFAULT",
            "TOKEN_ENCRYPTION_KEY",
            "NOTIFY_EMAIL_PASSWORD",
        ):
            assert overlay.get(field) == REDACTED_PLACEHOLDER, (
                f"{field} should be redacted in overlay"
            )

    def test_blind_mask_redacts_unknown_secret_named_env_var(self, monkeypatch):
        """A pod env var the codebase doesn't know about — but whose name matches the
        heuristic — must still be redacted. This is the core leak prevention."""
        canary_name = "SOME_VENDOR_INTEGRATION_SECRET"
        canary_value = "live-prod-credential-xyz"
        monkeypatch.setenv(canary_name, canary_value)
        overlay = build_redacted_env_overlay()
        assert overlay[canary_name] == REDACTED_PLACEHOLDER
        assert canary_value not in overlay.values()

    def test_heuristic_matches_token_key_secret_password_credential(self, monkeypatch):
        secret_names = (
            "FOO_TOKEN",
            "BAR_KEY",
            "BAZ_SECRET",
            "QUX_PASSWORD",
            "MY_CREDENTIAL",
            "lowercase_api_key",  # case-insensitive
        )
        for name in secret_names:
            monkeypatch.setenv(name, "v")
        overlay = build_redacted_env_overlay()
        for name in secret_names:
            assert overlay[name] == REDACTED_PLACEHOLDER

    def test_non_secret_names_not_in_overlay(self, monkeypatch):
        """Variables that don't match the heuristic must not be added to the overlay
        (so they pass through from os.environ unchanged via the SDK merge)."""
        # Use a name that's definitely not in Settings and doesn't match the heuristic.
        monkeypatch.setenv("MY_BENIGN_HOSTNAME", "example.com")
        overlay = build_redacted_env_overlay()
        assert "MY_BENIGN_HOSTNAME" not in overlay

    def test_simulated_sdk_merge_masks_canary_secret(self, monkeypatch):
        """End-to-end behavioral check: simulate the SDK's
        ``{**os.environ, **options.env}`` merge and confirm a pod secret value
        is not observable to the child process."""
        canary_name = "PROD_BILLING_API_KEY"
        canary_value = "sk-live-do-not-leak"
        monkeypatch.setenv(canary_name, canary_value)

        overlay = build_redacted_env_overlay()
        env_config = {"ANTHROPIC_API_KEY": "real-provider-value"}
        agent_env = {**overlay, **env_config}

        # This mirrors claude_agent_sdk's SubprocessCLITransport.connect merge.
        child_env = {**os.environ, **agent_env}

        assert child_env[canary_name] == REDACTED_PLACEHOLDER
        assert canary_value not in child_env.values()

    def test_gh_token_in_env_config_wins_over_pod_github_token_mask(self, monkeypatch):
        """Deploy agents inject GH_TOKEN via env_config; it must override blind-masked pod GITHUB_TOKEN."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_pod_token_should_not_reach_gh")
        monkeypatch.setenv("GH_TOKEN", "ghp_pod_gh_token_also_masked")

        overlay = build_redacted_env_overlay()
        assert overlay["GITHUB_TOKEN"] == REDACTED_PLACEHOLDER
        assert overlay["GH_TOKEN"] == REDACTED_PLACEHOLDER

        generation_pat = "ghp_generation_scoped_for_deploy"
        env_config = {"GH_TOKEN": generation_pat}
        agent_env = {**overlay, **env_config}
        child_env = {**os.environ, **agent_env}

        assert child_env["GH_TOKEN"] == generation_pat
        assert child_env["GITHUB_TOKEN"] == REDACTED_PLACEHOLDER

    def test_env_config_real_values_win_over_redaction(self, monkeypatch):
        """Provider/runtime credentials passed via env_config must NOT be redacted.
        The merge order ``{**overlay, **env_config}`` guarantees env_config wins."""
        # ANTHROPIC_API_KEY is a Settings-backed secret name and would be in the overlay.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "pod-value-should-be-shadowed")

        overlay = build_redacted_env_overlay()
        assert overlay["ANTHROPIC_API_KEY"] == REDACTED_PLACEHOLDER

        # Provider injects the real value via env_config.
        env_config = {"ANTHROPIC_API_KEY": "intentional-real-value"}
        agent_env = {**overlay, **env_config}

        assert agent_env["ANTHROPIC_API_KEY"] == "intentional-real-value"

    def test_workspace_cache_keys_pass_through_unredacted(self, tmp_path, monkeypatch):
        """Cache/path env vars (no secret-name match) must not be touched by overlay."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        overlay = build_redacted_env_overlay()
        assert "XDG_CACHE_HOME" not in overlay
        assert "CLAUDE_CODE_TMPDIR" not in overlay


# ---------------------------------------------------------------------------
# agent_query integration — confirms agent subprocess sees redacted env.
# ---------------------------------------------------------------------------

class TestAgentQueryEnvRedaction:
    """agent_query must hand ClaudeAgentOptions a redaction-shadowed env."""

    def test_agent_query_options_env_redacts_pod_secret(self, tmp_path, monkeypatch):
        """Drive agent_query far enough to capture the ClaudeAgentOptions it builds,
        and verify the env it passes redacts a canary pod secret."""
        canary_name = "CUSTOMER_ACME_API_TOKEN"
        canary_value = "leaked-credential"
        monkeypatch.setenv(canary_name, canary_value)
        # SKIP_AGENT_EXECUTION would short-circuit before options are built; ensure off.
        monkeypatch.delenv("SKIP_AGENT_EXECUTION", raising=False)

        captured = {}

        class _FakeOptions:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.__dict__.update(kwargs)

        def _fake_query(prompt, options):
            captured["__queried__"] = True

            async def _empty():
                if False:
                    yield None

            return _empty()

        async def _fake_process(stream, logger, metrics, lf_tracer=None, stream_publisher=None):
            return []

        def _fake_validate(options, logger):
            return None

        mock_settings = MagicMock()
        mock_settings.WORKSPACE_BASE_PATH = str(tmp_path)
        mock_settings.CLAUDE_CODE_TMPDIR_PATH = str(tmp_path / "cctmp")
        mock_settings.CLAUDE_CODE_MAX_OUTPUT_TOKENS = None
        # Settings.model_fields is read by build_redacted_env_overlay; keep the real one.
        # build_redacted_env_overlay reads model_fields off type(settings).

        monkeypatch.setattr(cc, "settings", mock_settings)
        monkeypatch.setattr(cc, "ClaudeAgentOptions", _FakeOptions)
        monkeypatch.setattr(cc, "query", _fake_query)
        monkeypatch.setattr(cc, "process_query_stream", _fake_process)
        monkeypatch.setattr(cc, "validate_models_and_tools", _fake_validate)

        asyncio.run(cc.agent_query(
            system_prompt="probe",
            workspace_path=str(tmp_path / "ws-1"),
            model="claude-haiku-4-5",
        ))

        assert captured.get("__queried__") is True
        env = captured["env"]
        assert env[canary_name] == REDACTED_PLACEHOLDER
        assert canary_value not in env.values()
