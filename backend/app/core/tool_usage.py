"""
Tool usage configuration for Claude Code agents.

This module defines allowed and disallowed tools for agents, including
dynamic generation of disallowed patterns for common dependency and build directories.
"""

from typing import List

from app.core.config import MCP_FIGMA_SERVER_KEY, MCP_PLAYWRIGHT, ROSETTA_SERVER_KEY
from app.core.ttl_config import GenerationLifecyclePolicy


# Standard tool operations.
# NOTE: the Claude Code CLI removed the `LS` tool (directory listing folded into
# `Glob`/`Bash`); naming it in a permission rule makes the CLI warn on stderr
# ("matches no known tool"). Keep this to tools the CLI actually recognizes.
TOOL_OPERATIONS = ["Read", "Glob"]


# ---------------------------------------------------------------------------
# Per-Bash-tool-call timeouts forwarded to the Claude Code CLI as the env vars
# BASH_DEFAULT_TIMEOUT_MS / BASH_MAX_TIMEOUT_MS. The default applies when the
# agent does not specify a timeout; the max is a hard ceiling the agent cannot
# exceed. Hung dev servers / watch modes / `gh run watch` get killed at the CLI
# layer with an error returned to the agent, so a single bad command can no
# longer consume the whole 5 h phase budget.
# ---------------------------------------------------------------------------
def _validate_bash_timeouts(
    default_ms: int,
    max_ms: int,
    phase_timeout_seconds: int,
) -> None:
    """Validate the Bash-timeout invariants. Raises ValueError when broken.

    Invariant: a single Bash call must terminate well before the wall-clock
    phase timeout. Otherwise one hung subprocess silently burns the phase
    budget. Uses raise (not assert) so it survives ``python -O``.
    """
    if default_ms > max_ms:
        raise ValueError(
            f"BASH_DEFAULT_TIMEOUT_MS ({default_ms}ms) must be <= "
            f"BASH_MAX_TIMEOUT_MS ({max_ms}ms)"
        )
    if max_ms >= phase_timeout_seconds * 1000:
        raise ValueError(
            f"BASH_MAX_TIMEOUT_MS ({max_ms}ms) must be < "
            f"AGENT_PHASE_TIMEOUT_SECONDS ({phase_timeout_seconds * 1000}ms); "
            f"otherwise one hung Bash call can consume the whole phase budget."
        )


BASH_DEFAULT_TIMEOUT_MS: int = 10 * 60 * 1000   # 10 min
BASH_MAX_TIMEOUT_MS: int = 30 * 60 * 1000       # 30 min

# Enforce the invariant at module load on the shipped values.
_validate_bash_timeouts(
    default_ms=BASH_DEFAULT_TIMEOUT_MS,
    max_ms=BASH_MAX_TIMEOUT_MS,
    phase_timeout_seconds=GenerationLifecyclePolicy.AGENT_PHASE_TIMEOUT_SECONDS,
)


def _generate_disallowed_patterns(
    patterns: List[str],
    include_root: bool = True,
    include_nested: bool = True,
) -> List[str]:
    """
    Generate disallowed tool patterns for given directory/file patterns.
    
    For each pattern, generates all combinations of:
    - Operations: Read, Glob
    - Locations: root-level (optional) and nested (*/pattern) (optional)
    
    Args:
        patterns: List of directory/file patterns to disallow (e.g., [".venv", "node_modules"])
        include_root: Whether to include root-level patterns (e.g., "Read(.venv)")
        include_nested: Whether to include nested patterns (e.g., "Read(*/.venv)")
        
    Returns:
        List of disallowed tool strings
        
    Example:
        >>> _generate_disallowed_patterns([".venv"], include_root=True, include_nested=True)
        ['Read(.venv)', 'Glob(.venv)', 'Read(*/.venv)', 'Glob(*/.venv)']
    """
    result = []
    for pattern in patterns:
        if include_root:
            for operation in TOOL_OPERATIONS:
                result.append(f"{operation}({pattern})")
        if include_nested:
            for operation in TOOL_OPERATIONS:
                result.append(f"{operation}(*/{pattern})")
    return result


def get_disallowed_tools() -> List[str]:
    """
    Get the complete list of disallowed tools for agents.
    
    This function dynamically generates patterns for common dependency directories,
    build artifacts, IDE files, and system files that should not be accessed by agents.
    
    Returns:
        List of disallowed tool strings
    """
    disallowed = []
    
    # Version control
    disallowed.extend(_generate_disallowed_patterns([".git"]))
    
    # Python virtual environments and caches
    disallowed.extend(_generate_disallowed_patterns([
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
    ]))
    
    # Node.js dependencies
    disallowed.extend(_generate_disallowed_patterns([
        "node_modules",
        ".npm",
    ]))
    
    # PHP dependencies
    disallowed.extend(_generate_disallowed_patterns(["vendor"]))
    
    # Java build artifacts and dependencies
    disallowed.extend(_generate_disallowed_patterns([
        "target",
        ".gradle",
        ".m2",
    ]))
    
    # Go dependencies (vendor is nested-only, .go is both root and nested)
    disallowed.extend(_generate_disallowed_patterns(
        ["vendor"],
        include_root=False,  # Go vendor is typically nested
        include_nested=True
    ))
    disallowed.extend(_generate_disallowed_patterns([".go"]))
    
    # Rust build artifacts (target is both root and nested)
    disallowed.extend(_generate_disallowed_patterns(["target"]))
    
    # Build and distribution directories
    disallowed.extend(_generate_disallowed_patterns([
        "build",
        "dist",
    ]))
    
    # IDE and editor directories
    disallowed.extend(_generate_disallowed_patterns([
        ".idea",
        ".vscode",
    ]))
    
    # System files
    disallowed.extend(_generate_disallowed_patterns([
        ".DS_Store",
        "Thumbs.db",
    ]))
    
    # `rm` and `chmod` are NOT globally disallowed: rm is granted per-workspace via
    # get_workspace_rm_bash_allowlist() and chmod as `chmod +x` in bash_usage. A global deny
    # here would silently void those grants (deny overrides allow).
    dangerous_bash_commands = [
        "Bash(sudo:*)",
        "Bash(chown:*)",
    ]
    disallowed.extend(dangerous_bash_commands)

    # SDK tools that have no meaning in an unattended pipeline. AskUserQuestion
    # would suspend the agent waiting for a human reply that will never come,
    # hanging the phase until the wall-clock timeout fires.
    disallowed.append("AskUserQuestion")

    return disallowed


# Standard allowed tool sets
skill_usage = [
    "Skill"
]

skill_sources = ["project"]  # Load Skills from filesystem

bash_usage = [
    # Shell essentials
    "Bash(echo:*)",
    "Bash(printf:*)",
    "Bash(export:*)",
    "Bash(env:*)",
    "Bash(which:*)",
    "Bash(type:*)",
    # File operations
    "Bash(ls:*)",
    "Bash(mkdir:*)",
    "Bash(cp:*)",
    "Bash(mv:*)",
    "Bash(touch:*)",
    # Literal prefix match — `chmod +x` grants execute-bit only; broader modes are not covered.
    "Bash(chmod +x:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    # Search / text processing
    "Bash(grep:*)",
    "Bash(find:*)",
    "Bash(tree:*)",
    "Bash(xargs:*)",
    "Bash(sed:*)",
    "Bash(awk:*)",
    "Bash(sort:*)",
    "Bash(uniq:*)",
    "Bash(wc:*)",
    "Bash(jq:*)",
    # Networking (read-only — curl used for health checks and API calls)
    "Bash(curl:*)",
    # Navigation
    "Bash(cd:*)",
    # Build systems
    "Bash(make:*)",
    # Python tooling
    "Bash(python3:*)",
    "Bash(python:*)",
    "Bash(pytest:*)",
    "Bash(uv:*)",
    "Bash(pip:*)",
    "Bash(pip3:*)",
    # Node.js/TypeScript tooling
    "Bash(npm:*)",
    "Bash(npx:*)",
    "Bash(node:*)",
    "Bash(yarn:*)",
    "Bash(pnpm:*)",
    "Bash(tsc:*)",
    "Bash(ts-node:*)",
    # Go tooling
    "Bash(go:*)",
    "Bash(gofmt:*)",
    "Bash(gotest:*)",
    # Java / Kotlin / Android tooling.
    # Multiple gradlew spellings are listed because each is a distinct literal prefix —
    # `sh gradlew` and `./gradlew` are different strings to the allowlist matcher.
    # adb/avdmanager/emulator are deploy/QA-only (ANDROID_SDK_BASH_USAGE); sdkmanager is operator-only.
    "Bash(java:*)",
    "Bash(javac:*)",
    "Bash(mvn:*)",
    "Bash(gradle:*)",
    "Bash(./gradlew:*)",
    "Bash(sh gradlew:*)",
    "Bash(bash gradlew:*)",
    "Bash(sh ./gradlew:*)",
    "Bash(bash ./gradlew:*)",
    "Bash(kotlin:*)",
    "Bash(kotlinc:*)",
    # Flutter / Dart tooling
    "Bash(flutter:*)",
    "Bash(dart:*)",
    # Version control
    "Bash(git:*)",
]

# GitHub CLI tools — only granted to deploy/QA agents.
# NOT included in bash_usage so generation agents don't get gh access.
GH_CLI_USAGE = [
    "Bash(gh:*)",
]

# Deploy/QA-specific tools added on top of bash_usage + GH_CLI_USAGE.
# Agents generate infra code and trigger GitHub Actions — they do NOT deploy directly.
# docker/docker-compose: broad access for local image builds and smoke tests.
# kubectl/helm/terraform: restricted to docs lookup and local validation only (no cluster/state access).
DEPLOY_BASH_USAGE = [
    "Bash(docker:*)",
    "Bash(docker-compose:*)",
    "Bash(wget:*)",
    # kubectl — docs and dry-run validation only
    "Bash(kubectl explain:*)",
    "Bash(kubectl help:*)",
    # helm — local chart validation only
    "Bash(helm lint:*)",
    "Bash(helm template:*)",
    "Bash(helm help:*)",
    # terraform — local config validation only
    "Bash(terraform validate:*)",
    "Bash(terraform fmt:*)",
    "Bash(terraform help:*)",
]

# Deploy/QA-only device tools. sdkmanager is absent — the shared Android SDK is provisioned
# once by an operator via init-mobile-sdk.sh; agents must never install SDK packages.
ANDROID_SDK_BASH_USAGE = [
    "Bash(adb:*)",
    "Bash(avdmanager:*)",
    "Bash(emulator:*)",
]

DEPLOY_EXTRA_TOOLS = GH_CLI_USAGE + DEPLOY_BASH_USAGE + ANDROID_SDK_BASH_USAGE


MCP_TOOL_PREFIX = "mcp__"
_MCP_TOOL_WILDCARD = "__*"


class McpToolSet:
    """Common interface for MCP server tool allowlist construction.

    Each subclass declares ``_tools`` as a list of tool-name prefixes (e.g.
    ``"mcp__playwright"``).  ``get_tools()`` appends the wildcard so callers
    always receive the fully-expanded allowlist string expected by the SDK.
    """

    _tools: List[str]

    @classmethod
    def get_tools(cls) -> List[str]:
        return [t + _MCP_TOOL_WILDCARD for t in cls._tools]


class _RosettaKbMcpTools(McpToolSet):
    _tools = [f"{MCP_TOOL_PREFIX}{ROSETTA_SERVER_KEY}"]


# Microsoft @playwright/mcp — server key MCP_PLAYWRIGHT.
class _PlaywrightMcpTools(McpToolSet):
    _tools = [f"{MCP_TOOL_PREFIX}{MCP_PLAYWRIGHT}"]


# figma-developer-mcp — server key MCP_FIGMA_SERVER_KEY (see app.core.mcp_config.build_figma_mcp_config).
class _FigmaMcpTools(McpToolSet):
    _tools = [f"{MCP_TOOL_PREFIX}{MCP_FIGMA_SERVER_KEY}"]


def get_rosetta_kb_tools() -> List[str]:
    return _RosettaKbMcpTools.get_tools()


def get_rosetta_plugin_tools() -> List[str]:
    """Tools the KB init agent needs in plugin mode (no MCP server).

    The Rosetta plugin ships its init-workspace flow as Agent Skills / slash-commands,
    so the agent drives initialization via ``Skill`` / ``SlashCommand`` instead of the
    ``mcp__KnowledgeBase__*`` tools used in MCP mode. ``Skill`` is also in
    ``skill_usage`` (folded into ``get_common_allowed_tools``); listed here so the
    plugin-mode allowlist is self-describing.
    """
    return ["Skill", "SlashCommand"]


def get_playwright_mcp_tools() -> List[str]:
    return _PlaywrightMcpTools.get_tools()


def get_figma_mcp_tools() -> List[str]:
    return _FigmaMcpTools.get_tools()


def get_rosetta_allowed_tools(workspace_path: str, rosetta_dir: str) -> List[str]:
    """
    Get allowed tools for KB init agent to write to rosetta/ output directory.

    The agent stages all output under rosetta/ (no .claude/ in any path) to avoid
    the SDK's hardcoded sensitive-file guard. Unpack remaps rosetta/agents/,
    rosetta/skills/, and rosetta/commands/ to .claude/agents/, .claude/skills/, and
    .claude/commands/ after the agent finishes.

    Args:
        workspace_path: The workspace root path (e.g., "/workspaces/workspaceName")
        rosetta_dir: The rosetta output directory name (e.g., "rosetta")

    Returns:
        List of allowed tool strings for rosetta/ directory access
    """
    rosetta_path = f"{workspace_path}/{rosetta_dir}"
    return [
        f"Read({rosetta_path}/**)",
        f"Write({rosetta_path}/**)",
        f"Edit({rosetta_path}/**)",
        f"StrReplace({rosetta_path}/**)",
        f"Glob({rosetta_path}/**)",
    ]

def get_workspace_rm_bash_allowlist(workspace_path: str) -> List[str]:
    """
    Allow ``rm`` only under the isolated workspace (e.g. deleting ``*_part*.md`` after ``cat`` merge).

    Not part of ``bash_usage`` / ``get_common_allowed_tools``; callers add this for agents whose
    prompts instruct merge-then-delete of part files under the workspace.
    """
    root = workspace_path.rstrip("/")
    return [f"Bash(rm:{root}/**)"]


# Generate disallowed_tools dynamically
disallowed_tools = get_disallowed_tools()
