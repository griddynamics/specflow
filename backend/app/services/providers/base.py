"""
Base provider interface for AI model providers.
"""

from abc import ABC, abstractmethod
import logging
from typing import Dict, Optional


class BaseProvider(ABC):
    """
    Abstract base class for AI model providers.
    
    Each provider implementation handles the specific configuration
    needed to make Claude Agent SDK work with that provider's API.
    """
    
    def __init__(self, 
                 api_key: str,
                 base_url: Optional[str] = None,
                 logger: Optional[logging.Logger] = None,
                 **kwargs):
        """
        Initialize the provider.
        
        Args:
            api_key: API key for the provider
            base_url: Optional base URL override
            logger: Logger instance
            **kwargs: Provider-specific configuration
        """
        self.api_key = api_key
        self.base_url = base_url
        self.logger = logger or logging.getLogger(__name__)
        self.config = kwargs
    
    @abstractmethod
    def get_environment_config(self) -> Dict[str, str]:
        """
        Get environment variables needed for Claude SDK.
        
        Returns:
            Dict of environment variable names to values
        """
        pass
    
    @abstractmethod
    def transform_model_name(self, model: str) -> str:
        """
        Transform our internal model name to provider-specific format.
        
        Args:
            model: Our internal model identifier
            
        Returns:
            Provider-specific model identifier
        """
        pass
    
    @abstractmethod
    def use_catalog_pricing(self) -> bool:
        """Return True if cost should be resolved via the OpenRouter catalog."""
        pass
    
    def get_base_url(self) -> Optional[str]:
        """Get the base URL for the provider."""
        return self.base_url
    
    def get_api_key(self) -> str:
        """Get the API key for the provider."""
        return self.api_key
