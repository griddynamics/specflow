"""
OpenRouter provider for accessing multiple AI models through a unified API.
"""

import json
from typing import Dict, Optional
import logging
from .base import BaseProvider


class OpenRouterProvider(BaseProvider):
    """
    Provider for OpenRouter API access.
    
    OpenRouter provides access to multiple models (Gemini, GPT-4, etc.) 
    through a unified API that's compatible with Claude SDK when configured properly.
    """
    
    # Model mapping: our names -> OpenRouter names
    MODEL_MAPPING = {
        # Claude models
        "claude-sonnet-4-5": "anthropic/claude-sonnet-4.5",
        "claude-sonnet-4-6": "anthropic/claude-sonnet-4.6",
        "claude-haiku-4-5": "anthropic/claude-haiku-4.5",
        "claude-opus-4-5": "anthropic/claude-opus-4.5",
        "claude-opus-4-6": "anthropic/claude-opus-4.6",
        
        # Google Gemini models
        "gemini-2.5-pro": "google/gemini-2.5-pro",
        "gemini-3.0-pro-preview": "google/gemini-3-pro-preview",
        "gemini-3.1-pro-preview": "google/gemini-3.1-pro-preview",
        "gemini-3-flash-preview": "google/gemini-3-flash-preview",
        
        # OpenAI GPT models
        "gpt-4-turbo": "openai/gpt-4-turbo",
        "gpt-4o": "openai/gpt-4o",
        "gpt-4o-mini": "openai/gpt-4o-mini",
        "gpt-4": "openai/gpt-4",
        "gpt-5.2": "openai/gpt-5.2",
        "gpt-5.4": "openai/gpt-5.4",
        
        # Zhipu AI GLM models
        "glm-4.5-air": "z-ai/glm-4.5-air:free",
        "glm-5": "z-ai/glm-5",

        # Mistral models
        "mistral-2512": "mistralai/devstral-2512:free"
    }
    
    DEFAULT_BASE_URL = "https://openrouter.ai/api"
    
    def __init__(self,
                 api_key: str,
                 base_url: Optional[str] = None,
                 app_name: Optional[str] = None,
                 logger: Optional[logging.Logger] = None):
        """
        Initialize OpenRouter provider.
        
        Args:
            api_key: OpenRouter API key
            base_url: Override base URL (defaults to openrouter.ai)
            app_name: Optional app name for OpenRouter analytics
            logger: Logger instance
        """
        super().__init__(
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URL,
            logger=logger,
            app_name=app_name
        )
    
    def get_environment_config(self) -> Dict[str, str]:
        """
        Configure Claude SDK to use OpenRouter.
        
        OpenRouter requires:
        - ANTHROPIC_AUTH_TOKEN: Set to OpenRouter API key
        - ANTHROPIC_API_KEY: Must be explicitly empty!!!!!
        - ANTHROPIC_BASE_URL: Set to OpenRouter's API endpoint
        
        Returns:
            Dict with ANTHROPIC_AUTH_TOKEN, ANTHROPIC_API_KEY (empty), and ANTHROPIC_BASE_URL
        """
        config = {
            "ANTHROPIC_AUTH_TOKEN": self.api_key,
            "ANTHROPIC_API_KEY": "",  # Must be explicitly empty for OpenRouter
            "ANTHROPIC_BASE_URL": self.base_url,
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0"
        }
        
        if self.config.get("app_name"):
            config["ANTHROPIC_EXTRA_HEADERS"] = json.dumps({
                "X-Title": self.config["app_name"]
            })
        
        return config
    
    def transform_model_name(self, model: str) -> str:
        """
        Transform model name to OpenRouter format.
        
        Args:
            model: Our internal model name or OpenRouter format
            
        Returns:
            OpenRouter model identifier (e.g., "google/gemini-3.1-pro-preview")
        """
        # If already in OpenRouter format (contains '/'), return as-is
        if '/' in model:
            return model
        
        # Otherwise, look up in mapping
        return self.MODEL_MAPPING.get(model, model)

    def use_catalog_pricing(self) -> bool:
        return True
