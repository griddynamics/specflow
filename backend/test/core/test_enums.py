"""
Tests for core enums module.

Validates that enum values match the legacy raw strings exactly (INV-3) so
existing env-variable values and == comparisons remain valid without change.
"""

import pytest
from pydantic import ValidationError

from app.core.enums import AuthMode, BackendRuntime, DatabaseType, LLMProvider


class TestLLMProvider:
    def test_openrouter_value(self):
        assert LLMProvider.OPENROUTER == "openrouter"

    def test_anthropic_value(self):
        assert LLMProvider.ANTHROPIC == "anthropic"

    def test_str_equality(self):
        assert LLMProvider.OPENROUTER == "openrouter"
        assert "openrouter" == LLMProvider.OPENROUTER

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            LLMProvider("invalid_provider")


class TestAuthMode:
    def test_api_key_value(self):
        assert AuthMode.API_KEY == "api_key"

    def test_local_value(self):
        assert AuthMode.LOCAL == "local"

    def test_str_equality(self):
        assert AuthMode.API_KEY == "api_key"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            AuthMode("unknown_mode")


class TestDatabaseType:
    def test_memory_value(self):
        assert DatabaseType.MEMORY == "memory"

    def test_emulator_value(self):
        assert DatabaseType.EMULATOR == "emulator"

    def test_firestore_value(self):
        assert DatabaseType.FIRESTORE == "firestore"

    def test_str_equality(self):
        assert DatabaseType.MEMORY == "memory"
        assert DatabaseType.EMULATOR == "emulator"
        assert DatabaseType.FIRESTORE == "firestore"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            DatabaseType("bogus")


class TestSettingsDefaultProvider:
    """DEFAULT_PROVIDER is derived from the key present — not an env-settable knob."""

    def test_no_keys_resolve_openrouter(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("ANTHROPIC_API_KEY", raising=False)
            mp.delenv("OPENROUTER_API_KEY", raising=False)
            s = Settings(_env_file=None)
        assert s.DEFAULT_PROVIDER == LLMProvider.OPENROUTER
        assert s.DEFAULT_PROVIDER == "openrouter"

    def test_env_default_provider_is_ignored(self):
        from app.core.config import Settings

        # Setting DEFAULT_PROVIDER has no effect: the present key decides.
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("ANTHROPIC_API_KEY", raising=False)
            mp.setenv("OPENROUTER_API_KEY", "or-key")
            mp.setenv("DEFAULT_PROVIDER", "anthropic")
            s = Settings(_env_file=None)
        assert s.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_database_type_bogus_raises(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DATABASE_TYPE", "bogus")
            with pytest.raises(ValidationError, match="Invalid DATABASE_TYPE"):
                Settings()


class TestBackendRuntime:
    def test_values(self):
        assert BackendRuntime.DOCKER == "docker"
        assert BackendRuntime.PROCESS == "process"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            BackendRuntime("vm")


class TestSettingsBackendRuntime:
    def test_defaults_to_docker(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("BACKEND_RUNTIME", raising=False)
            s = Settings(_env_file=None)
        assert s.BACKEND_RUNTIME == BackendRuntime.DOCKER

    def test_process_accepted(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("BACKEND_RUNTIME", "process")
            s = Settings(_env_file=None)
        assert s.BACKEND_RUNTIME == BackendRuntime.PROCESS

    def test_bogus_raises(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("BACKEND_RUNTIME", "vm")
            with pytest.raises(ValidationError, match="Invalid BACKEND_RUNTIME"):
                Settings(_env_file=None)
