"""
Unit tests for WorkspaceManager.
"""

import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock
from app.services.workspace_manager import WorkspaceManager
from app.services.providers import AnthropicProvider, OpenRouterProvider
from app.schemas.workspace import WorkspaceSettings
from app.core.config import Settings


class TestWorkspaceManager:
    """Tests for WorkspaceManager."""
    
    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = Mock(spec=Settings)
        settings.AGENT_BASE_PATH = "/agent"
        settings.ANTHROPIC_API_KEY = "test-anthropic-key"
        settings.OPENROUTER_API_KEY = "test-openrouter-key"
        settings.OPENROUTER_BASE_URL = "https://openrouter.ai/api"
        settings.STANDARDS_DIR_NAME = "standards"  # Add the new constant
        return settings
    
    def test_create_workspace_settings(self, mock_settings):
        """Test creating workspace settings."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = manager.create_workspace_settings(
            workspace_path="workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        assert workspace.workspace_path.name == "workspace1"
        assert workspace.provider == "anthropic"
        assert workspace.model == "claude-sonnet-4-0"
        assert workspace.base_path.as_posix() == "/agent"
    
    def test_get_provider_anthropic(self, mock_settings):
        """Test getting Anthropic provider."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        provider = manager.get_provider(workspace)
        
        assert isinstance(provider, AnthropicProvider)
        assert provider.get_api_key() == "test-anthropic-key"
    
    def test_get_provider_openrouter(self, mock_settings):
        """Test getting OpenRouter provider."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="openrouter",
            model="gemini-2.5-pro"
        )
        
        provider = manager.get_provider(workspace)
        
        assert isinstance(provider, OpenRouterProvider)
        assert provider.get_api_key() == "test-openrouter-key"
        assert provider.get_base_url() == "https://openrouter.ai/api"
    
    def test_provider_caching(self, mock_settings):
        """Test that providers are cached."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        provider1 = manager.get_provider(workspace)
        provider2 = manager.get_provider(workspace)
        
        assert provider1 is provider2  # Same instance
        assert "anthropic:claude-sonnet-4-0" in manager.list_cached_providers()
    
    def test_clear_cache(self, mock_settings):
        """Test clearing provider cache."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        provider1 = manager.get_provider(workspace)
        manager.clear_cache()
        provider2 = manager.get_provider(workspace)
        
        assert provider1 is not provider2  # Different instances
        assert len(manager.list_cached_providers()) == 1  # Only provider2 cached
    
    def test_missing_api_key_raises_error(self, mock_settings):
        """Test that missing API key raises error."""
        mock_settings.ANTHROPIC_API_KEY = None
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        with pytest.raises(ValueError, match="No API key configured"):
            manager.get_provider(workspace)
    
    def test_openrouter_requires_openrouter_key(self, mock_settings):
        """Test OpenRouter requires OPENROUTER_API_KEY (no fallback)."""
        mock_settings.OPENROUTER_API_KEY = None
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="openrouter",
            model="gemini-2.5-pro"
        )
        
        # Should raise ValueError since OpenRouter key is required
        with pytest.raises(ValueError, match="No API key configured"):
            manager.get_provider(workspace)
    
    def test_provider_config_passed_through(self, mock_settings):
        """Test that provider_config is passed to provider."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="openrouter",
            model="gemini-2.5-pro",
            provider_config={"app_name": "Test App"}
        )
        
        provider = manager.get_provider(workspace)
        config = provider.get_environment_config()
        
        assert "ANTHROPIC_EXTRA_HEADERS" in config
    
    def test_custom_cache_key(self, mock_settings):
        """Test using custom cache key."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace",
            provider="openrouter",
            model="gemini-2.5-pro"
        )
        
        manager.get_provider(workspace, cache_key="custom_key")
        
        assert "custom_key" in manager.list_cached_providers()
        assert "openrouter" not in manager.list_cached_providers()


class TestWorkspaceManagerIntegration:
    """Integration tests for WorkspaceManager."""
    
    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = Mock(spec=Settings)
        settings.AGENT_BASE_PATH = "/agent"
        settings.ANTHROPIC_API_KEY = "anthropic-key"
        settings.OPENROUTER_API_KEY = "openrouter-key"
        settings.OPENROUTER_BASE_URL = "https://openrouter.ai/api"
        return settings
    
    def test_multiple_providers_cached_separately(self, mock_settings):
        """Test multiple providers can be cached simultaneously."""
        manager = WorkspaceManager(mock_settings)
        
        workspace1 = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace1",
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        workspace2 = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace2",
            provider="openrouter",
            model="gemini-2.5-pro"
        )
        
        provider1 = manager.get_provider(workspace1)
        provider2 = manager.get_provider(workspace2)
        
        assert isinstance(provider1, AnthropicProvider)
        assert isinstance(provider2, OpenRouterProvider)
        assert len(manager.list_cached_providers()) == 2
    
    def test_same_provider_different_workspaces(self, mock_settings):
        """Test same provider for different workspaces uses cache."""
        manager = WorkspaceManager(mock_settings)
        
        workspace1 = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace1",
            provider="openrouter",
            model="gpt-4o"
        )
        
        workspace2 = WorkspaceSettings(name="test-workspace",
            workspace_path="/agent/workspace2",
            provider="openrouter",
            model="gpt-4o"
        )
        
        provider1 = manager.get_provider(workspace1)
        provider2 = manager.get_provider(workspace2)

        assert provider1 is provider2  # Same cached instance
        assert len(manager.list_cached_providers()) == 1


class TestWorkspacePreparation:
    """Tests for workspace preparation methods."""
    
    @pytest.fixture
    def mock_settings(self):
        """Create mock settings."""
        settings = Mock(spec=Settings)
        settings.AGENT_BASE_PATH = "/agent"
        settings.ANTHROPIC_API_KEY = "test-key"
        settings.STANDARDS_DIR_NAME = "standards"
        settings.ROSETTA_OUTPUT_DIR = "rosetta"
        # Exclude set used by sync_directories / clear_src_directory (must be a real list).
        settings.EXCLUDED_ARTIFACT_PATTERNS = [
            ".git", ".venv", "venv", "__pycache__", "*.pyc", "node_modules",
            "dist", "build", "standards",
        ]
        settings.WORKSPACE_EXCLUDE_PATTERNS = []
        # Plugin provisioning is a no-op here (no plugin path); MCP gate not taken.
        settings.ROSETTA_MCP_ENABLED = False
        settings.ROSETTA_PLUGIN_PATH = None
        return settings

    @pytest.fixture
    def temp_workspace_dir(self):
        """Create a temporary directory for workspace testing."""
        temp_dir = tempfile.mkdtemp()
        yield temp_dir
        shutil.rmtree(temp_dir)
    
    def test_prepare_single_workspace_creates_outputs_dir(self, mock_settings, temp_workspace_dir):
        """Test that prepare_single_workspace creates the outputs directory."""
        manager = WorkspaceManager(mock_settings)
        
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path=temp_workspace_dir,
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        manager.prepare_single_workspace(
            workspace=workspace,
            spec_path="specifications",
            outputs_dir="specflow"
        )
        
        # Check that outputs directory was created
        outputs_path = Path(temp_workspace_dir) / "specflow"
        assert outputs_path.exists()
        assert outputs_path.is_dir()

        gitignore = Path(temp_workspace_dir) / ".gitignore"
        assert gitignore.exists()
        assert "node_modules/" in gitignore.read_text(encoding="utf-8")
    
    def test_copy_standards_to_workspace(self, mock_settings, temp_workspace_dir):
        """Test copying standards files to workspace."""
        manager = WorkspaceManager(mock_settings)
        
        # Create source standards directory
        source_standards = Path(temp_workspace_dir) / "source_standards"
        source_standards.mkdir()
        (source_standards / "commit_standards.md").write_text("# Commit Standards")
        (source_standards / "tech_stacks.md").write_text("# Tech Stacks")
        
        # Create target workspace
        target_workspace_path = Path(temp_workspace_dir) / "workspace1"
        target_workspace_path.mkdir()
        
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path=str(target_workspace_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        manager.copy_standards_to_workspace(workspace, str(source_standards))
        
        # Verify standards were copied
        target_standards = target_workspace_path / "standards"
        assert target_standards.exists()
        assert (target_standards / "commit_standards.md").exists()
        assert (target_standards / "tech_stacks.md").exists()
        assert (target_standards / "commit_standards.md").read_text() == "# Commit Standards"
    
    def test_copy_standards_replaces_existing(self, mock_settings, temp_workspace_dir):
        """Test that copying standards replaces existing standards."""
        manager = WorkspaceManager(mock_settings)
        
        # Create source standards
        source_standards = Path(temp_workspace_dir) / "source_standards"
        source_standards.mkdir()
        (source_standards / "new_file.md").write_text("New content")
        
        # Create target workspace with existing standards
        target_workspace_path = Path(temp_workspace_dir) / "workspace1"
        target_workspace_path.mkdir()
        existing_standards = target_workspace_path / "standards"
        existing_standards.mkdir()
        (existing_standards / "old_file.md").write_text("Old content")
        
        workspace = WorkspaceSettings(
            name="test-workspace",
            workspace_path=str(target_workspace_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        manager.copy_standards_to_workspace(workspace, str(source_standards))
        
        # Verify old file is gone and new file is present
        assert not (existing_standards / "old_file.md").exists()
        assert (existing_standards / "new_file.md").exists()
    
    def test_sync_directories(self, mock_settings, temp_workspace_dir):
        """Test syncing directories between workspaces."""
        manager = WorkspaceManager(mock_settings)
        
        # Create source workspace with content
        source_path = Path(temp_workspace_dir) / "source_ws"
        source_path.mkdir()
        specs_dir = source_path / "specifications"
        specs_dir.mkdir()
        (specs_dir / "spec1.md").write_text("Spec 1")
        (specs_dir / "spec2.md").write_text("Spec 2")
        
        outputs_dir = source_path / "specflow"
        outputs_dir.mkdir()
        (outputs_dir / "output.md").write_text("Output")
        
        # Create target workspace
        target_path = Path(temp_workspace_dir) / "target_ws"
        target_path.mkdir()
        
        source_ws = WorkspaceSettings(
            name="source-workspace",
            workspace_path=str(source_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        target_ws = WorkspaceSettings(
            name="target-workspace",
            workspace_path=str(target_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        manager.sync_directories(
            source_ws=source_ws,
            target_ws=target_ws,
            directories=["specifications", "specflow"]
        )
        
        # Verify directories were synced
        assert (target_path / "specifications" / "spec1.md").exists()
        assert (target_path / "specifications" / "spec2.md").exists()
        assert (target_path / "specflow" / "output.md").exists()
        assert (target_path / "specifications" / "spec1.md").read_text() == "Spec 1"
    
    def test_sync_directories_overwrites_existing(self, mock_settings, temp_workspace_dir):
        """Test that syncing overwrites existing content."""
        manager = WorkspaceManager(mock_settings)
        
        # Create source workspace
        source_path = Path(temp_workspace_dir) / "source_ws"
        source_path.mkdir()
        specs_dir = source_path / "specifications"
        specs_dir.mkdir()
        (specs_dir / "spec.md").write_text("New content")
        
        # Create target workspace with existing file
        target_path = Path(temp_workspace_dir) / "target_ws"
        target_path.mkdir()
        target_specs = target_path / "specifications"
        target_specs.mkdir()
        (target_specs / "spec.md").write_text("Old content")
        (target_specs / "extra.md").write_text("Extra file")
        
        source_ws = WorkspaceSettings(
            name="source-workspace",
            workspace_path=str(source_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        target_ws = WorkspaceSettings(
            name="target-workspace",
            workspace_path=str(target_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        manager.sync_directories(
            source_ws=source_ws,
            target_ws=target_ws,
            directories=["specifications"]
        )
        
        # Verify content was overwritten and extra file was removed
        assert (target_specs / "spec.md").read_text() == "New content"
        assert not (target_specs / "extra.md").exists()
    
    def test_clear_src_directory(self, mock_settings, temp_workspace_dir):
        """Test clearing src directory."""
        manager = WorkspaceManager(mock_settings)
        
        # Create workspace with src directory
        workspace_path = Path(temp_workspace_dir) / "workspace1"
        workspace_path.mkdir()
        src_dir = workspace_path / "src"
        src_dir.mkdir()
        (src_dir / "app.py").write_text("print('hello')")
        (src_dir / "utils.py").write_text("# utils")
        
        workspace = WorkspaceSettings(
            workspace_path=str(workspace_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        manager.clear_src_directory(workspace)
        
        # Verify src directory was removed
        assert not src_dir.exists()
    
    def test_clear_src_directory_no_op_if_not_exists(self, mock_settings, temp_workspace_dir):
        """Test that clearing src directory is no-op if it doesn't exist."""
        manager = WorkspaceManager(mock_settings)
        
        workspace_path = Path(temp_workspace_dir) / "workspace1"
        workspace_path.mkdir()
        
        workspace = WorkspaceSettings(
            workspace_path=str(workspace_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        # Should not raise an error
        manager.clear_src_directory(workspace)
        
        # Verify workspace still exists
        assert workspace_path.exists()
    
    def test_prepare_parallel_workspaces(self, mock_settings, temp_workspace_dir):
        """Test preparing multiple workspaces for parallel execution."""
        manager = WorkspaceManager(mock_settings)
        
        # Create primary workspace with content
        primary_path = Path(temp_workspace_dir) / "primary"
        primary_path.mkdir()
        
        specs_dir = primary_path / "specifications"
        specs_dir.mkdir()
        (specs_dir / "spec.md").write_text("Specification")
        
        outputs_dir = primary_path / "specflow"
        outputs_dir.mkdir()
        (outputs_dir / "output.md").write_text("Output")
        
        # Create src directory that should be cleared in extra workspaces
        src_dir = primary_path / "src"
        src_dir.mkdir()
        (src_dir / "app.py").write_text("# app")
        
        # Create standards source
        standards_source = Path(temp_workspace_dir) / "standards"
        standards_source.mkdir()
        (standards_source / "standards.md").write_text("Standards")
        
        # Create extra workspaces
        extra1_path = Path(temp_workspace_dir) / "extra1"
        extra1_path.mkdir()
        extra1_src = extra1_path / "src"
        extra1_src.mkdir()
        (extra1_src / "old_code.py").write_text("# old code")
        
        extra2_path = Path(temp_workspace_dir) / "extra2"
        extra2_path.mkdir()
        
        primary_ws = WorkspaceSettings(
            workspace_path=str(primary_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )
        
        extra_ws1 = WorkspaceSettings(
            workspace_path=str(extra1_path),
            provider="openrouter",
            model="gpt-4o"
        )
        
        extra_ws2 = WorkspaceSettings(
            workspace_path=str(extra2_path),
            provider="openrouter",
            model="gemini-2.5-pro"
        )
        
        manager.prepare_parallel_workspaces(
            primary_workspace=primary_ws,
            extra_workspaces=[extra_ws1, extra_ws2],
            spec_path="specifications",
            outputs_dir="specflow",
            standards_source=str(standards_source)
        )
        
        # Verify primary workspace has standards and seeded gitignore
        assert (primary_path / "standards" / "standards.md").exists()
        assert (primary_path / ".gitignore").exists()
        assert "node_modules/" in (primary_path / ".gitignore").read_text(encoding="utf-8")
        
        # Verify extra workspace 1
        assert (extra1_path / "specifications" / "spec.md").exists()
        assert (extra1_path / "specflow" / "output.md").exists()
        assert (extra1_path / "standards" / "standards.md").exists()
        assert not (extra1_path / "src").exists()  # src should be cleared
        assert (extra1_path / ".gitignore").exists()
        assert "node_modules/" in (extra1_path / ".gitignore").read_text(encoding="utf-8")
        
        # Verify extra workspace 2
        assert (extra2_path / "specifications" / "spec.md").exists()
        assert (extra2_path / "specflow" / "output.md").exists()
        assert (extra2_path / "standards" / "standards.md").exists()
        assert (extra2_path / ".gitignore").exists()

    def test_prepare_parallel_workspaces_creates_outputs_on_all(self, mock_settings, temp_workspace_dir):
        """prepare_parallel_workspaces ensures specflow/ exists on primary and extra workspaces."""
        mock_settings.ROSETTA_OUTPUT_DIR = "rosetta"
        manager = WorkspaceManager(mock_settings)

        primary_path = Path(temp_workspace_dir) / "primary"
        primary_path.mkdir()
        (primary_path / "specifications").mkdir()
        (primary_path / "specflow").mkdir()
        (primary_path / "rosetta").mkdir()

        extra1_path = Path(temp_workspace_dir) / "extra1"
        extra1_path.mkdir()

        extra2_path = Path(temp_workspace_dir) / "extra2"
        extra2_path.mkdir()

        primary_ws = WorkspaceSettings(
            workspace_path=str(primary_path),
            provider="anthropic",
            model="claude-sonnet-4-0"
        )

        extra_ws1 = WorkspaceSettings(
            workspace_path=str(extra1_path),
            provider="openrouter",
            model="gpt-4o"
        )

        extra_ws2 = WorkspaceSettings(
            workspace_path=str(extra2_path),
            provider="openrouter",
            model="gemini-2.5-pro"
        )

        manager.prepare_parallel_workspaces(
            primary_workspace=primary_ws,
            extra_workspaces=[extra_ws1, extra_ws2],
            spec_path="specifications",
            outputs_dir="specflow"
        )

        for ws_path in [primary_path, extra1_path, extra2_path]:
            assert (ws_path / "specflow").is_dir(), f"specflow/ missing under {ws_path}"

    def test_sync_directories_root_target_preserves_git_and_excludes(
        self, mock_settings, temp_workspace_dir
    ):
        """Scenario: brownfield sync where src resolves to the workspace root (src_dir=".").

        The target workspace's own .git (its origin) MUST be preserved, the source's .git MUST
        NOT be copied in (origin must not flip), build caches MUST be skipped, and files the
        source repo .gitignore lists MUST NOT be propagated. Source code IS copied.
        """
        base = Path(temp_workspace_dir)

        source_path = base / "primary"
        source_path.mkdir()
        (source_path / "app.py").write_text("print('app')")
        (source_path / ".env").write_text("SECRET=1")
        (source_path / ".gitignore").write_text(".env\nnode_modules/\n", encoding="utf-8")
        (source_path / "node_modules").mkdir()
        (source_path / "node_modules" / "lib.js").write_text("x")
        (source_path / ".git").mkdir()
        (source_path / ".git" / "config").write_text("origin = workspace1")

        target_path = base / "extra"
        target_path.mkdir()
        (target_path / ".git").mkdir()
        (target_path / ".git" / "config").write_text("origin = workspace2")

        source_ws = WorkspaceSettings(
            workspace_path=str(source_path), provider="anthropic", model="m"
        )
        target_ws = WorkspaceSettings(
            workspace_path=str(target_path), provider="anthropic", model="m"
        )

        WorkspaceManager(mock_settings).sync_directories(
            source_ws=source_ws, target_ws=target_ws, directories=["."]
        )

        # Source code copied.
        assert (target_path / "app.py").read_text() == "print('app')"
        # Target keeps its OWN origin — not overwritten by the source's .git.
        assert (target_path / ".git" / "config").read_text() == "origin = workspace2"
        # Build caches not copied.
        assert not (target_path / "node_modules").exists()
        # Source-gitignored file not propagated.
        assert not (target_path / ".env").exists()

    def test_sync_directories_excludes_caches_in_subdir(
        self, mock_settings, temp_workspace_dir
    ):
        """Non-root sync keeps replace semantics but still skips build/cache dirs."""
        base = Path(temp_workspace_dir)

        source_path = base / "primary"
        source_path.mkdir()
        src_sub = source_path / "src"
        src_sub.mkdir()
        (src_sub / "app.py").write_text("code")
        (src_sub / "node_modules").mkdir()
        (src_sub / "node_modules" / "x.js").write_text("x")
        (src_sub / "__pycache__").mkdir()
        (src_sub / "__pycache__" / "a.pyc").write_text("y")

        target_path = base / "extra"
        target_path.mkdir()

        source_ws = WorkspaceSettings(
            workspace_path=str(source_path), provider="anthropic", model="m"
        )
        target_ws = WorkspaceSettings(
            workspace_path=str(target_path), provider="anthropic", model="m"
        )

        WorkspaceManager(mock_settings).sync_directories(
            source_ws=source_ws, target_ws=target_ws, directories=["src"]
        )

        assert (target_path / "src" / "app.py").exists()
        assert not (target_path / "src" / "node_modules").exists()
        assert not (target_path / "src" / "__pycache__").exists()

    def test_sync_directories_honors_additive_workspace_exclude_patterns(
        self, mock_settings, temp_workspace_dir
    ):
        """WORKSPACE_EXCLUDE_PATTERNS is ADDED to the built-in defaults, not a replacement."""
        mock_settings.WORKSPACE_EXCLUDE_PATTERNS = ["customcache"]
        base = Path(temp_workspace_dir)

        source_path = base / "primary"
        source_path.mkdir()
        sub = source_path / "src"
        sub.mkdir()
        (sub / "app.py").write_text("code")
        (sub / "customcache").mkdir()
        (sub / "customcache" / "blob.bin").write_text("x")
        (sub / "node_modules").mkdir()  # still excluded via the built-in defaults
        (sub / "node_modules" / "x.js").write_text("y")

        target_path = base / "extra"
        target_path.mkdir()

        source_ws = WorkspaceSettings(
            workspace_path=str(source_path), provider="anthropic", model="m"
        )
        target_ws = WorkspaceSettings(
            workspace_path=str(target_path), provider="anthropic", model="m"
        )

        WorkspaceManager(mock_settings).sync_directories(
            source_ws=source_ws, target_ws=target_ws, directories=["src"]
        )

        assert (target_path / "src" / "app.py").exists()
        assert not (target_path / "src" / "customcache").exists()  # additive pattern
        assert not (target_path / "src" / "node_modules").exists()  # built-in default kept

    def test_clear_src_directory_root_preserves_git(
        self, mock_settings, temp_workspace_dir
    ):
        """clear_src_directory with src_dir="." must never rmtree the root or its .git.

        It removes stale tracked content but preserves .git, dependency caches, and
        gitignored files so the workspace keeps its own VCS identity.
        """
        base = Path(temp_workspace_dir)
        ws_path = base / "extra"
        ws_path.mkdir()
        (ws_path / "stale.py").write_text("# stale generated code")
        (ws_path / ".git").mkdir()
        (ws_path / ".git" / "config").write_text("origin = workspace2")
        (ws_path / "node_modules").mkdir()
        (ws_path / ".gitignore").write_text(".env\n", encoding="utf-8")
        (ws_path / ".env").write_text("SECRET=1")

        workspace = WorkspaceSettings(
            workspace_path=str(ws_path), provider="anthropic", model="m"
        )

        WorkspaceManager(mock_settings).clear_src_directory(workspace, src_dir=".")

        # Root not deleted; identity + caches + gitignored files preserved.
        assert ws_path.exists()
        assert (ws_path / ".git" / "config").read_text() == "origin = workspace2"
        assert (ws_path / "node_modules").exists()
        assert (ws_path / ".env").exists()
        # Stale tracked content removed.
        assert not (ws_path / "stale.py").exists()

    def test_clear_root_preserves_git_even_if_exclude_list_omits_it(
        self, mock_settings, temp_workspace_dir
    ):
        """M1: .git must survive clear even when EXCLUDED_ARTIFACT_PATTERNS is overridden to drop it."""
        mock_settings.EXCLUDED_ARTIFACT_PATTERNS = ["node_modules"]  # NOTE: no ".git"
        ws_path = Path(temp_workspace_dir) / "extra"
        ws_path.mkdir()
        (ws_path / "stale.py").write_text("# stale")
        (ws_path / ".git").mkdir()
        (ws_path / ".git" / "config").write_text("origin = workspace2")

        workspace = WorkspaceSettings(
            workspace_path=str(ws_path), provider="anthropic", model="m"
        )
        WorkspaceManager(mock_settings).clear_src_directory(workspace, src_dir=".")

        assert (ws_path / ".git" / "config").read_text() == "origin = workspace2"
        assert not (ws_path / "stale.py").exists()

    def test_sync_root_preserves_target_git_even_if_exclude_list_omits_it(
        self, mock_settings, temp_workspace_dir
    ):
        """M1: source .git must never overwrite target .git even if config drops the pattern."""
        mock_settings.EXCLUDED_ARTIFACT_PATTERNS = ["node_modules"]  # NOTE: no ".git"
        base = Path(temp_workspace_dir)

        source_path = base / "primary"
        source_path.mkdir()
        (source_path / "app.py").write_text("code")
        (source_path / ".git").mkdir()
        (source_path / ".git" / "config").write_text("origin = workspace1")

        target_path = base / "extra"
        target_path.mkdir()
        (target_path / ".git").mkdir()
        (target_path / ".git" / "config").write_text("origin = workspace2")

        source_ws = WorkspaceSettings(
            workspace_path=str(source_path), provider="anthropic", model="m"
        )
        target_ws = WorkspaceSettings(
            workspace_path=str(target_path), provider="anthropic", model="m"
        )
        WorkspaceManager(mock_settings).sync_directories(
            source_ws=source_ws, target_ws=target_ws, directories=["."]
        )

        assert (target_path / "app.py").read_text() == "code"
        assert (target_path / ".git" / "config").read_text() == "origin = workspace2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
