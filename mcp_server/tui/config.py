"""Read/write the local MCP config — the single source of truth for settings.

The settings screen edits the same ``.specflow-local/mcp-config.json`` env block
the MCP server and CLI already read (``cli._load_mcp_config``), so IDE and
terminal never diverge. Reading reuses that existing helper; this module adds
the matching writer, which preserves every other key in the document and only
replaces the ``mcpServers.specflow.env`` block.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli import _MCP_CONFIG_FILENAME, _load_mcp_config
from services import local_env

# Runtime settings keys, stored in mcp-config.json, in display order.
EDITABLE_KEYS: list[str] = [
    "WORKSPACE_COUNT",
    "LLM_MODEL_HIGH",
    "LLM_MODEL_MEDIUM",
    "LLM_MODEL_LOW",
    "USER_EMAIL",
    "BACKEND_URL",
]

# Secret/identity keys, stored in .env (consumed by docker-compose / the backend
# / the init script). Names match .env.quickstart.example so write_dotenv fills
# the template entries in place rather than appending duplicates.
ENV_SECRET_KEYS: list[str] = [
    "GITHUB_TOKEN",
    "GIT_USER_NAME",
    "GITHUB_ORG",
    "P10Y_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
]

# Secret keys rendered masked; in Settings a blank masked field means "keep
# the stored value" so editing never wipes a secret you didn't touch.
MASKED_KEYS: frozenset[str] = frozenset(
    {
        "GITHUB_TOKEN",
        "P10Y_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
    }
)


def config_path(root: Path) -> Path:
    return root / _MCP_CONFIG_FILENAME


def load_env(root: Path) -> dict[str, Any]:
    """Return the ``mcpServers.specflow.env`` block (reuses the CLI reader)."""
    return _load_mcp_config(root)


def save_env(root: Path, env: dict[str, str]) -> Path:
    """Persist ``env`` into the config file, preserving all other content.

    Creates the file (and ``.specflow-local/``) with a minimal skeleton if it
    does not exist yet. Empty values are dropped so cleared fields fall back to
    defaults rather than being stored as blank strings.
    """
    path = config_path(root)
    if path.exists():
        doc = json.loads(path.read_text())
    else:
        doc = {}

    cleaned = {k: v for k, v in env.items() if v not in (None, "")}

    servers = doc.setdefault("mcpServers", {})
    specflow = servers.setdefault("specflow", {})
    existing_env = specflow.get("env")
    merged = existing_env if isinstance(existing_env, dict) else {}
    # Replace only the editable keys; leave any other env entries untouched.
    for key in EDITABLE_KEYS:
        merged.pop(key, None)
    merged.update(cleaned)
    specflow["env"] = merged

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return path


def load_env_secrets(root: Path) -> dict[str, str]:
    """Return the secret/identity values from ``.env`` (``{}`` if absent)."""
    return local_env.read_dotenv(root)


def save_env_secrets(root: Path, updates: dict[str, str]) -> Path:
    """Persist secret/identity ``updates`` into ``.env``, preserving everything else."""
    return local_env.write_dotenv(root, updates, template_if_new=True)
