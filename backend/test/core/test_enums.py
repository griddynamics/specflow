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

        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("ANTHROPIC_API_KEY", raising=False)
            mp.delenv("OPENROUTER_API_KEY", raising=False)
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


class TestDefaultProviderInference:
    """DEFAULT_PROVIDER auto-detection from whichever API key is set (unset case).

    Regression coverage for the bug where hand-editing .env to switch from
    OPENROUTER_API_KEY to ANTHROPIC_API_KEY (without re-running specflow-init.sh)
    left DEFAULT_PROVIDER unset, and docker-compose's hardcoded
    ``${DEFAULT_PROVIDER:-openrouter}`` fallback silently forced openrouter,
    failing startup validation despite a valid Anthropic key being present.
    """

    def _settings(self, mp, *, anthropic: str | None, openrouter: str | None):
        from app.core.config import Settings

        mp.delenv("DEFAULT_PROVIDER", raising=False)
        if anthropic is None:
            mp.delenv("ANTHROPIC_API_KEY", raising=False)
        else:
            mp.setenv("ANTHROPIC_API_KEY", anthropic)
        if openrouter is None:
            mp.delenv("OPENROUTER_API_KEY", raising=False)
        else:
            mp.setenv("OPENROUTER_API_KEY", openrouter)
        return Settings()

    def test_anthropic_only_infers_anthropic(self):
        with pytest.MonkeyPatch.context() as mp:
            s = self._settings(mp, anthropic="sk-ant-test", openrouter=None)
        assert s.DEFAULT_PROVIDER == LLMProvider.ANTHROPIC

    def test_openrouter_only_infers_openrouter(self):
        with pytest.MonkeyPatch.context() as mp:
            s = self._settings(mp, anthropic=None, openrouter="or-test")
        assert s.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_both_keys_set_defaults_to_openrouter(self):
        with pytest.MonkeyPatch.context() as mp:
            s = self._settings(mp, anthropic="sk-ant-test", openrouter="or-test")
        assert s.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_neither_key_set_defaults_to_openrouter(self):
        with pytest.MonkeyPatch.context() as mp:
            s = self._settings(mp, anthropic=None, openrouter=None)
        assert s.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_explicit_default_provider_wins_over_inference(self):
        """An explicit DEFAULT_PROVIDER always overrides key-based inference."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
            mp.delenv("OPENROUTER_API_KEY", raising=False)
            mp.setenv("DEFAULT_PROVIDER", "openrouter")
            s = Settings()
        assert s.DEFAULT_PROVIDER == LLMProvider.OPENROUTER

    def test_blank_default_provider_still_infers(self):
        """Mirrors docker-compose's ${DEFAULT_PROVIDER:-} passing an empty string
        rather than omitting the var entirely — must be treated as unset."""
        from app.core.config import Settings

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
            mp.delenv("OPENROUTER_API_KEY", raising=False)
            mp.setenv("DEFAULT_PROVIDER", "")
            s = Settings()
        assert s.DEFAULT_PROVIDER == LLMProvider.ANTHROPIC
