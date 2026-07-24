"""Tests for Rosetta plugin-path validation."""

from pathlib import Path
from unittest.mock import Mock

from app.core.config import Settings
from app.core.rosetta_kb import rosetta_plugin_root


def _settings(plugin_path) -> Mock:
    s = Mock(spec=Settings)
    s.ROSETTA_PLUGIN_PATH = plugin_path
    return s


def test_provisioned_plugin_when_path_exists(tmp_path: Path) -> None:
    s = _settings(str(tmp_path))
    assert rosetta_plugin_root(s) == str(tmp_path)


def test_configured_but_missing_path_is_unavailable(tmp_path: Path) -> None:
    """A configured path must point to an existing directory."""
    s = _settings(str(tmp_path / "missing"))
    assert rosetta_plugin_root(s) is None


def test_whitespace_and_none_paths_are_unavailable() -> None:
    assert rosetta_plugin_root(_settings(None)) is None
    assert rosetta_plugin_root(_settings("   ")) is None
