"""Tests for app.core.rosetta_kb — the single source of truth for the Rosetta KB mode."""

from pathlib import Path
from unittest.mock import Mock

from app.core.config import Settings
from app.core.rosetta_kb import (
    RosettaKbMode,
    resolve_rosetta_kb_mode,
    rosetta_plugin_root,
)


def _settings(mcp_enabled: bool, plugin_path) -> Mock:
    s = Mock(spec=Settings)
    s.ROSETTA_MCP_ENABLED = mcp_enabled
    s.ROSETTA_PLUGIN_PATH = plugin_path
    return s


def test_live_mcp_wins_over_plugin(tmp_path: Path) -> None:
    """ROSETTA_MCP_ENABLED true -> LIVE_MCP even if a plugin dir also exists on disk."""
    s = _settings(True, str(tmp_path))
    assert resolve_rosetta_kb_mode(s) is RosettaKbMode.LIVE_MCP
    # The plugin root helper hides the path in MCP mode so the env var is never set.
    assert rosetta_plugin_root(s) is None


def test_provisioned_plugin_when_path_exists(tmp_path: Path) -> None:
    s = _settings(False, str(tmp_path))
    assert resolve_rosetta_kb_mode(s) is RosettaKbMode.PROVISIONED_PLUGIN
    assert rosetta_plugin_root(s) == str(tmp_path)


def test_configured_but_missing_path_is_disabled_not_plugin(tmp_path: Path) -> None:
    """A set-but-nonexistent path must NOT count as provisioned-plugin mode.

    This is the drift the central resolver fixes: the decision and the actions keyed on it
    (provisioning, CLAUDE_PLUGIN_ROOT, unpack-skip, prompt shape) all require the path on disk.
    """
    s = _settings(False, str(tmp_path / "missing"))
    assert resolve_rosetta_kb_mode(s) is RosettaKbMode.DISABLED
    assert rosetta_plugin_root(s) is None


def test_whitespace_and_none_paths_are_disabled() -> None:
    assert resolve_rosetta_kb_mode(_settings(False, None)) is RosettaKbMode.DISABLED
    assert resolve_rosetta_kb_mode(_settings(False, "   ")) is RosettaKbMode.DISABLED
