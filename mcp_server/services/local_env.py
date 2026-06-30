"""Local self-host environment helpers — the single source of truth for
filesystem/process logic shared by ``cli.cmd_init`` and the TUI onboarding/
startup gates.

Everything here is pure stdlib + ``httpx`` (already a dependency): no
``textual`` import, no service singletons, all imports at module top. The actual
bootstrap work is **not** reimplemented — ``run_init`` wraps the existing
``specflow-init.sh`` and streams its output, keeping the bash script the single
source of truth for what setup does.

Two config stores stay distinct:
  * ``.env``                         — secrets + local config consumed by
    docker-compose / the backend / the init script.
  * ``.specflow-local/mcp-config.json`` — runtime config the MCP client / CLI /
    TUI read (``MCP_CONFIG_FILENAME``).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

# Single source of truth for the runtime-config filename (cli.py re-exports it).
MCP_CONFIG_FILENAME = ".specflow-local/mcp-config.json"

# A directory is the repo root iff it contains ALL of these.
SENTINEL_FILES: tuple[str, ...] = ("specflow-init.sh", "docker-compose.yml")

_ENV_FILENAME = ".env"
_ENV_EXAMPLE_FILENAME = ".env.quickstart.example"
_INIT_SCRIPT = "specflow-init.sh"

# Mirror docker-compose.yml container-name env-var defaults.
_BACKEND_CONTAINER_DEFAULT = "specflow-backend"
_FIRESTORE_CONTAINER_DEFAULT = "specflow-firestore-emulator"


# ---------------------------------------------------------------------------
# Repo-root + path resolution
# ---------------------------------------------------------------------------


def repo_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd) for a dir containing all sentinels.

    Returns the repo root, or ``None`` if no ancestor qualifies. The init flow
    inherently requires a checkout (docker-compose.yml, the script, scripts/),
    so callers surface a clear error when this is ``None`` rather than guessing.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if all((candidate / name).exists() for name in SENTINEL_FILES):
            return candidate
    return None


def env_file_path(root: Path) -> Path:
    return root / _ENV_FILENAME


def env_example_path(root: Path) -> Path:
    return root / _ENV_EXAMPLE_FILENAME


def mcp_config_path(root: Path) -> Path:
    return root / MCP_CONFIG_FILENAME


# ---------------------------------------------------------------------------
# .env (dotenv) read/write — comment- and order-preserving
# ---------------------------------------------------------------------------


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines into a dict.

    Blank lines and ``#`` comments are ignored; surrounding whitespace on the
    key and value is stripped; ``=`` inside the value is preserved (split once);
    last assignment wins. Quotes are not stripped — the script writes bare values.
    """
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def read_dotenv(root: Path) -> dict[str, str]:
    """Parse the project ``.env``; ``{}`` when it does not exist."""
    path = env_file_path(root)
    if not path.exists():
        return {}
    return parse_dotenv(path.read_text())


def write_dotenv(root: Path, updates: dict[str, str], *, template_if_new: bool = True) -> Path:
    """Merge ``updates`` into ``.env`` preserving comment/blank lines and order.

    Existing keys are replaced in place; new keys are appended. When ``.env`` is
    absent and ``template_if_new`` is set, it is seeded from
    ``.env.quickstart.example`` (so all the scaffolding + comments come along)
    before applying ``updates``. Creates parent dirs. Returns the path.
    """
    path = env_file_path(root)
    if path.exists():
        original = path.read_text()
    elif template_if_new and env_example_path(root).exists():
        original = env_example_path(root).read_text()
    else:
        original = ""

    remaining = dict(updates)
    out_lines: list[str] = []
    for raw in original.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out_lines.append(f"{key}={remaining.pop(key)}")
                continue
        out_lines.append(raw)

    for key, value in remaining.items():
        out_lines.append(f"{key}={value}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out_lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Setup detection
# ---------------------------------------------------------------------------


def env_exists(root: Path) -> bool:
    return env_file_path(root).exists()


def mcp_config_exists(root: Path) -> bool:
    return mcp_config_path(root).exists()


def is_setup_complete(root: Path) -> bool:
    """True once both setup artifacts exist: ``.env`` and the mcp-config."""
    return env_exists(root) and mcp_config_exists(root)


# ---------------------------------------------------------------------------
# Docker container detection + control
# ---------------------------------------------------------------------------


def _container_names() -> tuple[str, str]:
    backend = os.getenv("SPECFLOW_BACKEND_CONTAINER", _BACKEND_CONTAINER_DEFAULT)
    firestore = os.getenv("SPECFLOW_FIRESTORE_CONTAINER", _FIRESTORE_CONTAINER_DEFAULT)
    return backend, firestore


def containers_running(root: Path | None = None) -> bool:
    """True iff BOTH SpecFlow containers are currently running.

    Uses ``docker ps`` filtered by the compose container names. A missing docker
    CLI or any error is treated as "not running" (the caller then offers to
    start them, which surfaces the real failure with streamed output).
    """
    backend, firestore = _container_names()
    try:
        completed = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={backend}",
                "--filter",
                f"name={firestore}",
                "--format",
                "{{.Names}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return False
    if completed.returncode != 0:
        return False
    names = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    return backend in names and firestore in names


# ---------------------------------------------------------------------------
# Backend health
# ---------------------------------------------------------------------------


async def backend_ready(backend_url: str, *, timeout_seconds: float = 3.0) -> bool:
    """True iff ``GET {backend_url}/health/ready`` returns 200.

    Swallows every error (connection refused / 503 while starting / timeout) →
    ``False``. Uses ``httpx`` directly to stay decoupled from the backend service
    singleton (which reads env at import time).
    """
    url = f"{backend_url.rstrip('/')}/health/ready"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url)
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def wait_backend_ready(
    backend_url: str,
    *,
    retries: int = 60,
    interval: float = 2.0,
    on_attempt: Callable[[int], None] | None = None,
) -> bool:
    """Poll ``backend_ready`` until ready or ``retries`` exhausted.

    Mirrors the init script's 60×2s health gate. ``on_attempt(i)`` is called
    before each attempt so a caller can stream progress. Returns ``True`` on
    ready, ``False`` on timeout.
    """
    for attempt in range(1, retries + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        if await backend_ready(backend_url):
            return True
        if attempt < retries:
            await asyncio.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Subprocess streaming (one shared pattern)
# ---------------------------------------------------------------------------


async def _stream_subprocess(
    argv: list[str], cwd: Path, on_line: Callable[[str], None] | None
) -> int:
    """Run ``argv`` in ``cwd``, streaming combined stdout/stderr line-by-line."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        if on_line is not None:
            on_line(raw.decode(errors="replace"))
    return await proc.wait()


async def start_containers(root: Path, on_line: Callable[[str], None] | None = None) -> int:
    """Start the SpecFlow stack (``docker compose up -d --no-build``), streamed.

    Matches ``make run-detached`` and the script's compose v2 usage. Returns the
    process exit code; non-zero surfaces through the streamed output.
    """
    return await _stream_subprocess(["docker", "compose", "up", "-d", "--no-build"], root, on_line)


# ---------------------------------------------------------------------------
# specflow-init.sh wrapper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InitFlags:
    """Flags passed through to ``specflow-init.sh`` (only non-defaults emitted)."""

    max_parallel_runs: int | None = None
    skip_build: bool = False
    reset_local_db: bool = False
    provide_own_repos: str | None = None
    dry_run: bool = False

    def to_argv(self) -> list[str]:
        argv: list[str] = []
        if self.max_parallel_runs is not None:
            argv += ["--max-parallel-runs", str(self.max_parallel_runs)]
        if self.skip_build:
            argv.append("--skip-build")
        if self.reset_local_db:
            argv.append("--reset-local-db")
        if self.provide_own_repos:
            argv += ["--provide-own-repos", self.provide_own_repos]
        if self.dry_run:
            argv.append("--dry-run")
        return argv


async def run_init(
    root: Path, flags: InitFlags, on_line: Callable[[str], None] | None = None
) -> int:
    """Run ``bash ./specflow-init.sh <flags>`` from ``root``, streaming output.

    The script owns all state mutation (docker up, repo provisioning, firestore
    seed, mcp-config write); this only invokes and streams it. Returns the exit
    code.
    """
    argv = ["bash", str(root / _INIT_SCRIPT), *flags.to_argv()]
    return await _stream_subprocess(argv, root, on_line)
