"""Tests for the settings read/write helper (tui/config.py)."""

import json

from services.llm_tiers import LLM_TIER_KEYS
from tui import config


def _write_config(root, doc):
    path = root / ".specflow-local" / "mcp-config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


class TestEditableKeys:
    def test_uses_the_real_tier_keys_not_the_dead_llm_model_names(self):
        assert config.EDITABLE_KEYS == [
            "WORKSPACE_COUNT",
            "LLM_HIGH",
            "LLM_MEDIUM",
            "LLM_LOW",
            "USER_EMAIL",
            "BACKEND_URL",
        ]
        assert not any(k.startswith("LLM_MODEL_") for k in config.EDITABLE_KEYS)

    def test_backend_runtime_is_not_editable_and_is_purged(self):
        # Runtime isn't an mcp-config setting (see cli.resolve_backend_runtime), so
        # it must not be shown in Settings, and a stale value is swept on save.
        assert "BACKEND_RUNTIME" not in config.EDITABLE_KEYS
        assert "BACKEND_RUNTIME" in config._LEGACY_EDITABLE_KEYS

    def test_tier_slice_is_the_llm_tiers_ssot(self):
        # The tier entries must BE the shared list the MCP server/backend read.
        assert config.EDITABLE_KEYS[1:4] == list(LLM_TIER_KEYS)


class TestLoadEnv:
    def test_reads_env_block(self, tmp_path):
        _write_config(tmp_path, {"mcpServers": {"specflow": {"env": {"USER_EMAIL": "a@b.c"}}}})
        assert config.load_env(tmp_path)["USER_EMAIL"] == "a@b.c"

    def test_missing_file_returns_empty(self, tmp_path):
        assert config.load_env(tmp_path) == {}


class TestSaveEnv:
    def test_creates_file_when_absent(self, tmp_path):
        path = config.save_env(tmp_path, {"WORKSPACE_COUNT": "3"})
        doc = json.loads(path.read_text())
        assert doc["mcpServers"]["specflow"]["env"]["WORKSPACE_COUNT"] == "3"

    def test_preserves_other_keys(self, tmp_path):
        _write_config(
            tmp_path,
            {
                "mcpServers": {
                    "specflow": {
                        "command": "uvx",
                        "args": ["--from", "x", "specflow-mcp"],
                        "env": {"BACKEND_URL": "http://127.0.0.1:8000", "KEEP_ME": "yes"},
                    }
                }
            },
        )
        config.save_env(tmp_path, {"WORKSPACE_COUNT": "2"})
        doc = json.loads((tmp_path / ".specflow-local" / "mcp-config.json").read_text())
        specflow = doc["mcpServers"]["specflow"]
        # Non-editable command/args and unrelated env entries are preserved.
        assert specflow["command"] == "uvx"
        assert specflow["env"]["KEEP_ME"] == "yes"
        assert specflow["env"]["WORKSPACE_COUNT"] == "2"

    def test_drops_empty_values(self, tmp_path):
        config.save_env(tmp_path, {"WORKSPACE_COUNT": "3", "USER_EMAIL": ""})
        env = config.load_env(tmp_path)
        assert "WORKSPACE_COUNT" in env
        assert "USER_EMAIL" not in env

    def test_clearing_a_key_removes_it(self, tmp_path):
        config.save_env(tmp_path, {"USER_EMAIL": "a@b.c"})
        config.save_env(tmp_path, {"USER_EMAIL": ""})
        assert "USER_EMAIL" not in config.load_env(tmp_path)

    def test_purges_dead_legacy_tier_keys(self, tmp_path):
        # A block written by the buggy build carries LLM_MODEL_* nothing reads;
        # saving must sweep them so they never linger or skew a fingerprint.
        _write_config(
            tmp_path,
            {"mcpServers": {"specflow": {"env": {"LLM_MODEL_HIGH": "old/model", "KEEP": "yes"}}}},
        )
        config.save_env(tmp_path, {"LLM_HIGH": "new/model"})
        env = config.load_env(tmp_path)
        assert "LLM_MODEL_HIGH" not in env
        assert env["LLM_HIGH"] == "new/model"
        assert env["KEEP"] == "yes"  # unrelated entries still preserved

    def test_purges_stale_backend_runtime(self, tmp_path):
        # A BACKEND_RUNTIME left in the env block by an older build is dead (the
        # runtime is resolved elsewhere); saving must sweep it out.
        _write_config(
            tmp_path,
            {"mcpServers": {"specflow": {"env": {"BACKEND_RUNTIME": "process", "KEEP": "yes"}}}},
        )
        config.save_env(tmp_path, {"WORKSPACE_COUNT": "2"})
        env = config.load_env(tmp_path)
        assert "BACKEND_RUNTIME" not in env
        assert env["KEEP"] == "yes"


class TestLangfuse:
    def test_secret_key_is_masked_but_public_and_host_are_not(self):
        assert "LANGFUSE_SECRET_KEY" in config.MASKED_KEYS
        assert "LANGFUSE_PUBLIC_KEY" not in config.MASKED_KEYS
        assert "LANGFUSE_BASE_URL" not in config.MASKED_KEYS

    def test_langfuse_keys_kept_out_of_core_secret_keys(self):
        # Advanced/optional keys must not leak into the required-core set.
        assert not (set(config.LANGFUSE_KEYS) & set(config.ENV_SECRET_KEYS))

    def test_none_present_is_ok(self):
        assert config.langfuse_partial_error({}) is None

    def test_all_three_present_is_ok(self):
        assert config.langfuse_partial_error(
            {
                "LANGFUSE_PUBLIC_KEY": "pk",
                "LANGFUSE_SECRET_KEY": "sk",
                "LANGFUSE_BASE_URL": "https://lf",
            }
        ) is None

    def test_partial_is_rejected_and_names_missing(self):
        error = config.langfuse_partial_error({"LANGFUSE_PUBLIC_KEY": "pk"})
        assert error is not None
        assert "LANGFUSE_SECRET_KEY" in error
        assert "LANGFUSE_BASE_URL" in error

    def test_whitespace_only_counts_as_absent(self):
        assert config.langfuse_partial_error(
            {"LANGFUSE_PUBLIC_KEY": "  ", "LANGFUSE_SECRET_KEY": "", "LANGFUSE_BASE_URL": ""}
        ) is None


class TestEnvSecrets:
    def test_round_trip(self, tmp_path):
        config.save_env_secrets(tmp_path, {"P10Y_API_KEY": "secret", "GITHUB_ORG": "acme"})
        loaded = config.load_env_secrets(tmp_path)
        assert loaded["P10Y_API_KEY"] == "secret"
        assert loaded["GITHUB_ORG"] == "acme"

    def test_partial_update_preserves_other_secrets(self, tmp_path):
        config.save_env_secrets(tmp_path, {"P10Y_API_KEY": "k1", "GITHUB_TOKEN_DEFAULT": "t1"})
        config.save_env_secrets(tmp_path, {"GITHUB_TOKEN_DEFAULT": "t2"})
        loaded = config.load_env_secrets(tmp_path)
        assert loaded["GITHUB_TOKEN_DEFAULT"] == "t2"
        assert loaded["P10Y_API_KEY"] == "k1"  # untouched key preserved

    def test_secret_and_runtime_stores_are_separate(self, tmp_path):
        # Secrets land in .env; runtime keys land in mcp-config.json — never mixed.
        config.save_env_secrets(tmp_path, {"P10Y_API_KEY": "k"})
        config.save_env(tmp_path, {"WORKSPACE_COUNT": "3"})
        assert "P10Y_API_KEY" not in config.load_env(tmp_path)
        assert "WORKSPACE_COUNT" not in config.load_env_secrets(tmp_path)
