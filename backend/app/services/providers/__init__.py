"""
Provider abstraction for multiple AI model providers.

This module provides a unified interface for configuring different
AI providers (Anthropic, OpenRouter, Bedrock, etc.) with Claude SDK.
"""

import logging
from typing import Dict, Optional

from .anthropic import AnthropicProvider
from .base import BaseProvider
from .openrouter import OpenRouterProvider

# Registry of available providers
PROVIDER_REGISTRY: Dict[str, type[BaseProvider]] = {
    "anthropic": AnthropicProvider,
    "openrouter": OpenRouterProvider,
    # Future providers can be added here:
    # "bedrock": BedrockProvider,
    # "vertex": VertexAIProvider,
}

class ProviderFactory:
    """Factory for creating provider instances."""
    
    @staticmethod
    def create_provider(
        provider_name: str,
        api_key: str,
        base_url: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        **kwargs
    ) -> BaseProvider:
        """
        Create a provider instance.
        
        Args:
            provider_name: Name of the provider ("anthropic", "openrouter", etc.)
            api_key: API key for the provider
            base_url: Optional base URL override
            logger: Optional logger instance
            **kwargs: Provider-specific configuration
            
        Returns:
            Configured provider instance
            
        Raises:
            ValueError: If provider_name is not supported
        """
        provider_class = PROVIDER_REGISTRY.get(provider_name.lower())
        
        if not provider_class:
            available = ", ".join(PROVIDER_REGISTRY.keys())
            raise ValueError(
                f"Unknown provider: {provider_name}. "
                f"Available providers: {available}"
            )
        
        return provider_class(
            api_key=api_key,
            base_url=base_url,
            logger=logger,
            **kwargs
        )
    
    @staticmethod
    def list_providers() -> list[str]:
        """Get list of available provider names."""
        return list(PROVIDER_REGISTRY.keys())

__all__ = [
    "BaseProvider",
    "AnthropicProvider",
    "OpenRouterProvider",
    "ProviderFactory",
    "PROVIDER_REGISTRY",
]
