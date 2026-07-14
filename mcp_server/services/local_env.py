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
import signal
import subprocess
import sys
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

# Bare-metal ("process") backend launch — pidfile + log live under .specflow-local
# (already the runtime-config home), port mirrors docker-compose's SPECFLOW_BACKEND_PORT.
_BACKEND_PID_FILENAME = ".specflow-local/backend.pid"
_BACKEND_LOG_FILENAME = ".specflow-local/backend.log"
_BACKEND_PORT_DEFAULT = "8000"


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
# Bare-metal ("process") backend control — BACKEND_RUNTIME=process
# ---------------------------------------------------------------------------
#
# The docker-mode boundary (the container) is gone here, so the backend runs
# directly on the host as a detached uvicorn process. The agents it launches are
# instead confined by the OS-level Bash sandbox (see the backend's
# app/agents_sandboxing/os_sandbox.py); this module only starts/stops/detects the
# process and preflights the host sandbox dependencies.


def backend_pid_path(root: Path) -> Path:
    return root / _BACKEND_PID_FILENAME


def backend_log_path(root: Path) -> Path:
    return root / _BACKEND_LOG_FILENAME


def _backend_port() -> str:
    """Host port for the bare-metal backend — mirrors docker-compose's default."""
    return os.getenv("SPECFLOW_BACKEND_PORT", _BACKEND_PORT_DEFAULT)


def _read_backend_pid(root: Path) -> int | None:
    """PID recorded by the last ``start_backend_process``; ``None`` if absent/garbage."""
    try:
        return int(backend_pid_path(root).read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """True iff a process with ``pid`` currently exists (POSIX ``kill -0``)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def backend_process_running(root: Path | None = None) -> bool:
    """True iff a previously-started bare-metal backend process is still alive."""
    root = root or Path.cwd()
    pid = _read_backend_pid(root)
    return pid is not None and _pid_alive(pid)


def build_process_backend_env(root: Path) -> dict[str, str]:
    """Environment for the bare-metal backend — the host equivalent of the
    docker-compose passthrough.

    docker-compose injects ``.env`` and then overrides a few paths with
    *container* locations (``/workspaces``, ``/root/.specflow/...``) that don't
    exist on the bare host. Here we merge the repo-root ``.env`` and substitute
    host-appropriate defaults for those paths (only when the user hasn't already
    set them), then force ``BACKEND_RUNTIME=process`` so the backend engages the
    agent OS-sandbox and its own fail-closed gate.
    """
    env = dict(os.environ)
    env.update(read_dotenv(root))  # API keys, provider, git identity, etc.
    # Host equivalents of the compose container paths (setdefault → respect any
    # value the user already exported or put in .env).
    env.setdefault("DATABASE_TYPE", "sqlite")
    env.setdefault("SQLITE_DB_PATH", str(Path.home() / ".specflow" / "db" / "specflow.db"))
    env.setdefault("WORKSPACE_BASE_PATH", str(root / "workspaces"))
    env["BACKEND_RUNTIME"] = BackendRuntime.PROCESS.value  # always forced
    return env


async def start_backend_process(
    root: Path, on_line: Callable[[str], None] | None = None
) -> int:
    """Launch the backend as a detached bare-metal ``uvicorn`` process.

    Mirrors the Dockerfile CMD on the host: ``uv run uvicorn app.main:app`` from
    ``root/backend`` bound to localhost. Detaches from the terminal via a new
    session (``start_new_session=True``) so it survives the TUI, redirecting
    output to ``.specflow-local/backend.log`` and recording the PID. Returns the
    spawned PID; readiness is confirmed separately via ``wait_backend_ready``.
    """
    backend_dir = root / "backend"
    log_path = backend_log_path(root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure the host workspace dir exists so the backend can allocate into it.
    env = build_process_backend_env(root)
    Path(env["WORKSPACE_BASE_PATH"]).mkdir(parents=True, exist_ok=True)

    argv = ["uv", "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", _backend_port()]
    log_file = open(log_path, "ab")  # child dups the fd; parent handle closes on GC
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(backend_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    finally:
        log_file.close()
    backend_pid_path(root).write_text(str(proc.pid))
    if on_line is not None:
        on_line(f"backend started detached (pid {proc.pid}); logs → {log_path}\n")
    return proc.pid


def stop_backend_process(root: Path | None = None) -> bool:
    """SIGTERM the detached backend's process group and clear the pidfile.

    Returns ``True`` if a live process was signalled, ``False`` if none was
    recorded/alive. Signals the whole session group (the process is a group
    leader from ``start_new_session``) so uvicorn workers are torn down too.
    """
    root = root or Path.cwd()
    pid = _read_backend_pid(root)
    signalled = False
    if pid is not None and _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            signalled = True
        except (ProcessLookupError, PermissionError, OSError):
            signalled = False
    backend_pid_path(root).unlink(missing_ok=True)
    return signalled


async def run_backend_process_cli(root: Path) -> int:
    """Orchestrate ``make run-process``: fail-closed sandbox preflight → start
    detached → wait for ready. Returns a shell exit code (0 = ready).

    Shares the exact launch path (``start_backend_process``) and health gate the
    TUI uses, so the Makefile convenience target can never drift from it.
    """
    reason = agent_sandbox_unavailable_reason()
    if reason is not None:
        print(f"❌ Cannot start in process mode — {reason}")
        return 1
    await start_backend_process(root, on_line=lambda line: print(line, end=""))
    if await wait_backend_ready(f"http://127.0.0.1:{_backend_port()}"):
        print("✅ Backend ready (detached). Stop with `make stop-process`.")
        return 0
    print("❌ Backend didn't become ready — see .specflow-local/backend.log")
    return 1


def agent_sandbox_unavailable_reason() -> str | None:
    """Host-side preflight for the agent OS sandbox; ``None`` if it can run here.

    Deliberately mirrors the backend's authoritative
    ``os_sandbox.check_agent_sandbox_available`` (the packages can't share code);
    this is the fast local gate so the TUI refuses before even starting the
    backend. macOS → Seatbelt (``sandbox-exec``); Linux → bubblewrap + socat.
    """
    if sys.platform == "darwin":
        if shutil.which("sandbox-exec") is None:
            return (
                "macOS sandbox tool `sandbox-exec` was not found on PATH. It ships with "
                "macOS — ensure /usr/bin is on PATH."
            )
        return None
    if sys.platform.startswith("linux"):
        missing = [dep for dep in ("bwrap", "socat") if shutil.which(dep) is None]
        if missing:
            return (
                f"Linux sandbox dependencies missing: {', '.join(missing)}. Install with "
                "`sudo apt-get install bubblewrap socat` (Debian/Ubuntu) or "
                "`sudo dnf install bubblewrap socat` (Fedora)."
            )
        return None
    return (
        f"The agent OS sandbox is not supported on this platform ({sys.platform}). Use "
        "BACKEND_RUNTIME=docker, or run on macOS or Linux."
    )


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
