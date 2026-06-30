"""
Unit tests for workspace settings.
"""

import pytest
from pathlib import Path
from app.schemas.workspace import WorkspaceSettings


class TestWorkspaceSettings:
    """Tests for WorkspaceSettings dataclass."""
    
    def test_minimal_workspace_settings(self):
        """Test creating workspace with minimal required fields."""
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        assert workspace.workspace_path == Path("/agent/workspace")
        assert workspace.provider == "anthropic"
        assert workspace.model == "claude-sonnet-4-0"
        assert workspace.provider_config == {}
    
    def test_workspace_with_base_path(self):
        """Test workspace with base path."""
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0",
            base_path="/agent"
        )
        
        assert workspace.base_path == Path("/agent")
        assert workspace.full_workspace_path == Path("/agent/workspace1")
    
    def test_workspace_without_base_path(self):
        """Test workspace without base path uses workspace_path directly."""
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/absolute/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        assert workspace.full_workspace_path == Path("/absolute/workspace")
    
    def test_workspace_with_provider_config(self):
        """Test workspace with provider-specific configuration."""
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="openrouter",
            model="gemini-2.5-pro",
            provider_config={
                "app_name": "Test App",
                "custom_setting": "value"
            }
        )
        
        assert workspace.provider_config["app_name"] == "Test App"
        assert workspace.provider_config["custom_setting"] == "value"
    
    def test_workspace_with_agent_overrides(self):
        """Test workspace with agent configuration overrides."""
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0",
            max_turns=500,
            max_buffer_size=10485760,
            allowed_tools=["Read", "Write", "Bash"]
        )
        
        assert workspace.max_turns == 500
        assert workspace.max_buffer_size == 10485760
        assert workspace.allowed_tools == ["Read", "Write", "Bash"]
    
    def test_path_normalization(self):
        """Test that paths are normalized to Path objects."""
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="workspace",
            provider="anthropic",
            model="claude-sonnet-4-0",
            base_path="/base"
        )
        
        assert isinstance(workspace.workspace_path, Path)
        assert isinstance(workspace.base_path, Path)
        assert isinstance(workspace.full_workspace_path, Path)
    

class TestWorkspaceSettingsValidation:
    """Tests for workspace settings validation."""
    
    def test_required_fields(self):
        """Test that required fields must be provided."""
        # This should work
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/path",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        assert workspace is not None
        
        # Missing fields would be caught by dataclass at instantiation
        with pytest.raises(TypeError):
            WorkspaceSettings(workspace_path="/path")  # Missing provider and model
    
    def test_provider_config_defaults_to_empty_dict(self):
        """Test that provider_config defaults to empty dict."""
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/path",
            provider="anthropic",
            model="claude-sonnet-4-0",
            provider_config=None  # Explicitly None
        )
        
        assert workspace.provider_config == {}
        assert isinstance(workspace.provider_config, dict)


class TestIsolatedWorkspaceModel:
    """Tests for isolated workspace model path resolution methods."""
    
    def test_get_isolated_root(self):
        """Test get_isolated_root returns workspace_path as string."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        assert workspace.get_isolated_root() == "/workspace1"
        assert isinstance(workspace.get_isolated_root(), str)
    
    def test_get_isolated_root_converts_path_to_string(self):
        """Test get_isolated_root converts Path to string."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path=Path("/workspace2"),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        assert workspace.get_isolated_root() == "/workspace2"
        assert isinstance(workspace.get_isolated_root(), str)
    
    def test_get_workspace_relative_path_from_absolute(self):
        """Test converting absolute path within workspace to relative."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        result = workspace.get_workspace_relative_path("/workspace1/specifications")
        assert result == "./specifications"
        
        result = workspace.get_workspace_relative_path("/workspace1/specflow/output")
        assert result == "./specflow/output"
    
    def test_get_workspace_relative_path_from_simple_string(self):
        """Test converting simple string to relative path."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        result = workspace.get_workspace_relative_path("specifications")
        assert result == "./specifications"
    
    def test_get_workspace_relative_path_already_relative(self):
        """Test that already relative paths are returned unchanged."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        result = workspace.get_workspace_relative_path("./specflow")
        assert result == "./specflow"
    
    def test_get_workspace_relative_path_backward_compatibility(self):
        """Test backward compatibility with legacy /agent/workspace paths."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="workspace",
            provider="anthropic",
            model="claude-sonnet-4-0",
            base_path="/agent"
        )
        
        # Should work with full_workspace_path (/agent/workspace)
        result = workspace.get_workspace_relative_path("/agent/workspace/specifications")
        assert result == "./specifications"
    
    def test_get_workspace_relative_path_unknown_absolute(self):
        """Test handling of absolute paths not in workspace."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        # Should extract the basename
        result = workspace.get_workspace_relative_path("/some/other/path/specflow")
        assert result == "./specflow"
    
    def test_resolve_path_in_workspace(self):
        """Test resolving relative path to absolute within workspace."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        result = workspace.resolve_path_in_workspace("./specifications")
        assert result == "/workspace1/specifications"
        
        result = workspace.resolve_path_in_workspace("specflow")
        assert result == "/workspace1/specflow"
        
        result = workspace.resolve_path_in_workspace("./specflow/output")
        assert result == "/workspace1/specflow/output"
    
    def test_resolve_path_in_workspace_strips_dot_slash(self):
        """Test that ./ prefix is properly stripped."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace2",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        result = workspace.resolve_path_in_workspace("./specifications")
        assert result == "/workspace2/specifications"
        
        # Without ./ should work the same
        result2 = workspace.resolve_path_in_workspace("specifications")
        assert result == result2
    
    def test_path_conversion_roundtrip(self):
        """Test that converting absolute->relative->absolute works correctly."""
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path="/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        original = "/workspace1/specifications/requirements"
        
        # Convert to relative
        relative = workspace.get_workspace_relative_path(original)
        assert relative == "./specifications/requirements"
        
        # Convert back to absolute
        absolute = workspace.resolve_path_in_workspace(relative)
        assert absolute == original


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
