"""Unit tests for workspace manager Rosetta plugin provisioning and parallel prep."""

import pytest
from pathlib import Path
from unittest.mock import Mock

from app.core.config import Settings
from app.schemas.workspace import WorkspaceSettings
from app.services.workspace_manager import WorkspaceManager


def _mock_settings() -> Mock:
    settings = Mock(spec=Settings)
    settings.STANDARDS_DIR_NAME = "standards"
    # Exclude set used by sync_directories / clear_src_directory (must be a real list).
    settings.EXCLUDED_ARTIFACT_PATTERNS = [
        ".git", ".venv", "venv", "__pycache__", "*.pyc", "node_modules",
        "dist", "build", "standards",
    ]
    settings.WORKSPACE_EXCLUDE_PATTERNS = []
    # No plugin path by default; provisioning is a no-op unless a test points it at a real dir.
    settings.ROSETTA_PLUGIN_PATH = None
    return settings


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

    def _manager(self, plugin_path) -> WorkspaceManager:
        settings = _mock_settings()
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


class TestPrepareParallelWorkspacesPlugin:
    """prepare_parallel_workspaces: provision the plugin on every workspace and propagate the
    KB agent's outputs (root CLAUDE.md + outputs_dir docs) plus specs from primary to extras.
    """

    def _make_workspace(self, root: Path, name: str, *, populate: bool = False) -> WorkspaceSettings:
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

    def test_provisions_plugin_and_syncs_kb_output_to_all_workspaces(self, tmp_path: Path) -> None:
        plugin_root = tmp_path / "opt" / "rosetta-plugin"
        _make_plugin(plugin_root)
        settings = _mock_settings()
        settings.ROSETTA_PLUGIN_PATH = str(plugin_root)
        manager = WorkspaceManager(settings)

        root = tmp_path / "workspaces"
        root.mkdir()
        primary_ws = self._make_workspace(root, "primary", populate=True)
        extra1_ws = self._make_workspace(root, "extra1")
        extra2_ws = self._make_workspace(root, "extra2")

        # Simulate KB init agent output on primary: CLAUDE.md at root + a doc in outputs_dir.
        (root / "primary" / "CLAUDE.md").write_text("# Project Instructions")
        (root / "primary" / "specflow" / "CONTEXT.md").write_text("# Context")

        manager.prepare_parallel_workspaces(
            primary_workspace=primary_ws,
            extra_workspaces=[extra1_ws, extra2_ws],
            spec_path="specifications",
            outputs_dir="specflow",
        )

        for ws_name in ["primary", "extra1", "extra2"]:
            ws = root / ws_name
            # Plugin discovery trees provisioned into every workspace.
            assert (ws / ".claude" / "agents" / "engineer.md").exists(), f"{ws_name}: agents missing"
            assert (ws / ".claude" / "skills" / "init-workspace-flow" / "SKILL.md").exists()

        # KB output + specs propagated from primary to extras.
        for ws_name in ["extra1", "extra2"]:
            ws = root / ws_name
            assert (ws / "CLAUDE.md").read_text() == "# Project Instructions"
            assert (ws / "specifications" / "spec.md").exists()
            assert (ws / "specflow" / "CONTEXT.md").exists()

    def test_no_kb_output_still_syncs_specs(self, tmp_path: Path) -> None:
        """Scenario: KB init skipped (no CLAUDE.md) -> no crash; specs still reach extras."""
        settings = _mock_settings()  # no plugin path -> provisioning is a no-op
        manager = WorkspaceManager(settings)

        root = tmp_path / "workspaces"
        root.mkdir()
        primary_ws = self._make_workspace(root, "primary", populate=True)
        extra1_ws = self._make_workspace(root, "extra1")

        manager.prepare_parallel_workspaces(
            primary_workspace=primary_ws,
            extra_workspaces=[extra1_ws],
            spec_path="specifications",
            outputs_dir="specflow",
        )

        assert not (root / "extra1" / "CLAUDE.md").exists()
        assert not (root / "extra1" / ".claude").exists()
        assert (root / "extra1" / "specifications" / "spec.md").exists()
