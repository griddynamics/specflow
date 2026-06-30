"""Single source of truth for *which* Rosetta knowledge-base source is active.

There are three mutually-exclusive KB sources, modelled by ``RosettaKbMode``:

- ``LIVE_MCP`` — the live ims-mcp ``KnowledgeBase`` server (Grid-Dynamics-internal).
  Off by default; opt in with ``ROSETTA_MCP_ENABLED=true`` (+ ``ROSETTA_API_KEY`` …).
- ``PROVISIONED_PLUGIN`` — the **default**. The Rosetta plugin is baked into the
  backend image at ``ROSETTA_PLUGIN_PATH`` (pod-wide), then *provisioned* into each
  workspace at runtime: ``WorkspaceManager.provision_rosetta_plugin`` copies its
  agents/skills/commands into ``.claude/`` and merges its hooks into
  ``.claude/settings.json``, discovered via ``setting_sources=["project"]``. This is
  NOT the SDK ``plugins=`` loader and NOT a live MCP — the plugin is transiently
  vendored into the project tree. Hence "provisioned plugin".
- ``DISABLED`` — neither MCP nor a usable plugin on disk; KB init no-ops.

**Why this module exists.** The mode predicate used to be re-implemented in four
places (KB-init selection, plugin provisioning, the unpack-skip guard, and the
``CLAUDE_PLUGIN_ROOT`` env setup) and they drifted: the KB-init selection gated on
path *truthiness* while the others required the path to *exist on disk*, so a
misconfigured ``ROSETTA_PLUGIN_PATH`` could make the agent believe the toolset was
provisioned when nothing had been copied. Every caller now funnels through
``resolve_rosetta_kb_mode`` so the decision and the actions keyed on it can never
disagree.

**Removing a source cleanly (the design goal).** Each KB source is one enum value
plus the call-site arms that match it. To delete the live-MCP path in a future PR:
drop ``LIVE_MCP`` here, delete the ``RosettaKbMode.LIVE_MCP`` branch in
``resolve_rosetta_kb_mode``, and remove the (grep-able) ``is LIVE_MCP`` / ``for_step``
MCP arms — no hunting for re-derived predicates.
"""

from enum import Enum
from pathlib import Path
from typing import Optional

from app.core.config import Settings


class RosettaKbMode(str, Enum):
    """Which Rosetta KB source is active for this run. See module docstring."""

    DISABLED = "disabled"
    LIVE_MCP = "live_mcp"
    PROVISIONED_PLUGIN = "provisioned_plugin"


def resolve_rosetta_kb_mode(settings: Settings) -> RosettaKbMode:
    """Resolve the active KB mode from settings — the only place this is decided.

    Precedence: the live MCP wins over the bundled plugin (so an operator can opt
    back into the project-tailored KB). ``PROVISIONED_PLUGIN`` requires the plugin to
    actually exist on disk; a configured-but-missing ``ROSETTA_PLUGIN_PATH`` resolves
    to ``DISABLED`` rather than a half-active plugin state.
    """
    if settings.ROSETTA_MCP_ENABLED:
        return RosettaKbMode.LIVE_MCP
    if _existing_plugin_root(settings.ROSETTA_PLUGIN_PATH) is not None:
        return RosettaKbMode.PROVISIONED_PLUGIN
    return RosettaKbMode.DISABLED


def rosetta_plugin_root(settings: Settings) -> Optional[str]:
    """The on-disk plugin root when ``PROVISIONED_PLUGIN`` mode is active, else ``None``.

    Used by the provisioning copy and by ``CLAUDE_PLUGIN_ROOT`` env injection so both
    read the exact path the mode resolver validated.
    """
    if settings.ROSETTA_MCP_ENABLED:
        return None
    return _existing_plugin_root(settings.ROSETTA_PLUGIN_PATH)


def _existing_plugin_root(plugin_path: Optional[str]) -> Optional[str]:
    """Normalize ``plugin_path`` and return it only if it is an existing directory."""
    plugin_root = (plugin_path or "").strip()
    if not plugin_root or not Path(plugin_root).is_dir():
        return None
    return plugin_root
