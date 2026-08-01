"""Resolve the bundled Rosetta plugin used by knowledge-base initialization."""

from pathlib import Path
from typing import Optional

from app.core.config import Settings


def rosetta_plugin_root(settings: Settings) -> Optional[str]:
    """Return the normalized plugin root when it is an existing directory."""
    plugin_root = (settings.ROSETTA_PLUGIN_PATH or "").strip()
    if not plugin_root or not Path(plugin_root).is_dir():
        return None
    return plugin_root
