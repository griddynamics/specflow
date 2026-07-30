"""OS-level agent sandbox for ``BACKEND_RUNTIME=process``.

In ``docker`` mode the container is the isolation boundary and this module is a
no-op (Docker behaviour is unchanged). In ``process`` mode the backend runs on
the bare host, so the container boundary is gone; we substitute Claude Code's
built-in OS-level Bash sandbox (bubblewrap on Linux, Apple Seatbelt on macOS),
enabled per agent query via ``ClaudeAgentOptions.sandbox``.

This confines each agent's Bash subprocesses — the exact surface that escapes the
in-process allowlist and PreToolUse guard (``agent_hooks.py`` notes "``python
script.py`` is not caught … the real boundary is the sandbox"). It is an added
OS-enforced layer, **not** a replacement for the existing in-process controls
(defense in depth).

Security posture: **fail closed**. When process mode is active but the sandbox
cannot initialise (missing dependency / unsupported OS), ``run_generation``
refuses synchronously rather than running agents unconfined on the host.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from claude_agent_sdk import SandboxNetworkConfig, SandboxSettings

from app.core.config import WORKSPACE_CACHE_SUBDIR, settings
from app.core.enums import BackendRuntime

# Curated network allowlist for sandboxed agent Bash commands (allow-only). Kept
# deliberately tight — see the domain-fronting / exfiltration warning in Claude
# Code's sandbox docs: only the package registries and git host the supported
# toolchains need to fetch dependencies during generation. The LLM API is NOT
# listed because the Claude CLI process itself runs OUTSIDE the Bash sandbox, so
# model connectivity is unaffected by this list. Override via
# ``settings.AGENT_SANDBOX_ALLOWED_DOMAINS`` (comma-separated).
DEFAULT_AGENT_SANDBOX_ALLOWED_DOMAINS: tuple[str, ...] = (
    # Python
    "pypi.org",
    "files.pythonhosted.org",
    # Node
    "registry.npmjs.org",
    # Go
    "proxy.golang.org",
    "sum.golang.org",
    # Java / Gradle / Android
    "repo.maven.apache.org",
    "repo1.maven.org",
    "plugins.gradle.org",
    "services.gradle.org",
    "dl.google.com",
    # Dart / Flutter
    "pub.dev",
    # Git host + release/object CDNs
    "github.com",
    "*.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    # Shared object storage used by several toolchains (go, flutter, gradle)
    "storage.googleapis.com",
)

# Commands known to be incompatible with the sandbox → run outside it. ``docker``
# is documented-incompatible; deploy/QA agents already gate docker/kubectl
# separately (see tool_usage.DEPLOY_BASH_USAGE). Kept minimal per the exfil warning.
_SANDBOX_EXCLUDED_COMMANDS: tuple[str, ...] = ("docker",)


@dataclass(frozen=True)
class SandboxUnavailable:
    """Why the OS sandbox can't run on this host, with an actionable fix message."""

    dependency: str
    message: str


def _allowed_domains() -> list[str]:
    raw = settings.AGENT_SANDBOX_ALLOWED_DOMAINS
    if raw and raw.strip():
        return [domain.strip() for domain in raw.split(",") if domain.strip()]
    return list(DEFAULT_AGENT_SANDBOX_ALLOWED_DOMAINS)


def get_agent_sandbox_settings() -> SandboxSettings | None:
    """``SandboxSettings`` for agent queries, or ``None`` when no OS sandbox is engaged.

    Returns ``None`` in DOCKER mode (the container is the boundary — behaviour is
    unchanged). In PROCESS mode returns a fail-closed, allow-only sandbox that
    confines agent Bash subprocesses (and their children) at the OS level. Writes
    are confined by the SDK to the query ``cwd`` (already the workspace) plus the
    session temp dir, reinforcing the existing ``Write/Edit({workspace}/**)``
    allowlist at the OS level.
    """
    if settings.BACKEND_RUNTIME != BackendRuntime.PROCESS:
        return None
    network: SandboxNetworkConfig = {"allowedDomains": _allowed_domains()}
    return SandboxSettings(
        enabled=True,
        autoAllowBashIfSandboxed=True,
        # Fail closed: no dangerouslyDisableSandbox escape hatch — a command that
        # cannot run sandboxed fails rather than silently running on the bare host.
        allowUnsandboxedCommands=False,
        excludedCommands=list(_SANDBOX_EXCLUDED_COMMANDS),
        network=network,
    )


def get_agent_sandbox_write_allowlist() -> list[str]:
    """Extra ``Edit``/``Write`` tool rules that widen the sandbox writable set in
    process mode; empty in docker mode (no sandbox engaged).

    The OS Bash sandbox only allows writes to the query ``cwd`` (the workspace) and
    the session temp dir. But SpecFlow redirects every tool cache
    (``setup_workspace_cache_directories``) to ``{WORKSPACE_BASE_PATH}/caches/…``,
    which sits OUTSIDE ``cwd`` — so ``npm install`` / ``pip install`` / ``go mod
    download`` would be denied write access under the sandbox. The Claude Agent SDK
    intentionally has no ``SandboxSettings.filesystem`` field and directs filesystem
    write scope through ``Edit`` allow-rules (see ``SandboxSettings`` docstring),
    which merge into the subprocess writable set. Granting the caches subtree here
    is strictly tighter than docker mode (where bash writes are unrestricted).
    """
    if settings.BACKEND_RUNTIME != BackendRuntime.PROCESS:
        return []
    caches_root = os.path.join(settings.WORKSPACE_BASE_PATH, WORKSPACE_CACHE_SUBDIR)
    # Same single-slash absolute glob form as workspace_usage() in claude_code.py.
    return [f"Edit({caches_root}/**)", f"Write({caches_root}/**)"]


def check_agent_sandbox_available() -> SandboxUnavailable | None:
    """Return ``None`` if the OS sandbox can run on this host, else why not.

    - macOS: Seatbelt via the built-in ``sandbox-exec``.
    - Linux: bubblewrap (``bwrap``) for filesystem/namespace isolation and
      ``socat`` for the network proxy relay.

    Only meaningful in PROCESS mode; DOCKER mode always returns ``None`` (the
    container is the boundary, so host sandbox tooling is irrelevant).
    """
    if settings.BACKEND_RUNTIME != BackendRuntime.PROCESS:
        return None
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec") is None:
            return SandboxUnavailable(
                dependency="sandbox-exec",
                message=(
                    "macOS sandbox tool `sandbox-exec` was not found on PATH. It ships "
                    "with macOS — ensure /usr/bin is on PATH."
                ),
            )
        return None
    if sys.platform.startswith("linux"):
        missing = [dep for dep in ("bwrap", "socat") if shutil.which(dep) is None]
        if missing:
            return SandboxUnavailable(
                dependency=", ".join(missing),
                message=(
                    f"Linux sandbox dependencies missing: {', '.join(missing)}. Install with "
                    "`sudo apt-get install bubblewrap socat` (Debian/Ubuntu) or "
                    "`sudo dnf install bubblewrap socat` (Fedora). On Ubuntu 24.04+ you may "
                    "also need an AppArmor profile allowing unprivileged user namespaces for "
                    "`bwrap` (see docs/backend/backend-runtime.md)."
                ),
            )
        return None
    return SandboxUnavailable(
        dependency=sys.platform,
        message=(
            f"The agent OS sandbox is not supported on this platform ({sys.platform}). Use "
            "BACKEND_RUNTIME=docker, or run the backend on macOS or Linux."
        ),
    )
