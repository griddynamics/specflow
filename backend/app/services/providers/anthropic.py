"""
Anthropic provider for direct Anthropic API access.
"""

from typing import Dict, Optional
import logging
from .base import BaseProvider


class AnthropicProvider(BaseProvider):
    """
    Provider for direct Anthropic API access.
    
    This is the default provider, maintaining backward compatibility.
    """

    def __init__(self,
                 api_key: str,
                 base_url: Optional[str] = None,
                 logger: Optional[logging.Logger] = None):
        """
        Initialize Anthropic provider.
        
        Args:
            api_key: Anthropic API key
            base_url: Optional base URL override (defaults to Anthropic's official API)
            logger: Logger instance
        """
        super().__init__(api_key, base_url, logger)
        
        # Default to official Anthropic API
        if not self.base_url:
            self.base_url = "https://api.anthropic.com"
    
    def get_environment_config(self) -> Dict[str, str]:
        """
        Configure for direct Anthropic API.
        
        Returns:
            Dict with ANTHROPIC_API_KEY and optionally ANTHROPIC_BASE_URL
        """
        config = {
            "ANTHROPIC_API_KEY": self.api_key,
        }
        
        # Only set base URL if it's not the default
        if self.base_url and self.base_url != "https://api.anthropic.com":
            config["ANTHROPIC_BASE_URL"] = self.base_url
        
        return config
    
    def transform_model_name(self, model: str) -> str:
        """
        Anthropic uses models as-is.
        
        Args:
            model: Model name
            
        Returns:
            Same model name (no transformation needed)
        """
        return model

    def use_catalog_pricing(self) -> bool:
        return False
