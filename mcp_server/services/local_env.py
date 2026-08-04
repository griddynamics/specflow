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
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

# Single source of truth for the runtime-config filename (cli.py re-exports it).
MCP_CONFIG_FILENAME = ".specflow-local/mcp-config.json"

# A directory is the repo root iff it contains ALL of these.
SENTINEL_FILES: tuple[str, ...] = ("specflow-init.sh", "docker-compose.yml")

_ENV_FILENAME = ".env"
_ENV_EXAMPLE_FILENAME = ".env.quickstart.example"
_INIT_SCRIPT = "specflow-init.sh"

# Mirror docker-compose.yml container-name env-var defaults; sqlite has no separate container.
_BACKEND_CONTAINER_DEFAULT = "specflow-backend"

# Per-user SpecFlow home (``~/.specflow``) — the machine-wide state dir already
# shared by the central SQLite db and the connected-AI-tools config
# (``tui.mcp_clients`` reuses this same constant for ~/.specflow/config.json). This
# is the single source of truth for the directory name.
SPECFLOW_HOME_DIRNAME = ".specflow"

# The launcher's remembered runtime choice (docker | process) is machine-wide (the
# backend is a singleton), so it lives under ~/.specflow — NOT per-checkout — which
# lets `switch runtime` from any clone agree on one backend. It is a launcher fact,
# NOT an MCP-server setting (the MCP server only calls backend_url and is
# indifferent to how the backend is launched), so it is never written to
# mcp-config.json. The process-mode control surface (pidfile, log, start/stop)
# lives in ``services.local_backend_process``.
_BACKEND_RUNTIME_BASENAME = "backend-runtime"


def specflow_home_dir(home: Path | None = None) -> Path:
    """The per-user SpecFlow home (``~/.specflow``); ``home`` injectable for tests."""
    return (home or Path.home()) / SPECFLOW_HOME_DIRNAME


class BackendRuntime(StrEnum):
    """Where/how the backend service is launched (mcp_server view).

    Byte-identical to the backend's ``app.core.enums.BackendRuntime``; the two
    packages can't import each other, so — like the MCP-side run_generation
    precheck mirroring the backend contract validator — this is a deliberate,
    minimal duplication of the shared string contract.
    """

    DOCKER = "docker"
    PROCESS = "process"

    @classmethod
    def parse(cls, raw: str | None) -> "BackendRuntime":
        """Case-insensitive parse; unknown/empty → DOCKER (the safe default)."""
        if raw:
            value = raw.strip().lower()
            for member in cls:
                if member.value == value:
                    return member
        return cls.DOCKER

    @classmethod
    def parse_strict(cls, raw: str | None) -> "BackendRuntime | None":
        """Like :meth:`parse` but returns ``None`` for unknown/empty instead of
        defaulting to DOCKER — lets callers tell "never chosen" from "chose docker"."""
        if raw:
            value = raw.strip().lower()
            for member in cls:
                if member.value == value:
                    return member
        return None


def backend_runtime_path(home: Path | None = None) -> Path:
    return specflow_home_dir(home) / _BACKEND_RUNTIME_BASENAME


def read_saved_runtime(home: Path | None = None) -> "BackendRuntime | None":
    """The runtime the launcher's first-run chooser persisted, or ``None``.

    ``None`` means "not chosen yet" (file absent or unrecognized) — deliberately
    distinct from ``BackendRuntime.DOCKER`` so the startup gate can decide whether
    to prompt.
    """
    try:
        raw = backend_runtime_path(home).read_text()
    except OSError:
        return None
    return BackendRuntime.parse_strict(raw)


def save_backend_runtime(runtime: "BackendRuntime", home: Path | None = None) -> Path:
    """Persist the launcher's runtime choice under ``~/.specflow`` (beside the
    pidfile). Not written to mcp-config.json — see the module constants."""
    path = backend_runtime_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(runtime.value)
    return path


# ---------------------------------------------------------------------------
# Repo-root + path resolution
# ---------------------------------------------------------------------------


def _find_sentinel_root(start: Path) -> Path | None:
    """Nearest ancestor of ``start`` (inclusive) containing all SENTINEL_FILES."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if all((candidate / name).exists() for name in SENTINEL_FILES):
            return candidate
    return None


def repo_root(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default cwd) for a dir containing all sentinels.

    Returns the repo root, or ``None`` if no ancestor qualifies. The init flow
    inherently requires a checkout (docker-compose.yml, the script, scripts/),
    so callers surface a clear error when this is ``None`` rather than guessing.
    """
    return _find_sentinel_root(start or Path.cwd())


def installed_repo_root() -> Path | None:
    """The checkout this install's own code lives in, or ``None``.

    With ``uv tool install --editable ./mcp_server`` (the documented install)
    these modules are imported straight from the clone, so ``__file__`` lands
    inside the checkout — found with no cwd dependency and no setup step. Returns
    ``None`` for a non-editable / PyPI install, where the source sits in
    site-packages rather than a checkout.
    """
    return _find_sentinel_root(Path(__file__).resolve().parent)


def resolve_repo_root(start: Path | None = None) -> Path | None:
    """Locate the checkout: walk up from ``start`` (cwd), else this install's own.

    Lets ``specflow`` commands find the self-host checkout from any directory
    once installed editable — running from inside a different checkout still
    wins, otherwise we fall back to the clone the binary was installed from.
    """
    return repo_root(start) or installed_repo_root()


def resolve_project_root(root_path_arg: str | Path | None) -> Path:
    """Return the absolute project root the CLI's ``--root-path`` names.

    Distinct from ``resolve_repo_root`` above: that one hunts for the SpecFlow
    *checkout* the self-host bootstrap needs, this one is simply "the user's
    project", defaulting to cwd. Lives here rather than in ``cli`` so the command
    groups registered from ``services/`` can honour the same flag without
    importing their host back.
    """
    if root_path_arg:
        return Path(root_path_arg).expanduser().resolve()
    return Path.cwd().resolve()


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


def containers_running(root: Path | None = None) -> bool:
    """True iff the SpecFlow backend container is currently running.

    Uses ``docker ps`` filtered by the compose container name. A missing docker
    CLI or any error is treated as "not running" (the caller then offers to
    start them, which surfaces the real failure with streamed output).
    """
    backend = os.getenv("SPECFLOW_BACKEND_CONTAINER", _BACKEND_CONTAINER_DEFAULT)
    try:
        completed = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                f"name={backend}",
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
    return backend in names


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


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a timeout-bounded subprocess run (see ``run_command``)."""

    returncode: int
    output: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        """True only when the command exited 0 within the timeout."""
        return self.returncode == 0 and not self.timed_out


async def run_command(
    argv: list[str],
    cwd: Path,
    on_line: Callable[[str], None] | None = None,
    timeout: float = 30.0,
) -> CommandResult:
    """Run ``argv`` with a hard ``timeout``, capturing combined stdout/stderr.

    Unlike ``_stream_subprocess`` — used for self-terminating, user-watched
    commands like ``docker compose up`` / ``specflow-init.sh`` — this is for
    MCP-client registration probes such as ``claude mcp get`` that may block
    indefinitely on a network socket while producing no output. On timeout the
    child is **killed and reaped** (never left as a zombie) and ``timed_out`` is
    set, so a stuck probe can never freeze the caller. Output lines are collected
    into ``output`` and, when given, forwarded to ``on_line`` for live display.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    lines: list[str] = []

    async def _run() -> int:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            lines.append(line)
            if on_line is not None:
                on_line(line)
        return await proc.wait()

    try:
        returncode = await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CommandResult(returncode=-1, output="".join(lines), timed_out=True)
    return CommandResult(returncode=returncode, output="".join(lines), timed_out=False)


async def start_containers(root: Path, on_line: Callable[[str], None] | None = None) -> int:
    """Start the SpecFlow stack (``docker compose up -d --no-build``), streamed.

    Matches ``make run-detached`` and the script's compose v2 usage. Returns the
    process exit code; non-zero surfaces through the streamed output.
    """
    return await _stream_subprocess(["docker", "compose", "up", "-d", "--no-build"], root, on_line)


async def stop_containers(root: Path, on_line: Callable[[str], None] | None = None) -> int:
    """Stop the SpecFlow stack (``docker compose down``), streamed.

    The counterpart to :func:`start_containers`, used when switching away from the
    docker runtime. Returns the process exit code; non-zero surfaces through the
    streamed output.
    """
    return await _stream_subprocess(["docker", "compose", "down"], root, on_line)


def docker_cli_available() -> bool:
    """True iff the ``docker`` CLI is on PATH — a cheap preflight before trying to
    start the docker stack (distinct from :func:`containers_running`, which asks
    whether the stack is already up)."""
    return shutil.which("docker") is not None


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

    The script owns all state mutation (docker up, repo provisioning, database
    seed, mcp-config write); this only invokes and streams it. Returns the exit
    code.
    """
    argv = ["bash", str(root / _INIT_SCRIPT), *flags.to_argv()]
    return await _stream_subprocess(argv, root, on_line)
