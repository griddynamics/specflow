"""
Workspace configuration schemas.

Workspaces represent isolated environments with specific provider/model configurations.
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any
from pathlib import Path


@dataclass
class WorkspaceSettings:
    """
    Configuration for a workspace with provider-specific settings.
    
    Each workspace can have its own model provider and configuration,
    allowing different repositories to use different AI providers.
    
    Isolated Workspace Model:
    - Each workspace is mounted at an isolated root directory (e.g., /workspace1, /workspace2)
    - The workspace_path should be the isolated root (e.g., "/workspace1")
    - Agents run with cwd set to the isolated root, preventing cross-workspace access
    """
    
    # Core settings
    workspace_path: str | Path
    provider: str  # "anthropic", "openrouter", "bedrock", "vertex"
    model: str     # Provider-specific model name
    name: Optional[str] = None  # Workspace name/identifier (auto-generated if not provided)
    
    # Optional settings
    base_path: Optional[str | Path] = None  # Deprecated: kept for backward compatibility
    
    # Provider-specific configuration
    provider_config: Optional[Dict[str, Any]] = None
    
    # Agent configuration overrides
    max_turns: Optional[int] = None
    max_buffer_size: Optional[int] = None
    allowed_tools: Optional[list[str]] = None
    
    # P10Y configuration (each workspace has its own repository)
    p10y_repository_id: Optional[int] = None
    
    def __post_init__(self):
        """Normalize paths and validate settings."""
        self.workspace_path = Path(self.workspace_path)
        if self.base_path:
            self.base_path = Path(self.base_path)
        
        # Ensure provider_config is a dict
        if self.provider_config is None:
            self.provider_config = {}
    
    @property
    def full_workspace_path(self) -> Path:
        """
        Get the full path to the workspace.
        
        Deprecated: Use get_isolated_root() for new isolated workspace model.
        Kept for backward compatibility.
        """
        if self.base_path:
            return self.base_path / self.workspace_path
        return self.workspace_path
    
    def get_isolated_root(self) -> str:
        """
        Get the isolated root path for this workspace.
        
        In the isolated workspace model, each workspace is mounted at its own root
        directory (e.g., /workspace1, /workspace2) rather than nested under a common
        base path (e.g., /agent/workspace1).
        
        This method returns the workspace_path directly as the isolated root, which
        should be used as the agent's cwd parameter.
        
        Returns:
            str: The isolated root path (e.g., "/workspace1")
            
        Examples:
            >>> ws = WorkspaceSettings(workspace_path="/workspace1", ...)
            >>> ws.get_isolated_root()
            "/workspace1"
        """
        # Convert to string to ensure consistent return type
        return str(self.workspace_path)
    
    def get_workspace_relative_path(self, absolute_path: str) -> str:
        """
        Convert an absolute path to a workspace-relative path.
        
        This is useful for converting paths from API requests (which may use legacy
        absolute paths) to workspace-relative paths that agents can use.
        
        Args:
            absolute_path: An absolute path, possibly including base_path or workspace_path
            
        Returns:
            str: A workspace-relative path (e.g., "./specifications")
            
        Examples:
            >>> ws = WorkspaceSettings(workspace_path="/workspace1", ...)
            >>> ws.get_workspace_relative_path("/workspace1/specifications")
            "./specifications"
            >>> ws.get_workspace_relative_path("specifications")
            "./specifications"
            >>> ws.get_workspace_relative_path("/agent/workspace/specflow")
            "./specflow"
        """
        # Normalize the input path
        abs_path = Path(absolute_path)
        
        # Try to make it relative to the workspace root
        isolated_root = Path(self.get_isolated_root())
        
        try:
            # If the path starts with the isolated root, make it relative
            relative = abs_path.relative_to(isolated_root)
            return f"./{relative}"
        except ValueError:
            # Path doesn't start with isolated root
            pass
        
        # Try to make it relative to full_workspace_path (backward compatibility)
        try:
            relative = abs_path.relative_to(self.full_workspace_path)
            return f"./{relative}"
        except ValueError:
            pass
        
        # If the path is already relative or doesn't match workspace paths,
        # ensure it starts with ./
        path_str = str(absolute_path)
        if path_str.startswith('/'):
            # Extract the last component as a relative path
            # e.g., "/some/path/specifications" -> "./specifications"
            return f"./{abs_path.name}"
        elif path_str.startswith('./'):
            return path_str
        else:
            return f"./{path_str}"
    
    def resolve_path_in_workspace(self, relative_path: str) -> str:
        """
        Resolve a workspace-relative path to an absolute path within the workspace.
        
        This is the inverse of get_workspace_relative_path() - it takes a relative
        path and returns the full absolute path within the isolated workspace.
        
        Args:
            relative_path: A relative path (e.g., "./specifications" or "specifications")
            
        Returns:
            str: The absolute path within the workspace (e.g., "/workspace1/specifications")
            
        Examples:
            >>> ws = WorkspaceSettings(workspace_path="/workspace1", ...)
            >>> ws.resolve_path_in_workspace("./specifications")
            "/workspace1/specifications"
            >>> ws.resolve_path_in_workspace("specflow")
            "/workspace1/specflow"
        """
        # Remove leading ./ if present
        rel_path = relative_path.lstrip('./')
        
        # Join with isolated root
        isolated_root = Path(self.get_isolated_root())
        resolved = isolated_root / rel_path
        
        return str(resolved)