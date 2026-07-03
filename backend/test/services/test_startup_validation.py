"""
Tests for StartupValidationService

Covers startup scenarios from docs/deployment/state-management.md:
- Environment validation (including LLM-provider branching per S2.6/S2.7/S2.8)
- Firestore connectivity
- Workspace pool validation
- Filestore mount check
- Concurrent startup safety
"""

import pytest
import os
from unittest.mock import patch

from app.database.memory import InMemoryDatabase
from app.services.workspace_pool import WorkspacePoolService
from app.services.startup_validation import (
    StartupValidator,
    StartupValidationError,
)


@pytest.fixture
def db():
    """Create a fresh in-memory database for each test."""
    database = InMemoryDatabase()
    yield database
    database.clear()


@pytest.fixture
def temp_workspace_dir():
    """Create a temporary directory for workspace operations."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_pool(db, temp_workspace_dir):
    """Create workspace pool service with temporary workspace directory."""
    from unittest.mock import AsyncMock
    
    service = WorkspacePoolService(db, workspace_base_path=temp_workspace_dir)
    
    # Mock git clone operations to avoid actual network calls in tests
    async def mock_ensure_repo_cloned(workspace_id: str, ws_doc: dict, generation_id: str):
        """Mock repository cloning - just create the workspace directory."""
        workspace_path = service._get_workspace_path(workspace_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        # Create a mock .git directory to simulate cloned repo
        (workspace_path / ".git").mkdir(exist_ok=True)
    
    # Replace the method with our mock
    service._ensure_repo_cloned = AsyncMock(side_effect=mock_ensure_repo_cloned)
    
    return service


@pytest.fixture
def validator(db, workspace_pool):
    """Create startup validator."""
    return StartupValidator(db, workspace_pool)


@pytest.fixture
def sample_workspaces(db):
    """Create sample workspaces."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    
    for set_num in [1, 2]:
        for i in range(1, 4):
            ws_id = f"ws-{set_num:02d}-{i}"
            db.set("workspaces", ws_id, {
                "repo_url": f"https://github.com/org/workspace-{set_num}-{i}",
                "p10y_repository_id": 74900 + (set_num * 10) + i,
                "workspace_pool": "default",
                "set_number": set_num,
                "status": "available",
                "locked_by": None,
                "locked_at": None,
                "lease_expires_at": None,
                "clean_verified": True,
                "last_used_by": None,
                "last_cleaned_at": now,
                "allocation_history": [],
                "error": None,
            })


class TestEnvironmentValidation:
    """Test environment variable validation."""
    
    @pytest.mark.asyncio
    async def test_environment_check_passes(self, validator):
        """Environment check passes with OPENROUTER_API_KEY set (provider resolves to openrouter)."""
        from app.core.config import Settings

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "test-key",
            "DATABASE_TYPE": "memory",
        }, clear=True):
            with patch("app.services.startup_validation.settings", Settings(_env_file=None)):
                result = await validator._check_environment()

        assert result["passed"] is True
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_environment_check_fails_missing_vars(self, validator):
        """With no provider key set, the check fails fast naming both provider keys."""
        from app.core.config import Settings

        with patch.dict(os.environ, {}, clear=True):
            with patch("app.services.startup_validation.settings", Settings(_env_file=None)):
                result = await validator._check_environment()

        assert result["passed"] is False
        assert "OPENROUTER_API_KEY" in result["error"]
        assert "ANTHROPIC_API_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_environment_check_firestore_requires_git_secrets(self, validator):
        """Firestore mode requires token encryption + default PAT env unless K8s is configured."""
        # clear=True keeps the test hermetic: it must guarantee the git secrets
        # (and KUBERNETES_SERVICE_HOST) are absent, not rely on ambient env. Some
        # scripts call load_dotenv() at import, which can leak a developer's real
        # secrets into the test process and make a clear=False assertion flaky.
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "k",
            "DATABASE_TYPE": "firestore",
        }, clear=True):
            result = await validator._check_environment()
        assert result["passed"] is False
        assert "TOKEN_ENCRYPTION_KEY" in result["error"]


class TestFirestoreConnectivity:
    """Test Firestore connectivity check."""
    
    @pytest.mark.asyncio
    async def test_firestore_check_passes(self, validator):
        """Firestore check passes with working connection."""
        result = await validator._check_firestore_connectivity()
        
        assert result["passed"] is True
        assert result["error"] is None
    
    @pytest.mark.asyncio
    async def test_firestore_check_fails(self, validator):
        """Firestore check fails with connection error."""
        with patch.object(validator._db, "query", side_effect=Exception("Connection failed")):
            result = await validator._check_firestore_connectivity()
        
        assert result["passed"] is False
        assert "Connection failed" in result["error"]


class TestWorkspacePoolValidation:
    """Test workspace pool validation."""
    
    @pytest.mark.asyncio
    async def test_workspace_pool_check_passes(self, validator, sample_workspaces):
        """Workspace pool check passes with available workspaces."""
        result = await validator._check_workspace_pool()
        
        assert result["passed"] is True
        assert result["error"] is None
        assert result["available_sets"] == 2
        assert result["total_workspaces"] == 6
    
    @pytest.mark.asyncio
    async def test_workspace_pool_check_fails_empty(self, validator):
        """Workspace pool check fails with empty pool."""
        result = await validator._check_workspace_pool()
        
        assert result["passed"] is False
        assert "empty" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_workspace_pool_low_availability_warning(self, validator, sample_workspaces):
        """Workspace pool check warns on low availability."""
        # Allocate workspaces to reduce availability
        db = validator._db
        for ws_id in ["ws-01-1", "ws-01-2", "ws-01-3"]:
            db.update("workspaces", ws_id, {
                "status": "allocated",
                "locked_by": "est-123",
            })
        
        result = await validator._check_workspace_pool()
        
        assert result["passed"] is True
        assert result["warning"] == "Low workspace availability"
        assert result["available_sets"] == 1


class TestFilestoreMount:
    """Test Filestore mount check."""
    
    @pytest.mark.asyncio
    async def test_filestore_check_passes(self, validator, tmp_path):
        """Filestore check passes with accessible directory."""
        with patch.dict(os.environ, {"WORKSPACE_BASE_PATH": str(tmp_path)}):
            result = await validator._check_filestore_mount()
        
        assert result["passed"] is True
        assert result["error"] is None
    
    @pytest.mark.asyncio
    async def test_filestore_check_fails_missing(self, validator):
        """Filestore check fails with missing directory."""
        with patch.dict(os.environ, {"WORKSPACE_BASE_PATH": "/nonexistent/path"}):
            result = await validator._check_filestore_mount()
        
        assert result["passed"] is False
        assert "not found" in result["error"].lower()
    
    @pytest.mark.asyncio
    async def test_filestore_check_fails_not_writable(self, validator, tmp_path):
        """Filestore check fails with non-writable directory."""
        test_dir = tmp_path / "readonly"
        test_dir.mkdir()
        
        with patch.dict(os.environ, {"WORKSPACE_BASE_PATH": str(test_dir)}):
            # Mock write failure
            with patch("pathlib.Path.write_text", side_effect=PermissionError("Cannot write")):
                result = await validator._check_filestore_mount()
        
        assert result["passed"] is False
        assert "Cannot write" in result["error"]


class TestFullStartupValidation:
    """Test complete startup validation sequence."""
    
    @pytest.mark.asyncio
    async def test_startup_validation_passes(self, validator, sample_workspaces, tmp_path):
        """Complete startup validation passes."""
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "test-key",
            "DATABASE_TYPE": "memory",
            "WORKSPACE_BASE_PATH": str(tmp_path),
        }):
            results = await validator.run_all_checks()

        assert results["environment"]["passed"] is True
        assert results["firestore"]["passed"] is True
        assert results["workspace_pool"]["passed"] is True
        assert results["filestore"]["passed"] is True
    
    @pytest.mark.asyncio
    async def test_startup_validation_fails_on_critical(self, validator, sample_workspaces):
        """Startup validation fails on critical check failure."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(StartupValidationError) as exc_info:
                await validator.run_all_checks()
        
        assert "Environment check failed" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_startup_validation_completes_all_checks(self, validator, sample_workspaces, tmp_path):
        """Startup validation completes all checks."""
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "test-key",
            "DATABASE_TYPE": "memory",
            "WORKSPACE_BASE_PATH": str(tmp_path),
        }):
            results = await validator.run_all_checks()

        assert results["environment"]["passed"] is True
        assert results["workspace_pool"]["passed"] is True
        assert results["filestore"]["passed"] is True


class TestConcurrentStartup:
    """Test concurrent startup safety."""
    
    @pytest.mark.asyncio
    async def test_multiple_validators_safe(self, db, workspace_pool, sample_workspaces, tmp_path):
        """Multiple validators can run concurrently."""
        import asyncio

        validator1 = StartupValidator(db, workspace_pool)
        validator2 = StartupValidator(db, workspace_pool)

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "test-key",
            "DATABASE_TYPE": "memory",
            "WORKSPACE_BASE_PATH": str(tmp_path),
        }):
            results1, results2 = await asyncio.gather(
                validator1.run_all_checks(),
                validator2.run_all_checks()
            )

        # Both should pass
        assert results1["environment"]["passed"] is True
        assert results2["environment"]["passed"] is True


class TestProviderKeyValidation:
    """Task 2.8 — LLM provider branching, memory skip, emulator requirements."""

    @pytest.mark.asyncio
    async def test_openrouter_only_passes(self, validator):
        """OpenRouter key present → provider resolves to openrouter, check passes (FR-1)."""
        from app.core.config import Settings

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "or-key",
            "DATABASE_TYPE": "memory",
        }, clear=True):
            with patch("app.services.startup_validation.settings", Settings(_env_file=None)):
                result = await validator._check_environment()
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_anthropic_only_passes(self, validator):
        """Anthropic key only → provider resolves to anthropic, check passes (FR-2)."""
        from app.core.config import Settings

        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-ant-key",
            "DATABASE_TYPE": "memory",
        }, clear=True):
            with patch("app.services.startup_validation.settings", Settings(_env_file=None)):
                result = await validator._check_environment()
        assert result["passed"] is True

    @pytest.mark.asyncio
    async def test_no_provider_key_fails_naming_both(self, validator):
        """When neither key is set, the message names both provider keys (FR-3, fail fast)."""
        from app.core.config import Settings

        with patch.dict(os.environ, {
            "DATABASE_TYPE": "memory",
        }, clear=True):
            with patch("app.services.startup_validation.settings", Settings(_env_file=None)):
                result = await validator._check_environment()
        assert result["passed"] is False
        assert "OPENROUTER_API_KEY" in result["error"]
        assert "ANTHROPIC_API_KEY" in result["error"]

    @pytest.mark.asyncio
    async def test_memory_skips_git_secrets(self, validator):
        """memory database mode does not require git/token secrets (FR-4)."""
        from app.core.config import Settings

        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "or-key",
            "DATABASE_TYPE": "memory",
        }, clear=True):
            with patch("app.services.startup_validation.settings", Settings(_env_file=None)):
                result = await validator._check_environment()
        assert result["passed"] is True
        # Confirm no git-secret keys were required
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_emulator_requires_token_encryption_key(self, validator):
        """emulator mode requires TOKEN_ENCRYPTION_KEY (FR-5)."""
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "or-key",
            "DATABASE_TYPE": "emulator",
        }, clear=True):
            result = await validator._check_environment()
        assert result["passed"] is False
        assert "TOKEN_ENCRYPTION_KEY" in result["error"]
        assert "encrypt" in result["error"].lower() or "required" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_emulator_requires_github_token(self, validator):
        """emulator mode requires GITHUB_TOKEN_DEFAULT (FR-5)."""
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "or-key",
            "DATABASE_TYPE": "emulator",
            "TOKEN_ENCRYPTION_KEY": "fernet-key",
            "GIT_USER_NAME_DEFAULT": "bot",
        }, clear=True):
            result = await validator._check_environment()
        assert result["passed"] is False
        assert "GITHUB_TOKEN_DEFAULT" in result["error"]

    @pytest.mark.asyncio
    async def test_emulator_requires_git_user(self, validator):
        """emulator mode requires GIT_USER_NAME_DEFAULT (FR-5)."""
        with patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "or-key",
            "DATABASE_TYPE": "emulator",
            "TOKEN_ENCRYPTION_KEY": "fernet-key",
            "GITHUB_TOKEN_DEFAULT": "ghp-token",
        }, clear=True):
            result = await validator._check_environment()
        assert result["passed"] is False
        assert "GIT_USER_NAME_DEFAULT" in result["error"]

    def test_unknown_auth_mode_fails_closed(self):
        """Unknown AUTH_MODE value raises ValidationError at settings creation (FR-8)."""
        from pydantic import ValidationError
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("AUTH_MODE", "bogus_mode")
            with pytest.raises(ValidationError):
                Settings()

    def test_unknown_database_type_fails_closed(self):
        """Unknown DATABASE_TYPE value raises ValidationError at settings creation (FR-8)."""
        from pydantic import ValidationError
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DATABASE_TYPE", "unknown_db")
            with pytest.raises(ValidationError, match="Invalid DATABASE_TYPE"):
                Settings()

    def test_extra_env_keys_are_ignored(self):
        """Unsupported .env keys do not fail settings validation or echo their values."""
        from app.core.config import Settings
        from app.core.enums import DatabaseType

        with patch.dict(os.environ, {
            "DATABASE_TYPE": "memory",
            "GIT_USER_EMAIL": "local@example.test",
        }, clear=True):
            settings = Settings(_env_file=None)

        assert settings.DATABASE_TYPE == DatabaseType.MEMORY

    def test_local_auth_rejected_in_kubernetes(self):
        """AUTH_MODE=local fails closed when running inside Kubernetes."""
        from pydantic import ValidationError
        from app.core.config import Settings

        with patch.dict(os.environ, {
            "AUTH_MODE": "local",
            "DATABASE_TYPE": "emulator",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        }, clear=True):
            with pytest.raises(ValidationError, match="AUTH_MODE=local is not allowed"):
                Settings(_env_file=None)

    def test_local_auth_rejected_with_firestore(self):
        """AUTH_MODE=local fails closed for real Firestore-backed startup."""
        from pydantic import ValidationError
        from app.core.config import Settings

        with patch.dict(os.environ, {
            "AUTH_MODE": "local",
            "DATABASE_TYPE": "firestore",
        }, clear=True):
            with pytest.raises(ValidationError, match="AUTH_MODE=local is not allowed"):
                Settings(_env_file=None)


class TestProviderResolution:
    """DEFAULT_PROVIDER is derived solely from which key is present — not configurable.

    Mirrors the .env.quickstart.example "set ONE of the two keys" UX so an
    Anthropic-only setup selects Anthropic instead of silently defaulting to
    OpenRouter and failing the startup key check (backend stuck at 503).
    """

    def _settings(self, env: dict):
        from app.core.config import Settings

        with patch.dict(os.environ, {"DATABASE_TYPE": "memory", **env}, clear=True):
            return Settings(_env_file=None)

    def test_anthropic_only_resolves_anthropic(self):
        from app.core.enums import LLMProvider

        settings = self._settings({"ANTHROPIC_API_KEY": "sk-ant-key"})
        assert settings.DEFAULT_PROVIDER == LLMProvider.ANTHROPIC

    def test_openrouter_only_resolves_openrouter(self):
        from app.core.enums import LLMProvider

        settings = self._settings({"OPENROUTER_API_KEY": "or-key"})
        assert settings.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_both_keys_resolve_openrouter(self):
        from app.core.enums import LLMProvider

        settings = self._settings(
            {"ANTHROPIC_API_KEY": "sk-ant-key", "OPENROUTER_API_KEY": "or-key"}
        )
        assert settings.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_no_keys_resolve_openrouter(self):
        from app.core.enums import LLMProvider

        # Left at openrouter; startup validation then fails fast naming both keys.
        settings = self._settings({})
        assert settings.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_whitespace_key_is_treated_as_unset(self):
        from app.core.enums import LLMProvider

        # A blank/whitespace-only key must not count as "set": OpenRouter is
        # whitespace, only Anthropic is real → provider resolves to anthropic.
        settings = self._settings(
            {"OPENROUTER_API_KEY": "   ", "ANTHROPIC_API_KEY": "sk-ant-key"}
        )
        assert settings.DEFAULT_PROVIDER == LLMProvider.ANTHROPIC

    def test_explicit_default_provider_is_ignored(self):
        from app.core.enums import LLMProvider

        # There is no knob: an attempt to set DEFAULT_PROVIDER is overridden by
        # the key-derived value (anthropic key only → anthropic, despite the env).
        settings = self._settings(
            {"ANTHROPIC_API_KEY": "sk-ant-key", "DEFAULT_PROVIDER": "openrouter"}
        )
        assert settings.DEFAULT_PROVIDER == LLMProvider.ANTHROPIC


class TestIsKeyValid:
    """is_key_valid — the shared blank/whitespace-safe key presence check."""

    def test_non_blank_is_valid(self):
        from app.core.config import is_key_valid

        assert is_key_valid("sk-ant-key") is True

    def test_none_blank_and_whitespace_are_invalid(self):
        from app.core.config import is_key_valid

        assert is_key_valid(None) is False
        assert is_key_valid("") is False
        assert is_key_valid("   ") is False
