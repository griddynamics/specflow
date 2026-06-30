"""Unit tests for workspace manager rosetta unpack and sync functionality."""

import pytest
from pathlib import Path
from unittest.mock import Mock

from app.core.config import Settings
from app.schemas.workspace import WorkspaceSettings
from app.services.workflow_steps import WorkflowContext, sync_specs_to_workspaces
from app.services.workspace_manager import WorkspaceManager


def _mock_settings() -> Mock:
    settings = Mock(spec=Settings)
    settings.STANDARDS_DIR_NAME = "standards"
    settings.ROSETTA_OUTPUT_DIR = "rosetta"
    # Exclude set used by sync_directories / clear_src_directory (must be a real list).
    settings.EXCLUDED_ARTIFACT_PATTERNS = [
        ".git", ".venv", "venv", "__pycache__", "*.pyc", "node_modules",
        "dist", "build", "standards",
    ]
    settings.WORKSPACE_EXCLUDE_PATTERNS = []
    # Plugin provisioning is a no-op in these unpack-focused tests (no plugin path).
    settings.ROSETTA_MCP_ENABLED = False
    settings.ROSETTA_PLUGIN_PATH = None
    return settings


def _populate_rosetta(ws_root: Path) -> None:
    """Create a representative rosetta/ directory structure in a workspace."""
    rosetta = ws_root / "rosetta"
    rosetta.mkdir(exist_ok=True)
    (rosetta / "CLAUDE.md").write_text("# Project Instructions")

    agents = rosetta / "agents"
    agents.mkdir(parents=True)
    (agents / "backend-agent.md").write_text("---\nname: backend\n---")
    (agents / "frontend-agent.md").write_text("---\nname: frontend\n---")

    docs = rosetta / "docs"
    docs.mkdir()
    (docs / "CONTEXT.md").write_text("# Context")
    (docs / "ARCHITECTURE.md").write_text("# Architecture")


class TestUnpackRosettaArtifacts:
    """Tests for WorkspaceManager.unpack_rosetta_artifacts()."""

    @pytest.fixture
    def workspace_dir(self, tmp_path: Path) -> Path:
        ws = tmp_path / "workspace"
        ws.mkdir()
        return ws

    @pytest.fixture
    def mock_settings(self) -> Mock:
        return _mock_settings()

    @pytest.fixture
    def workspace(self, workspace_dir: Path) -> WorkspaceSettings:
        return WorkspaceSettings(
            name="test-ws",
            workspace_path=str(workspace_dir),
            provider="anthropic",
            model="claude-sonnet-4-0",
        )

    @pytest.fixture
    def manager(self, mock_settings: Mock) -> WorkspaceManager:
        return WorkspaceManager(mock_settings)

    def test_noop_when_rosetta_missing(
        self, manager: WorkspaceManager, workspace: WorkspaceSettings,
    ) -> None:
        """Scenario: no rosetta/ directory -> no-op, no error."""
        manager.unpack_rosetta_artifacts(workspace)
        ws_root = Path(workspace.get_isolated_root())
        assert not (ws_root / "CLAUDE.md").exists()

    def test_copies_files_correctly(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: rosetta/ has CLAUDE.md and docs/ -> unpacked to workspace root."""
        rosetta = workspace_dir / "rosetta"
        rosetta.mkdir()
        (rosetta / "CLAUDE.md").write_text("# Project Instructions")
        docs = rosetta / "docs"
        docs.mkdir()
        (docs / "CONTEXT.md").write_text("# Context")

        manager.unpack_rosetta_artifacts(workspace)

        assert (workspace_dir / "CLAUDE.md").read_text() == "# Project Instructions"
        assert (workspace_dir / "docs" / "CONTEXT.md").read_text() == "# Context"

    def test_agents_dir_remapped_to_dot_claude(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: rosetta/agents/ is remapped to workspace_root/.claude/agents/ on unpack.

        The KB init agent writes to rosetta/agents/ (not rosetta/.claude/agents/) to avoid
        the SDK sensitive-file guard. unpack_rosetta_artifacts remaps this to .claude/agents/.
        """
        rosetta = workspace_dir / "rosetta"
        agents_dir = rosetta / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "backend-agent.md").write_text("---\nname: backend\n---")

        manager.unpack_rosetta_artifacts(workspace)

        unpacked = workspace_dir / ".claude" / "agents" / "backend-agent.md"
        assert unpacked.exists(), "rosetta/agents/ must be remapped to .claude/agents/"
        assert "backend" in unpacked.read_text()
        # rosetta/agents/ must NOT be copied literally to workspace_root/agents/
        assert not (workspace_dir / "agents").exists()

    def test_skills_dir_remapped_to_dot_claude(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: rosetta/skills/ is remapped to workspace_root/.claude/skills/."""
        rosetta = workspace_dir / "rosetta"
        skills = rosetta / "skills" / "my-skill"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("---\nname: my-skill\n---\n")

        manager.unpack_rosetta_artifacts(workspace)

        unpacked = workspace_dir / ".claude" / "skills" / "my-skill" / "SKILL.md"
        assert unpacked.exists()
        assert "my-skill" in unpacked.read_text()
        assert not (workspace_dir / "skills").exists()

    def test_commands_dir_remapped_to_dot_claude(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: rosetta/commands/ is remapped to workspace_root/.claude/commands/."""
        rosetta = workspace_dir / "rosetta"
        cmd_dir = rosetta / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "review.md").write_text("# /review command\n")

        manager.unpack_rosetta_artifacts(workspace)

        unpacked = workspace_dir / ".claude" / "commands" / "review.md"
        assert unpacked.exists()
        assert not (workspace_dir / "commands").exists()

    def test_agents_remap_preserves_content(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: multiple agent files in rosetta/agents/ all land in .claude/agents/."""
        rosetta = workspace_dir / "rosetta"
        agents_dir = rosetta / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "frontend.md").write_text("---\nname: frontend\n---\ncontent")
        (agents_dir / "backend.md").write_text("---\nname: backend\n---\ncontent")

        manager.unpack_rosetta_artifacts(workspace)

        dot_claude_agents = workspace_dir / ".claude" / "agents"
        assert (dot_claude_agents / "frontend.md").read_text() == "---\nname: frontend\n---\ncontent"
        assert (dot_claude_agents / "backend.md").read_text() == "---\nname: backend\n---\ncontent"

    def test_overwrites_existing_target(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: target directory exists -> rosetta/ content is merged in.

        Pre-existing files (e.g. planning outputs such as IMPLEMENTATION_PLAN.md)
        must be preserved.  Rosetta files are added/updated alongside them.
        """
        (workspace_dir / "docs").mkdir()
        (workspace_dir / "docs" / "OLD.md").write_text("old content")

        rosetta = workspace_dir / "rosetta"
        docs = rosetta / "docs"
        docs.mkdir(parents=True)
        (docs / "NEW.md").write_text("new content")

        manager.unpack_rosetta_artifacts(workspace)

        assert (workspace_dir / "docs" / "NEW.md").read_text() == "new content"
        # OLD.md (e.g. a planning file written before unpack) must survive.
        assert (workspace_dir / "docs" / "OLD.md").read_text() == "old content"

    def test_custom_rosetta_dir_name(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: custom rosetta directory name -> uses that name."""
        custom = workspace_dir / "my_kb"
        custom.mkdir()
        (custom / "CLAUDE.md").write_text("custom")

        manager.unpack_rosetta_artifacts(workspace, rosetta_dir="my_kb")

        assert (workspace_dir / "CLAUDE.md").read_text() == "custom"

    def test_rosetta_dir_preserved_after_unpack(
        self,
        manager: WorkspaceManager,
        workspace: WorkspaceSettings,
        workspace_dir: Path,
    ) -> None:
        """Scenario: unpack copies content but original rosetta/ stays intact."""
        _populate_rosetta(workspace_dir)

        manager.unpack_rosetta_artifacts(workspace)

        assert (workspace_dir / "rosetta" / "CLAUDE.md").exists()
        assert (workspace_dir / "CLAUDE.md").exists()


class TestPrepareParallelWorkspacesRosetta:
    """Tests for rosetta/ sync + unpack integration in prepare_parallel_workspaces.

    Scenario: KB init agent produced rosetta/ on primary workspace. When
    prepare_parallel_workspaces runs it should:
    1. Unpack rosetta/ on the primary workspace
    2. Sync rosetta/ directory to each extra workspace
    3. Unpack rosetta/ on each extra workspace
    Result: every workspace has CLAUDE.md, .claude/agents/, docs/ at root.
    """

    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        return tmp_path

    @pytest.fixture
    def manager(self) -> WorkspaceManager:
        return WorkspaceManager(_mock_settings())

    def _make_workspace(
        self, root: Path, name: str, *, populate: bool = False,
    ) -> WorkspaceSettings:
        ws_path = root / name
        ws_path.mkdir(exist_ok=True)
        if populate:
            (ws_path / "specifications").mkdir()
            (ws_path / "specifications" / "spec.md").write_text("spec")
            (ws_path / "specflow").mkdir()
            (ws_path / "specflow" / "output.md").write_text("output")
        return WorkspaceSettings(
            name=name,
            workspace_path=str(ws_path),
            provider="anthropic",
            model="claude-sonnet-4-0",
        )

    def test_rosetta_synced_and_unpacked_on_all_workspaces(
        self, root: Path, manager: WorkspaceManager,
    ) -> None:
        """Scenario: primary has rosetta/ -> all workspaces get SDK artifacts."""
        primary_ws = self._make_workspace(root, "primary", populate=True)
        extra1_ws = self._make_workspace(root, "extra1")
        extra2_ws = self._make_workspace(root, "extra2")

        _populate_rosetta(root / "primary")

        manager.prepare_parallel_workspaces(
            primary_workspace=primary_ws,
            extra_workspaces=[extra1_ws, extra2_ws],
            spec_path="specifications",
            outputs_dir="specflow",
        )

        for ws_name in ["primary", "extra1", "extra2"]:
            ws = root / ws_name
            assert (ws / "CLAUDE.md").exists(), f"{ws_name}: CLAUDE.md missing"
            assert (ws / "CLAUDE.md").read_text() == "# Project Instructions"
            assert (ws / ".claude" / "agents" / "backend-agent.md").exists(), (
                f"{ws_name}: backend-agent.md missing"
            )
            assert (ws / ".claude" / "agents" / "frontend-agent.md").exists(), (
                f"{ws_name}: frontend-agent.md missing"
            )
            assert (ws / "docs" / "CONTEXT.md").exists(), (
                f"{ws_name}: docs/CONTEXT.md missing"
            )
            assert (ws / "docs" / "ARCHITECTURE.md").exists(), (
                f"{ws_name}: docs/ARCHITECTURE.md missing"
            )
            assert (ws / "rosetta" / "CLAUDE.md").exists(), (
                f"{ws_name}: rosetta/ source should be preserved"
            )

    def test_no_rosetta_no_unpack(
        self, root: Path, manager: WorkspaceManager,
    ) -> None:
        """Scenario: KB init skipped, no rosetta/ -> no crash, no SDK artifacts."""
        primary_ws = self._make_workspace(root, "primary", populate=True)
        extra_ws = self._make_workspace(root, "extra1")

        manager.prepare_parallel_workspaces(
            primary_workspace=primary_ws,
            extra_workspaces=[extra_ws],
            spec_path="specifications",
            outputs_dir="specflow",
        )

        for ws_name in ["primary", "extra1"]:
            ws = root / ws_name
            assert not (ws / "CLAUDE.md").exists()
            assert not (ws / ".claude").exists()

        assert (root / "extra1" / "specifications" / "spec.md").exists()
        assert (root / "extra1" / "specflow" / "output.md").exists()

    def test_rosetta_does_not_clobber_existing_specs(
        self, root: Path, manager: WorkspaceManager,
    ) -> None:
        """Scenario: rosetta/docs/ should not destroy existing spec or specflow dirs."""
        primary_ws = self._make_workspace(root, "primary", populate=True)
        extra_ws = self._make_workspace(root, "extra1")

        _populate_rosetta(root / "primary")

        manager.prepare_parallel_workspaces(
            primary_workspace=primary_ws,
            extra_workspaces=[extra_ws],
            spec_path="specifications",
            outputs_dir="specflow",
        )

        assert (root / "extra1" / "specifications" / "spec.md").exists()
        assert (root / "extra1" / "specflow" / "output.md").exists()
        assert (root / "extra1" / "CLAUDE.md").exists()


class TestSyncSpecsToWorkspacesRosettaIntegration:
    """End-to-end tests for the full KB-init → sync pipeline.

    Exercises sync_specs_to_workspaces() — the actual workflow step function —
    against real filesystem directories (tmp_path).  No mocking of workspace
    manager or filesystem operations.

    What's verified:
      1. Artifacts written by KB init agent (rosetta/) are synced from the
         primary workspace to every extra workspace.
      2. Artifacts are unpacked to workspace root on every workspace, including
         primary, so agents can discover CLAUDE.md and .claude/agents/ via SDK.
      3. Specs and outputs directories are also synced (rosetta is additive).
      4. If rosetta/ is absent (KB init skipped) the step still completes
         cleanly and specs are still synced.
    """

    def _make_ws(
        self,
        root: Path,
        name: str,
        *,
        specs: bool = False,
    ) -> WorkspaceSettings:
        ws_path = root / name
        ws_path.mkdir(exist_ok=True)
        if specs:
            (ws_path / "specifications").mkdir()
            (ws_path / "specifications" / "spec.md").write_text("spec content")
            (ws_path / "specflow").mkdir()
            (ws_path / "specflow" / "output.md").write_text("output content")
        return WorkspaceSettings(
            name=name,
            workspace_path=str(ws_path),
            provider="anthropic",
            model="claude-sonnet-4-0",
        )

    def _make_ctx(
        self,
        tmp_path: Path,
        primary_ws: WorkspaceSettings,
        extra_workspaces: list,
        *,
        standards_source: str | None = None,
    ) -> WorkflowContext:
        settings = _mock_settings()
        settings.AGENT_BASE_PATH = str(tmp_path)
        settings.WORKSPACE_BASE_PATH = str(tmp_path)
        settings.STANDARDS_SOURCE_PATH = standards_source or ""
        manager = WorkspaceManager(settings)
        request = Mock()
        request.spec_path = "specifications"
        request.outputs_dir = "specflow"
        request.src_dir = "src"
        return WorkflowContext(
            request=request,
            settings=settings,
            logger=Mock(),
            workspace_ids=[primary_ws.name] + [w.name for w in extra_workspaces],
            workspaces=[primary_ws] + extra_workspaces,
            workspace_manager=manager,
            primary_workspace=primary_ws,
            extra_workspaces=extra_workspaces,
        )

    @pytest.mark.asyncio
    async def test_rosetta_artifacts_reach_all_workspaces_after_sync(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: KB init populated rosetta/ on primary. After sync_specs_to_workspaces,
        every workspace (primary and all extras) has the unpacked SDK artifacts."""
        primary = self._make_ws(tmp_path, "primary", specs=True)
        extra1 = self._make_ws(tmp_path, "extra1")
        extra2 = self._make_ws(tmp_path, "extra2")

        # Simulate KB init agent output on primary workspace
        _populate_rosetta(tmp_path / "primary")

        ctx = self._make_ctx(tmp_path, primary, [extra1, extra2])
        await sync_specs_to_workspaces(ctx)

        for ws_name in ["primary", "extra1", "extra2"]:
            ws = tmp_path / ws_name
            assert (ws / "CLAUDE.md").exists(), f"{ws_name}: CLAUDE.md not unpacked"
            assert (ws / "CLAUDE.md").read_text() == "# Project Instructions"
            assert (ws / ".claude" / "agents" / "backend-agent.md").exists(), (
                f"{ws_name}: backend-agent.md not unpacked"
            )
            assert (ws / ".claude" / "agents" / "frontend-agent.md").exists(), (
                f"{ws_name}: frontend-agent.md not unpacked"
            )
            assert (ws / "docs" / "CONTEXT.md").exists(), (
                f"{ws_name}: docs/CONTEXT.md not unpacked"
            )

    @pytest.mark.asyncio
    async def test_artifact_content_is_identical_across_workspaces(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: every workspace must receive byte-for-byte identical content
        from rosetta/ so agents work from the same KB snapshot."""
        primary = self._make_ws(tmp_path, "primary", specs=True)
        extra1 = self._make_ws(tmp_path, "extra1")
        extra2 = self._make_ws(tmp_path, "extra2")

        _populate_rosetta(tmp_path / "primary")

        ctx = self._make_ctx(tmp_path, primary, [extra1, extra2])
        await sync_specs_to_workspaces(ctx)

        for ws_name in ["extra1", "extra2"]:
            ws = tmp_path / ws_name
            assert (ws / "CLAUDE.md").read_text() == (tmp_path / "primary" / "CLAUDE.md").read_text()
            assert (ws / ".claude" / "agents" / "backend-agent.md").read_text() == (
                tmp_path / "primary" / ".claude" / "agents" / "backend-agent.md"
            ).read_text()
            # Verify rosetta/agents/ source files are not exposed at workspace root
            assert not (ws / "agents").exists()

    @pytest.mark.asyncio
    async def test_no_rosetta_sync_completes_cleanly(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: KB init was skipped (no rosetta/ dir). sync step must not
        crash and specs must still be synced to extra workspaces."""
        primary = self._make_ws(tmp_path, "primary", specs=True)
        extra1 = self._make_ws(tmp_path, "extra1")

        # No rosetta/ directory — KB init was skipped
        ctx = self._make_ctx(tmp_path, primary, [extra1])
        await sync_specs_to_workspaces(ctx)  # must not raise

        # SDK artifacts absent (KB was skipped)
        assert not (tmp_path / "extra1" / "CLAUDE.md").exists()
        # Specs still synced
        assert (tmp_path / "extra1" / "specifications" / "spec.md").exists()

    @pytest.mark.asyncio
    async def test_rosetta_dir_name_comes_from_settings(
        self, tmp_path: Path,
    ) -> None:
        """Scenario: ROSETTA_OUTPUT_DIR setting controls which directory is
        treated as KB artifacts source — not a hardcoded 'rosetta' string."""
        primary = self._make_ws(tmp_path, "primary", specs=True)
        extra1 = self._make_ws(tmp_path, "extra1")

        # Use a custom rosetta dir name via settings
        custom_rosetta = tmp_path / "primary" / "kb_artifacts"
        custom_rosetta.mkdir()
        (custom_rosetta / "CLAUDE.md").write_text("# Custom KB")

        ctx = self._make_ctx(tmp_path, primary, [extra1])
        ctx.settings.ROSETTA_OUTPUT_DIR = "kb_artifacts"

        await sync_specs_to_workspaces(ctx)

        assert (tmp_path / "primary" / "CLAUDE.md").read_text() == "# Custom KB"
        assert (tmp_path / "extra1" / "CLAUDE.md").read_text() == "# Custom KB"


def _make_plugin(plugin_root: Path) -> None:
    """Build a representative bundled Rosetta plugin tree at plugin_root."""
    (plugin_root / ".claude-plugin").mkdir(parents=True)
    # The real Rosetta plugin relocates slash-commands to ./workflows/ via the manifest;
    # provision_rosetta_plugin reads this to map workflows/ -> .claude/commands/.
    (plugin_root / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "rosetta", "commands": "./workflows/"}'
    )

    agents = plugin_root / "agents"
    agents.mkdir()
    (agents / "engineer.md").write_text("---\nname: engineer\n---")

    skills = plugin_root / "skills" / "init-workspace-flow"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# init-workspace-flow")

    workflows = plugin_root / "workflows"
    workflows.mkdir()
    (workflows / "init-workspace-flow.md").write_text("# workflow")

    hooks = plugin_root / "hooks"
    hooks.mkdir()
    (hooks / "hooks.json").write_text(
        '{"hooks": {"SessionStart": [{"matcher": "startup", '
        '"hooks": [{"type": "command", "command": "echo hi"}]}]}}'
    )


class TestProvisionRosettaPlugin:
    """Tests for WorkspaceManager.provision_rosetta_plugin()."""

    @pytest.fixture
    def workspace_dir(self, tmp_path: Path) -> Path:
        ws = tmp_path / "workspace"
        ws.mkdir()
        return ws

    @pytest.fixture
    def workspace(self, workspace_dir: Path) -> WorkspaceSettings:
        return WorkspaceSettings(
            name="test-ws",
            workspace_path=str(workspace_dir),
            provider="anthropic",
            model="claude-sonnet-4-0",
        )

    @pytest.fixture
    def plugin_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "opt" / "rosetta-plugin"
        _make_plugin(root)
        return root

    def _manager(self, plugin_path, mcp_enabled: bool = False) -> WorkspaceManager:
        settings = _mock_settings()
        settings.ROSETTA_MCP_ENABLED = mcp_enabled
        settings.ROSETTA_PLUGIN_PATH = str(plugin_path) if plugin_path else None
        return WorkspaceManager(settings)

    def test_copies_agents_skills_commands_into_dot_claude(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: plugin mode -> agents/skills + workflows->commands land in .claude/."""
        assert self._manager(plugin_root).provision_rosetta_plugin(workspace) is True

        claude = workspace_dir / ".claude"
        assert (claude / "agents" / "engineer.md").exists()
        assert (claude / "skills" / "init-workspace-flow" / "SKILL.md").exists()
        # plugin.json commands: ./workflows/ -> .claude/commands/
        assert (claude / "commands" / "init-workspace-flow.md").exists()

    def test_does_not_copy_full_plugin_tree_into_workspace(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: only discovery trees are copied; the plugin's own files (hooks scripts,
        rules/, templates/, .claude-plugin/) stay in the image and are reached via
        CLAUDE_PLUGIN_ROOT — nothing is duplicated into the workspace."""
        self._manager(plugin_root).provision_rosetta_plugin(workspace)

        # No full-tree copy anywhere in the workspace.
        assert not (workspace_dir / ".rosetta-plugin").exists()
        assert not (workspace_dir / ".claude" / "rosetta-plugin").exists()
        assert not (workspace_dir / ".claude" / "hooks").exists()
        assert not (workspace_dir / ".claude" / ".claude-plugin").exists()

    def test_merges_hooks_into_settings_json(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: plugin hooks are registered in .claude/settings.json under project scope."""
        self._manager(plugin_root).provision_rosetta_plugin(workspace)

        import json
        settings_data = json.loads(
            (workspace_dir / ".claude" / "settings.json").read_text()
        )
        assert "SessionStart" in settings_data["hooks"]

    def test_preserves_existing_settings_json(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: an existing settings.json key is preserved when hooks are merged in."""
        import json
        claude = workspace_dir / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps({"model": "opus"}))

        self._manager(plugin_root).provision_rosetta_plugin(workspace)

        settings_data = json.loads((claude / "settings.json").read_text())
        assert settings_data["model"] == "opus"
        assert "SessionStart" in settings_data["hooks"]

    def test_second_call_is_idempotent_no_duplicate_hooks(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: provisioning twice re-converges (copy + dedup) without duplicating hooks."""
        import json
        manager = self._manager(plugin_root)
        manager.provision_rosetta_plugin(workspace)
        manager.provision_rosetta_plugin(workspace)

        settings_data = json.loads(
            (workspace_dir / ".claude" / "settings.json").read_text()
        )
        # The single SessionStart entry must not be appended twice.
        assert len(settings_data["hooks"]["SessionStart"]) == 1

    def test_noop_when_plugin_path_unset(
        self, workspace: WorkspaceSettings, workspace_dir: Path,
    ) -> None:
        """Scenario: no ROSETTA_PLUGIN_PATH -> no-op, nothing written."""
        assert self._manager(None).provision_rosetta_plugin(workspace) is False
        assert not (workspace_dir / ".claude").exists()

    def test_noop_when_mcp_enabled(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: live MCP enabled -> plugin is NOT provisioned (MCP supplies the KB)."""
        manager = self._manager(plugin_root, mcp_enabled=True)
        assert manager.provision_rosetta_plugin(workspace) is False
        assert not (workspace_dir / ".claude").exists()

    def test_noop_when_plugin_path_missing_on_disk(
        self, workspace: WorkspaceSettings, workspace_dir: Path, tmp_path: Path,
    ) -> None:
        """Scenario: configured path doesn't exist -> no-op, no error."""
        manager = self._manager(tmp_path / "does-not-exist")
        assert manager.provision_rosetta_plugin(workspace) is False
        assert not (workspace_dir / ".claude").exists()

    def test_command_dir_follows_manifest_not_hardcoded(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: a plugin that relocates commands to a non-default dir is honored.

        The manifest declares commands at ./cmds/; provision must copy that tree (not the
        conventional ./commands/ or ./workflows/) into .claude/commands/.
        """
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "rosetta", "commands": "./cmds/"}'
        )
        cmds = plugin_root / "cmds"
        cmds.mkdir()
        (cmds / "custom-flow.md").write_text("# custom")

        self._manager(plugin_root).provision_rosetta_plugin(workspace)

        assert (workspace_dir / ".claude" / "commands" / "custom-flow.md").exists()

    def test_falls_back_to_default_map_when_manifest_unreadable(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: a corrupt plugin.json -> default layout (workflows->commands) still applies."""
        (plugin_root / ".claude-plugin" / "plugin.json").write_text("{not valid json")

        assert self._manager(plugin_root).provision_rosetta_plugin(workspace) is True
        # Default fallback maps workflows/ -> .claude/commands/.
        assert (workspace_dir / ".claude" / "commands" / "init-workspace-flow.md").exists()

    def test_overlapping_manifest_dirs_do_not_drop_a_tree(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: a manifest pointing commands at ./skills/ must not collide with the skills
        mapping and silently drop a tree (regression for the old dict-keyed-on-source map).

        With ordered (src, dest) pairs, skills/ is copied to BOTH .claude/skills/ and
        .claude/commands/ — no entry is lost to a dict-key clash.
        """
        (plugin_root / ".claude-plugin" / "plugin.json").write_text(
            '{"name": "rosetta", "commands": "./skills/"}'
        )

        assert self._manager(plugin_root).provision_rosetta_plugin(workspace) is True

        claude = workspace_dir / ".claude"
        # skills/ still lands in .claude/skills/ ...
        assert (claude / "skills" / "init-workspace-flow" / "SKILL.md").exists()
        # ... AND in .claude/commands/ (the manifest's declared commands source) — not dropped.
        assert (claude / "commands" / "init-workspace-flow" / "SKILL.md").exists()
        # agents are unaffected.
        assert (claude / "agents" / "engineer.md").exists()

    def test_unpack_is_noop_when_plugin_mode_active(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: plugin mode -> unpack_rosetta_artifacts skips (plugin provisions .claude/).

        A staged rosetta/ tree must NOT be remapped in plugin mode; the plugin handles .claude/
        and the agent writes docs to their final locations directly.
        """
        # Stage a rosetta/ tree as the MCP-mode agent would.
        rosetta = workspace_dir / "rosetta"
        (rosetta / "agents").mkdir(parents=True)
        (rosetta / "agents" / "stale.md").write_text("---\nname: stale\n---")
        (rosetta / "CLAUDE.md").write_text("# staged")

        self._manager(plugin_root).unpack_rosetta_artifacts(workspace, "rosetta")

        # Nothing remapped: no .claude/agents from rosetta/, no root CLAUDE.md from staging.
        assert not (workspace_dir / ".claude" / "agents" / "stale.md").exists()
        assert not (workspace_dir / "CLAUDE.md").exists()

    def test_unpack_runs_in_mcp_mode(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: MCP mode -> unpack remaps the staged rosetta/ tree into .claude/ and root."""
        rosetta = workspace_dir / "rosetta"
        (rosetta / "agents").mkdir(parents=True)
        (rosetta / "agents" / "engineer.md").write_text("---\nname: engineer\n---")
        (rosetta / "CLAUDE.md").write_text("# project")

        self._manager(plugin_root, mcp_enabled=True).unpack_rosetta_artifacts(workspace, "rosetta")

        assert (workspace_dir / ".claude" / "agents" / "engineer.md").exists()
        assert (workspace_dir / "CLAUDE.md").read_text() == "# project"

    def test_malformed_hook_entries_are_non_fatal(
        self, workspace: WorkspaceSettings, workspace_dir: Path, plugin_root: Path,
    ) -> None:
        """Scenario: a non-list event value in hooks.json must not abort provisioning."""
        (plugin_root / "hooks" / "hooks.json").write_text(
            '{"hooks": {"SessionStart": null, '
            '"PreToolUse": [{"type": "command", "command": "echo ok"}]}}'
        )

        # Must not raise; well-formed events are still merged.
        assert self._manager(plugin_root).provision_rosetta_plugin(workspace) is True

        import json
        settings_data = json.loads(
            (workspace_dir / ".claude" / "settings.json").read_text()
        )
        assert "PreToolUse" in settings_data["hooks"]
