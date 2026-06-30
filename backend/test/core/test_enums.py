"""
Tests for core enums module.

Validates that enum values match the legacy raw strings exactly (INV-3) so
existing env-variable values and == comparisons remain valid without change.
"""

import pytest
from pydantic import ValidationError

from app.core.enums import AuthMode, DatabaseType, LLMProvider


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
    """Verify DEFAULT_PROVIDER is a Settings field and env-overridable."""

    def test_default_is_openrouter(self):
        from app.core.config import Settings

        s = Settings()
        assert s.DEFAULT_PROVIDER == LLMProvider.OPENROUTER
        assert s.DEFAULT_PROVIDER == "openrouter"

    def test_env_override_to_anthropic(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DEFAULT_PROVIDER", "anthropic")
            s = Settings()
        assert s.DEFAULT_PROVIDER == LLMProvider.ANTHROPIC

    def test_database_type_bogus_raises(self):
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("DATABASE_TYPE", "bogus")
            with pytest.raises(ValidationError, match="Invalid DATABASE_TYPE"):
                Settings()
