"""
Unit tests for OpenRouter provider.
"""

import pytest
import json
from app.services.providers import OpenRouterProvider, ProviderFactory


class TestOpenRouterProvider:
    """Tests for OpenRouter provider."""
    
    def test_environment_config(self):
        """Test OpenRouter environment configuration."""
        provider = OpenRouterProvider(api_key="test-key")
        config = provider.get_environment_config()
        
        assert config["ANTHROPIC_API_KEY"] == ""
        assert config["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    
    def test_environment_config_with_custom_url(self):
        """Test with custom base URL."""
        provider = OpenRouterProvider(
            api_key="test-key",
            base_url="https://custom.openrouter.com/api/v1"
        )
        config = provider.get_environment_config()
        
        assert config["ANTHROPIC_BASE_URL"] == "https://custom.openrouter.com/api/v1"
    
    def test_environment_config_with_app_name(self):
        """Test that app_name is included in headers."""
        provider = OpenRouterProvider(
            api_key="test-key",
            app_name="Test App"
        )
        config = provider.get_environment_config()
        
        assert "ANTHROPIC_EXTRA_HEADERS" in config
        headers = json.loads(config["ANTHROPIC_EXTRA_HEADERS"])
        assert headers["X-Title"] == "Test App"
    
    def test_model_transformation_gemini(self):
        """Test Gemini model name transformation."""
        provider = OpenRouterProvider(api_key="test-key")
        
        assert provider.transform_model_name("gemini-2.5-pro") == "google/gemini-2.5-pro"
        assert provider.transform_model_name("gemini-3.1-pro-preview") == "google/gemini-3.1-pro-preview"
    
    def test_model_transformation_gpt(self):
        """Test GPT model name transformation."""
        provider = OpenRouterProvider(api_key="test-key")
        
        assert provider.transform_model_name("gpt-4o") == "openai/gpt-4o"
        assert provider.transform_model_name("gpt-4-turbo") == "openai/gpt-4-turbo"
        assert provider.transform_model_name("gpt-4o-mini") == "openai/gpt-4o-mini"
    
    def test_model_transformation_claude(self):
        """Test Claude model name transformation via OpenRouter."""
        provider = OpenRouterProvider(api_key="test-key")
        
        assert provider.transform_model_name("claude-haiku-4-5") == "anthropic/claude-haiku-4.5"
        assert provider.transform_model_name("claude-opus-4-5") == "anthropic/claude-opus-4.5"
    
    def test_model_transformation_glm(self):
        """Test GLM model name transformation."""
        provider = OpenRouterProvider(api_key="test-key")
        
        assert provider.transform_model_name("glm-4.5-air") == "z-ai/glm-4.5-air:free"
    
    def test_model_transformation_already_formatted(self):
        """Test that already-formatted names pass through."""
        provider = OpenRouterProvider(api_key="test-key")
        
        # Already in OpenRouter format (has '/')
        assert provider.transform_model_name("google/gemini-3.1-pro-preview") == "google/gemini-3.1-pro-preview"
        assert provider.transform_model_name("openai/gpt-4o") == "openai/gpt-4o"
        assert provider.transform_model_name("custom/model") == "custom/model"
    
    def test_model_transformation_unknown(self):
        """Test unknown model passes through unchanged."""
        provider = OpenRouterProvider(api_key="test-key")
        
        # Unknown model without '/' returns as-is
        assert provider.transform_model_name("unknown-model") == "unknown-model"

    def test_default_base_url(self):
        """Test default base URL."""
        provider = OpenRouterProvider(api_key="test-key")
        
        assert provider.get_base_url() == "https://openrouter.ai/api"


class TestOpenRouterProviderFactory:
    """Test OpenRouter through factory."""
    
    def test_create_openrouter_provider(self):
        """Test creating OpenRouter provider through factory."""
        provider = ProviderFactory.create_provider(
            "openrouter",
            api_key="test-key"
        )
        
        assert isinstance(provider, OpenRouterProvider)
        assert provider.get_api_key() == "test-key"
    
    def test_create_with_app_name(self):
        """Test creating with app_name configuration."""
        provider = ProviderFactory.create_provider(
            "openrouter",
            api_key="test-key",
            app_name="My App"
        )
        
        config = provider.get_environment_config()
        assert "ANTHROPIC_EXTRA_HEADERS" in config
    
    def test_list_providers_includes_openrouter(self):
        """Test that OpenRouter is in provider list."""
        providers = ProviderFactory.list_providers()
        
        assert "openrouter" in providers
        assert "anthropic" in providers


class TestOpenRouterIntegration:
    """Integration tests for OpenRouter with workspace."""
    
    def test_openrouter_with_workspace_settings(self):
        """Test OpenRouter provider with workspace settings."""
        from app.schemas.workspace import WorkspaceSettings
        
        # Create workspace for Gemini
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace_gemini",
            provider="openrouter",
            model="gemini-2.5-pro",
            provider_config={"app_name": "SpecFlow Gemini Test"}
        )
        
        # Create provider
        provider = ProviderFactory.create_provider(
            provider_name=workspace.provider,
            api_key="test-key",
            **workspace.provider_config
        )
        
        # Get configuration
        env_config = provider.get_environment_config()
        transformed_model = provider.transform_model_name(workspace.model)
        
        # Verify
        assert env_config["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
        assert transformed_model == "google/gemini-2.5-pro"

        # Verify app name in headers
        assert "ANTHROPIC_EXTRA_HEADERS" in env_config
        headers = json.loads(env_config["ANTHROPIC_EXTRA_HEADERS"])
        assert headers["X-Title"] == "SpecFlow Gemini Test"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
