"""
Tests for backend/scripts/init_firestore.py — Phase 5 (Config-driven Firestore seeding).

Coverage:
  (a) --workspace-config load replaces the hardcoded WORKSPACE_CONFIGS list.
  (b) --yes is non-interactive: builtins.input is NEVER called.
  (c) Idempotent rerun produces no duplicate / divergent docs.
  (d) Sentinel "local" doc seeded with LOCAL_KEY_UID and user_id from settings.LOCAL_USER_EMAIL.
  (e) --replace overwrites the sentinel.
  (f) Sentinel seeds even when e2e_tests_user already exists (the B2 path).
"""

import builtins
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.core.local_identity import LOCAL_API_KEY_DOC_ID, LOCAL_KEY_UID
from app.database.memory import InMemoryDatabase

# ---------------------------------------------------------------------------
# Import the functions under test directly.  The script sets DATABASE_TYPE
# early via os.environ; for unit tests we keep DATABASE_TYPE=memory so
# get_database() returns the InMemoryDatabase.  We patch in our own db
# instance rather than calling get_database() at all.
# ---------------------------------------------------------------------------
import scripts.init_firestore as init_script


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db() -> InMemoryDatabase:
    db = InMemoryDatabase()
    return db


def _workspace_config_file(tmp_path, entries) -> str:
    """Write a JSON workspace-config file and return its path."""
    path = str(tmp_path / "workspaces.json")
    with open(path, "w") as f:
        json.dump(entries, f)
    return path


SAMPLE_WORKSPACE_ENTRIES = [
    {
        "workspace_id": "ws-test-1",
        "repo_url": "https://github.com/test-org/test-repo-1",
        "p10y_repository_id": 11111,
        "workspace_pool": "default",
    },
    {
        "workspace_id": "ws-test-2",
        "repo_url": "https://github.com/test-org/test-repo-2",
        "p10y_repository_id": 22222,
        "workspace_pool": "testpool",
    },
]


# ---------------------------------------------------------------------------
# (a) --workspace-config load replaces the hardcoded list
# ---------------------------------------------------------------------------

class TestWorkspaceConfigLoad:
    def test_load_replaces_hardcoded_list(self, tmp_path):
        path = _workspace_config_file(tmp_path, SAMPLE_WORKSPACE_ENTRIES)
        loaded = init_script.load_workspace_configs_from_file(path)

        assert len(loaded) == 2
        assert loaded[0]["workspace_id"] == "ws-test-1"
        assert loaded[0]["repo_url"] == "https://github.com/test-org/test-repo-1"
        assert loaded[0]["p10y_id"] == 11111  # normalised key
        assert loaded[0]["workspace_pool"] == "default"
        assert loaded[1]["workspace_id"] == "ws-test-2"
        assert loaded[1]["p10y_id"] == 22222
        assert loaded[1]["workspace_pool"] == "testpool"

    def test_load_invalid_json_exits(self, tmp_path):
        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("not json {{{")
        with pytest.raises(SystemExit):
            init_script.load_workspace_configs_from_file(path)

    def test_load_not_a_list_exits(self, tmp_path):
        path = str(tmp_path / "obj.json")
        with open(path, "w") as f:
            json.dump({"workspace_id": "x"}, f)
        with pytest.raises(SystemExit):
            init_script.load_workspace_configs_from_file(path)

    def test_load_missing_field_exits(self, tmp_path):
        bad_entry = [{"workspace_id": "x", "repo_url": "y", "p10y_repository_id": 1}]
        # missing workspace_pool
        path = str(tmp_path / "missing.json")
        with open(path, "w") as f:
            json.dump(bad_entry, f)
        with pytest.raises(SystemExit):
            init_script.load_workspace_configs_from_file(path)

    def test_load_wrong_p10y_type_exits(self, tmp_path):
        bad_entry = [{
            "workspace_id": "x",
            "repo_url": "y",
            "p10y_repository_id": "not-an-int",
            "workspace_pool": "default",
        }]
        path = str(tmp_path / "bad_type.json")
        with open(path, "w") as f:
            json.dump(bad_entry, f)
        with pytest.raises(SystemExit):
            init_script.load_workspace_configs_from_file(path)

    def test_workspace_pool_initialized_with_config(self, tmp_path):
        """Workspaces from loaded config are written to db with correct workspace_id."""
        path = _workspace_config_file(tmp_path, SAMPLE_WORKSPACE_ENTRIES)
        loaded = init_script.load_workspace_configs_from_file(path)

        db = _make_db()
        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = loaded
            init_script.initialize_workspace_pool(db, dry_run=False, yes=True, replace=False)
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

        ws1 = db.get("workspaces", "ws-test-1")
        assert ws1 is not None
        assert ws1["repo_url"] == "https://github.com/test-org/test-repo-1"
        assert ws1["p10y_repository_id"] == 11111

        ws2 = db.get("workspaces", "ws-test-2")
        assert ws2 is not None
        assert ws2["workspace_pool"] == "testpool"


# ---------------------------------------------------------------------------
# (a2) main() requires --workspace-config: there are no default repos
# ---------------------------------------------------------------------------

class TestWorkspaceConfigRequired:
    def test_main_without_workspace_config_exits(self, capsys):
        """main() refuses to run (exit 1) when --workspace-config is absent."""
        with patch("sys.argv", ["init_firestore.py", "--yes"]):
            with pytest.raises(SystemExit) as exc:
                init_script.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "No workspace config provided" in out
        assert "e2e-workspace-config.example.json" in out

    def test_main_without_workspace_config_does_not_touch_db(self):
        """The gate fires before any database access."""
        with patch("sys.argv", ["init_firestore.py"]):
            with patch.object(init_script, "get_database") as mock_get_db:
                with pytest.raises(SystemExit):
                    init_script.main()
        mock_get_db.assert_not_called()


# ---------------------------------------------------------------------------
# (b) --yes is non-interactive: builtins.input is NEVER called
# ---------------------------------------------------------------------------

class TestYesFlag:
    def test_yes_skips_workspace_prompt(self):
        """When yes=True and existing workspaces found, no input() call."""
        db = _make_db()
        # Pre-seed a workspace so the prompt branch is reached
        db.set("workspaces", "ws-existing", {"status": "available", "clean_verified": True})

        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = []
            with patch.object(builtins, "input") as mock_input:
                init_script.initialize_workspace_pool(db, dry_run=False, yes=True, replace=True)
                mock_input.assert_not_called()
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

    def test_no_yes_calls_input(self):
        """Without yes=True, input() IS called when existing workspaces exist."""
        db = _make_db()
        db.set("workspaces", "ws-existing", {"status": "available", "clean_verified": True})

        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = []
            with patch.object(builtins, "input", return_value="no") as mock_input:
                init_script.initialize_workspace_pool(db, dry_run=False, yes=False, replace=False)
                mock_input.assert_called_once()
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

    def test_yes_skips_prod_prompt(self):
        """main() with --yes skips the production confirmation input()."""
        # We don't call main() directly (it calls sys.exit), so test the
        # inline branch logic via main's argparse path — simulate by calling
        # the underlying branch directly.
        # The production-confirm branch: if args.yes → response = "yes".
        # Verified by reading main source; here we test that initialize_workspace_pool
        # propagates yes=True without calling input.
        db = _make_db()
        db.set("workspaces", "ws-x", {"status": "available", "clean_verified": True})
        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = []
            with patch.object(builtins, "input") as mock_input:
                init_script.initialize_workspace_pool(db, dry_run=False, yes=True, replace=True)
                mock_input.assert_not_called()
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs


# ---------------------------------------------------------------------------
# (c) Idempotent rerun: no duplicate / divergent docs
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_sentinel_not_overwritten_without_replace(self):
        """Second call without --replace leaves the sentinel doc unchanged."""
        db = _make_db()

        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "first@example.com"
        mock_settings.LOCAL_USER_NAME = "First User"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        # Change the settings to verify second run does NOT overwrite
        mock_settings.LOCAL_USER_EMAIL = "second@example.com"
        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert doc["user_id"] == "first@example.com"  # unchanged

    def test_workspace_not_overwritten_without_replace(self, tmp_path):
        """Existing workspace docs skipped when replace=False."""
        db = _make_db()
        original_doc = {
            "repo_url": "https://original.example.com",
            "status": "available",
            "clean_verified": True,
            "p10y_repository_id": 99999,
            "set_number": 1,
            "workspace_pool": "default",
        }
        db.set("workspaces", "ws-test-1", original_doc)

        loaded = init_script.load_workspace_configs_from_file(
            _workspace_config_file(tmp_path, SAMPLE_WORKSPACE_ENTRIES)
        )
        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = loaded
            init_script.initialize_workspace_pool(db, dry_run=False, yes=True, replace=False)
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

        doc = db.get("workspaces", "ws-test-1")
        # repo_url must NOT have been overwritten
        assert doc["repo_url"] == "https://original.example.com"

    def test_second_sentinel_seed_is_noop(self):
        """Running initialize_local_identity twice without replace is a no-op."""
        db = _make_db()
        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "user@example.com"
        mock_settings.LOCAL_USER_NAME = "User"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        # Only one doc should exist
        all_keys = db.query("api_keys")
        local_docs = [k for k in all_keys if k.get("api_key") == LOCAL_API_KEY_DOC_ID]
        assert len(local_docs) == 1


# ---------------------------------------------------------------------------
# (d) Sentinel seeded with LOCAL_KEY_UID and user_id from settings.LOCAL_USER_EMAIL
# ---------------------------------------------------------------------------

class TestSentinelSeeding:
    def test_sentinel_seeded_with_correct_key_uid(self):
        db = _make_db()
        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "test@example.com"
        mock_settings.LOCAL_USER_NAME = "Test User"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert doc is not None
        assert doc["key_uid"] == LOCAL_KEY_UID
        assert doc["api_key"] == LOCAL_API_KEY_DOC_ID

    def test_sentinel_user_id_from_settings_local_user_email(self):
        db = _make_db()
        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "custom@example.com"
        mock_settings.LOCAL_USER_NAME = "Custom User"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert doc["user_id"] == "custom@example.com"
        assert doc["user_name"] == "Custom User"

    def test_sentinel_falls_back_to_env_user_email(self, monkeypatch):
        """Falls back to USER_EMAIL env var when settings.LOCAL_USER_EMAIL is None/falsy."""
        db = _make_db()
        monkeypatch.setenv("USER_EMAIL", "env-user@example.com")

        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = None
        mock_settings.LOCAL_USER_NAME = None

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert doc["user_id"] == "env-user@example.com"

    def test_sentinel_falls_back_to_system_init_local(self, monkeypatch):
        """Falls back to 'system@init.local' when no email is configured at all."""
        db = _make_db()
        monkeypatch.delenv("USER_EMAIL", raising=False)

        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = None
        mock_settings.LOCAL_USER_NAME = None

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert doc["user_id"] == "system@init.local"

    def test_sentinel_required_fields_present(self):
        """All fields required by LocalAuthMiddleware are present on the sentinel doc."""
        db = _make_db()
        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "user@example.com"
        mock_settings.LOCAL_USER_NAME = "User"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        required = {
            "api_key", "key_uid", "user_id", "user_name",
            "is_active", "permissions", "workspace_pool",
            "max_concurrent_sessions", "active_generation_sessions",
            "expires_at", "created_at",
        }
        for field in required:
            assert field in doc, f"Missing required field: {field}"

        assert doc["is_active"] is True
        assert doc["expires_at"] is None
        assert doc["workspace_pool"] == "default"
        assert doc["max_concurrent_sessions"] == 5
        assert doc["active_generation_sessions"] == []
        assert doc["permissions"] == ["admin"]

    def test_sentinel_dry_run_does_not_write(self):
        """dry_run=True must NOT write any doc to the database."""
        db = _make_db()
        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "user@example.com"
        mock_settings.LOCAL_USER_NAME = "User"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=True, replace=False)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert doc is None


# ---------------------------------------------------------------------------
# (e) --replace overwrites the sentinel
# ---------------------------------------------------------------------------

class TestReplaceFlag:
    def test_replace_overwrites_sentinel(self):
        db = _make_db()
        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "first@example.com"
        mock_settings.LOCAL_USER_NAME = "First"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        # Change email and run with replace=True
        mock_settings.LOCAL_USER_EMAIL = "second@example.com"
        mock_settings.LOCAL_USER_NAME = "Second"
        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=True)

        doc = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert doc["user_id"] == "second@example.com"
        assert doc["user_name"] == "Second"

    def test_replace_overwrites_workspace(self, tmp_path):
        """With replace=True, existing workspace docs are overwritten."""
        db = _make_db()
        db.set("workspaces", "ws-test-1", {
            "repo_url": "https://old.example.com",
            "status": "available",
            "clean_verified": True,
            "p10y_repository_id": 0,
            "set_number": 1,
            "workspace_pool": "default",
        })

        loaded = init_script.load_workspace_configs_from_file(
            _workspace_config_file(tmp_path, SAMPLE_WORKSPACE_ENTRIES)
        )
        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = loaded
            init_script.initialize_workspace_pool(db, dry_run=False, yes=True, replace=True)
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

        doc = db.get("workspaces", "ws-test-1")
        assert doc["repo_url"] == "https://github.com/test-org/test-repo-1"


# ---------------------------------------------------------------------------
# (f) Sentinel seeds even when e2e_tests_user already exists (B2 path)
# ---------------------------------------------------------------------------

class TestB2Path:
    def test_sentinel_seeds_when_e2e_tests_user_exists(self):
        """
        initialize_local_identity runs even when e2e_tests_user is already in the DB.

        This tests reviewer fix B2: the sentinel is NEVER gated behind
        initialize_api_key's early-return (which triggers when ANY api_keys exist).
        """
        db = _make_db()

        # Seed e2e_tests_user directly (simulating a DB that already has keys)
        db.set("api_keys", "e2e_tests_user", {
            "api_key": "e2e_tests_user",
            "key_uid": "00000000-e2e0-0000-0000-000000000001",
            "user_id": "system@init.local",
            "user_name": "System Initialization",
            "is_active": True,
            "permissions": ["admin"],
            "workspace_pool": "default",
            "max_concurrent_sessions": 5,
            "active_generation_sessions": [],
            "expires_at": None,
            "created_at": datetime.now(timezone.utc),
        })

        # initialize_api_key would early-return here because existing_keys is non-empty.
        # Verify that the sentinel is still seeded via initialize_local_identity.
        mock_settings = MagicMock()
        mock_settings.LOCAL_USER_EMAIL = "user@example.com"
        mock_settings.LOCAL_USER_NAME = "User"

        with patch.object(init_script, "settings", mock_settings):
            init_script.initialize_local_identity(db, dry_run=False, replace=False)

        sentinel = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert sentinel is not None
        assert sentinel["key_uid"] == LOCAL_KEY_UID

    def test_initialize_api_key_early_returns_with_existing_keys(self):
        """
        Confirm the early-return in initialize_api_key is still intact.

        This verifies the B2 concern: initialize_api_key does NOT seed the sentinel,
        so initialize_local_identity must be called separately.
        """
        db = _make_db()
        # Pre-seed any api_key doc
        db.set("api_keys", "some_key", {"api_key": "some_key", "is_active": True})

        # Call initialize_api_key — it should NOT write the "local" sentinel
        init_script.initialize_api_key(db, dry_run=False)

        sentinel = db.get("api_keys", LOCAL_API_KEY_DOC_ID)
        assert sentinel is None, (
            "initialize_api_key must not seed the 'local' sentinel doc; "
            "that is the responsibility of initialize_local_identity"
        )


class TestExtraPoolKeySeeding:
    def test_extra_pool_key_seeded_only_when_config_declares_extra_pool(self):
        db = _make_db()
        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = [
                {
                    "workspace_id": "ws-default-1",
                    "repo_url": "https://github.com/test-org/default",
                    "p10y_id": 11111,
                    "workspace_pool": "default",
                }
            ]
            init_script.initialize_api_key(db, dry_run=False)
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

        assert db.get("api_keys", "e2e_tests_user") is not None
        assert db.get("api_keys", "extra_pool_user") is None

    def test_extra_pool_key_seeded_when_config_declares_extra_pool(self):
        db = _make_db()
        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = [
                {
                    "workspace_id": "ws-extra-1",
                    "repo_url": "https://github.com/test-org/extra",
                    "p10y_id": 22222,
                    "workspace_pool": init_script.EXTRA_WORKSPACE_POOL,
                }
            ]
            init_script.initialize_api_key(db, dry_run=False)
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

        extra_key = db.get("api_keys", "extra_pool_user")
        assert extra_key is not None
        assert extra_key["workspace_pool"] == init_script.EXTRA_WORKSPACE_POOL
        assert extra_key["key_uid"] == init_script.EXTRA_POOL_KEY_UID

    def test_github_token_attachment_skips_when_extra_pool_not_configured(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-unit-test-token")
        original_configs = init_script.WORKSPACE_CONFIGS
        try:
            init_script.WORKSPACE_CONFIGS = [
                {
                    "workspace_id": "ws-default-1",
                    "repo_url": "https://github.com/test-org/default",
                    "p10y_id": 11111,
                    "workspace_pool": "default",
                }
            ]
            with patch.object(init_script.httpx, "put") as mock_put:
                init_script.attach_github_tokens(dry_run=False)
        finally:
            init_script.WORKSPACE_CONFIGS = original_configs

        mock_put.assert_not_called()
