"""
Workspace configuration loader for YAML-based multi-workspace setup.
"""

import yaml
from pathlib import Path
from typing import List, Dict, Any, Optional
import logging

from app.schemas.workspace import WorkspaceSettings


class WorkspaceConfigLoader:
    """
    Loads workspace configurations from YAML files.
    
    Supports multiple workspaces with different providers, models,
    and configurations for parallel execution.
    """
    
    def __init__(self, config_path: str | Path, logger: Optional[logging.Logger] = None):
        """
        Initialize workspace config loader.
        
        Args:
            config_path: Path to YAML configuration file
            logger: Optional logger instance
        """
        self.config_path = Path(config_path)
        self.logger = logger or logging.getLogger(__name__)
        self._config: Optional[Dict[str, Any]] = None
    
    def load(self) -> Dict[str, Any]:
        """
        Load configuration from YAML file.
        
        Returns:
            Dict containing configuration
            
        Raises:
            FileNotFoundError: If config file doesn't exist
            yaml.YAMLError: If YAML is invalid
        """
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        
        with open(self.config_path, 'r') as f:
            self._config = yaml.safe_load(f)
        
        self.logger.info(f"Loaded workspace config from: {self.config_path}")
        return self._config
    
    def get_workspaces(self) -> List[WorkspaceSettings]:
        """
        Get all workspace settings from config.
        
        Returns:
            List of WorkspaceSettings instances
        """
        if self._config is None:
            self.load()
        
        workspaces = []
        defaults = self._config.get('defaults', {})
        base_path = defaults.get('base_path', '/agent')
        
        for ws_config in self._config.get('workspaces', []):
            # Merge defaults with workspace-specific config
            workspace = WorkspaceSettings(
                name=ws_config.get('name', f"workspace-{len(workspaces)}"),
                workspace_path=ws_config['workspace_path'],
                provider=ws_config.get('provider', 'anthropic'),
                model=ws_config.get('model', 'claude-sonnet-4-0'),
                base_path=base_path,
                provider_config=ws_config.get('provider_config', {}),
                # Agent overrides
                max_turns=ws_config.get('agent_overrides', {}).get(
                    'max_turns', 
                    defaults.get('max_turns', 200)
                ),
                max_buffer_size=ws_config.get('agent_overrides', {}).get(
                    'max_buffer_size',
                    defaults.get('max_buffer_size', 1024 * 1024 * 10)
                ),
                allowed_tools=ws_config.get('agent_overrides', {}).get('allowed_tools'),
                # P10Y configuration
                p10y_repository_id=ws_config.get('p10y_repository_id'),
            )
            workspaces.append(workspace)
        
        self.logger.info(f"Loaded {len(workspaces)} workspace configurations")
        return workspaces
    
    def get_workspace(self, name: str) -> Optional[WorkspaceSettings]:
        """
        Get a specific workspace by name.
        
        Args:
            name: Workspace name
            
        Returns:
            WorkspaceSettings instance or None if not found
        """
        if self._config is None:
            self.load()
        
        for ws_config in self._config.get('workspaces', []):
            if ws_config.get('name') == name:
                defaults = self._config.get('defaults', {})
                base_path = defaults.get('base_path', '/agent')
                
                return WorkspaceSettings(
                    name=name,
                    workspace_path=ws_config['workspace_path'],
                    provider=ws_config.get('provider', 'anthropic'),
                    model=ws_config.get('model', 'claude-sonnet-4-0'),
                    base_path=base_path,
                    provider_config=ws_config.get('provider_config', {}),
                    max_turns=ws_config.get('agent_overrides', {}).get(
                        'max_turns',
                        defaults.get('max_turns', 200)
                    ),
                    max_buffer_size=ws_config.get('agent_overrides', {}).get(
                        'max_buffer_size',
                        defaults.get('max_buffer_size', 1024 * 1024 * 10)
                    ),
                    allowed_tools=ws_config.get('agent_overrides', {}).get('allowed_tools'),
                    # P10Y configuration
                    p10y_repository_id=ws_config.get('p10y_repository_id'),
                )
        
        return None
    
    def list_workspace_names(self) -> List[str]:
        """
        Get list of all workspace names.
        
        Returns:
            List of workspace names
        """
        if self._config is None:
            self.load()
        
        return [
            ws.get('name', f"workspace-{i}")
            for i, ws in enumerate(self._config.get('workspaces', []))
        ]

    def save(self, workspaces: List[WorkspaceSettings]):
        """
        Save workspaces to YAML file.
        
        Args:
            workspaces: List of WorkspaceSettings instances
        """
        with open(self.config_path, 'w') as f:
            yaml.dump(workspaces, f)