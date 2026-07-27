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
from services.llm_tiers import LLM_TIER_KEYS

# Runtime settings keys, stored in mcp-config.json, in display order. The tier
# keys come from ``services.llm_tiers.LLM_TIER_KEYS`` — the single source of
# truth the MCP server and backend actually read (``LLM_HIGH/MEDIUM/LOW``) — so
# the Settings screen can never again drift onto names nothing consumes.
EDITABLE_KEYS: list[str] = [
    "WORKSPACE_COUNT",
    *LLM_TIER_KEYS,
    "USER_EMAIL",
    "BACKEND_URL",
]

# Superseded tier key names an earlier build wrote into the env block; nothing
# reads them. Purged on save so they never linger or skew a config fingerprint.
_LEGACY_EDITABLE_KEYS: list[str] = ["LLM_MODEL_HIGH", "LLM_MODEL_MEDIUM", "LLM_MODEL_LOW"]

# Secret/identity keys, stored in .env (consumed by docker-compose / the backend
# / the init script). Names match .env.quickstart.example so write_dotenv fills
# the template entries in place rather than appending duplicates.
ENV_SECRET_KEYS: list[str] = [
    "GITHUB_TOKEN",
    "GIT_USER_NAME",
    "GITHUB_ORG",
    "BITBUCKET_TOKEN",
    "BITBUCKET_WORKSPACE",
    "P10Y_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
]

# Advanced/optional secrets, stored in .env. LangFuse captures LLM traces; it is
# all-or-nothing (the backend enables tracing only when all three are set — see
# backend `config.langfuse_enabled`). Kept separate from ENV_SECRET_KEYS so the
# core setup never treats these as required. Names match .env.quickstart.example
# and the docker-compose passthrough so write_dotenv fills them in place.
LANGFUSE_KEYS: list[str] = [
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
]

# Secret keys rendered masked; in Settings a blank masked field means "keep
# the stored value" so editing never wipes a secret you didn't touch.
MASKED_KEYS: frozenset[str] = frozenset(
    {
        "GITHUB_TOKEN",
        "BITBUCKET_TOKEN",
        "P10Y_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "LANGFUSE_SECRET_KEY",
    }
)


def langfuse_partial_error(values: dict[str, str]) -> str | None:
    """Enforce LangFuse's all-or-nothing rule; return an error or None.

    LangFuse tracing needs all three keys together (the backend enables it only
    when public key, secret key and host are all set). A partially-filled set
    would silently disable tracing, so we refuse it at the point of entry. This
    is the single source of truth for the rule, reused by onboarding and Settings.
    """
    present = [key for key in LANGFUSE_KEYS if (values.get(key) or "").strip()]
    if present and len(present) != len(LANGFUSE_KEYS):
        missing = [key for key in LANGFUSE_KEYS if key not in present]
        return "LangFuse needs all three values (or none). Missing: " + ", ".join(missing)
    return None


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
    # Replace only the editable keys (and sweep away any dead legacy tier keys);
    # leave every other env entry untouched.
    for key in (*EDITABLE_KEYS, *_LEGACY_EDITABLE_KEYS):
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
