"""
Unit tests for provider abstraction layer.
"""

import pytest
from app.services.providers import (
    ProviderFactory,
    AnthropicProvider,
    BaseProvider,
)


class TestAnthropicProvider:
    """Tests for Anthropic provider."""
    
    def test_environment_config_minimal(self):
        """Test environment config with just API key."""
        provider = AnthropicProvider(api_key="test-key")
        config = provider.get_environment_config()
        
        assert config["ANTHROPIC_API_KEY"] == "test-key"
        assert "ANTHROPIC_BASE_URL" not in config  # Uses default
    
    def test_environment_config_with_custom_url(self):
        """Test environment config with custom base URL."""
        provider = AnthropicProvider(
            api_key="test-key",
            base_url="https://custom.api.com"
        )
        config = provider.get_environment_config()
        
        assert config["ANTHROPIC_API_KEY"] == "test-key"
        assert config["ANTHROPIC_BASE_URL"] == "https://custom.api.com"
    
    def test_model_transformation(self):
        """Test that Anthropic models pass through unchanged."""
        provider = AnthropicProvider(api_key="test-key")
        
        assert provider.transform_model_name("claude-sonnet-4-0") == "claude-sonnet-4-0"
        assert provider.transform_model_name("claude-opus-4-5") == "claude-opus-4-5"

    def test_default_base_url(self):
        """Test default base URL is set correctly."""
        provider = AnthropicProvider(api_key="test-key")
        
        assert provider.get_base_url() == "https://api.anthropic.com"
    
    def test_api_key_accessor(self):
        """Test API key accessor."""
        provider = AnthropicProvider(api_key="test-key-123")
        
        assert provider.get_api_key() == "test-key-123"


class TestProviderFactory:
    """Tests for provider factory."""
    
    def test_create_anthropic_provider(self):
        """Test creating Anthropic provider through factory."""
        provider = ProviderFactory.create_provider(
            "anthropic",
            api_key="test-key"
        )
        
        assert isinstance(provider, AnthropicProvider)
        assert isinstance(provider, BaseProvider)
        assert provider.get_api_key() == "test-key"
    
    def test_create_provider_case_insensitive(self):
        """Test provider name is case-insensitive."""
        provider1 = ProviderFactory.create_provider("anthropic", api_key="key1")
        provider2 = ProviderFactory.create_provider("ANTHROPIC", api_key="key2")
        provider3 = ProviderFactory.create_provider("Anthropic", api_key="key3")
        
        assert isinstance(provider1, AnthropicProvider)
        assert isinstance(provider2, AnthropicProvider)
        assert isinstance(provider3, AnthropicProvider)
    
    def test_create_provider_with_base_url(self):
        """Test creating provider with base URL."""
        provider = ProviderFactory.create_provider(
            "anthropic",
            api_key="test-key",
            base_url="https://custom.url"
        )
        
        assert provider.get_base_url() == "https://custom.url"
    
    def test_unknown_provider_raises_error(self):
        """Test that unknown provider raises ValueError."""
        with pytest.raises(ValueError, match="Unknown provider: unknown"):
            ProviderFactory.create_provider("unknown", api_key="test-key")
    
    def test_error_message_lists_available_providers(self):
        """Test error message includes list of available providers."""
        with pytest.raises(ValueError, match="Available providers: anthropic"):
            ProviderFactory.create_provider("nonexistent", api_key="test-key")
    
    def test_list_providers(self):
        """Test listing available providers."""
        providers = ProviderFactory.list_providers()
        
        assert isinstance(providers, list)
        assert "anthropic" in providers
        assert len(providers) >= 1  # At least Anthropic


class TestWorkspaceIntegration:
    """Integration tests for providers with workspace settings."""
    
    def test_provider_with_workspace_settings(self):
        """Test provider works with workspace settings."""
        from app.schemas.workspace import WorkspaceSettings
        
        # Create workspace settings
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        # Create provider
        provider = ProviderFactory.create_provider(
            provider_name=workspace.provider,
            api_key="test-key"
        )
        
        # Get configuration
        env_config = provider.get_environment_config()
        transformed_model = provider.transform_model_name(workspace.model)
        
        # Verify
        assert "ANTHROPIC_API_KEY" in env_config
        assert transformed_model == "claude-sonnet-4-0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
