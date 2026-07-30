"""Bare-metal ("process") backend control — ``BACKEND_RUNTIME=process``.

In docker mode the container is the isolation boundary; in process mode the
backend runs directly on the host as a detached uvicorn process, and its agents
are confined instead by the OS-level Bash sandbox (see the backend's
``app/agents_sandboxing/os_sandbox.py``). This module owns only the host-side
process lifecycle: start / stop / detect the detached backend and preflight the
sandbox dependencies.

Runtime *selection* (the ``BackendRuntime`` enum and the saved runtime choice)
and the shared filesystem / health helpers live in ``local_env``; this module is
strictly the process-mode control surface split out of it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from services.local_env import (
    BackendRuntime,
    read_dotenv,
    specflow_home_dir,
    wait_backend_ready,
)

# The bare-metal backend is a machine-wide singleton (one uvicorn on one port,
# backed by the shared ~/.specflow db), so its PID lives under ~/.specflow — NOT
# per-checkout — so `stop` / `switch runtime` from any clone find the one running
# backend instead of spawning a duplicate on the same port.
_BACKEND_PID_BASENAME = "backend.pid"

# The launch log stays per-project (under the repo's .specflow-local, beside
# mcp-config.json) so each checkout's launch output is inspectable next to it.
_BACKEND_LOG_FILENAME = ".specflow-local/backend.log"
_BACKEND_PORT_DEFAULT = "8000"


def backend_pid_path(home: Path | None = None) -> Path:
    return specflow_home_dir(home) / _BACKEND_PID_BASENAME


def backend_log_path(root: Path) -> Path:
    return root / _BACKEND_LOG_FILENAME


def _backend_port() -> str:
    """Host port for the bare-metal backend — mirrors docker-compose's default."""
    return os.getenv("SPECFLOW_BACKEND_PORT", _BACKEND_PORT_DEFAULT)


def _read_backend_pid(home: Path | None = None) -> int | None:
    """PID recorded by the last ``start_backend_process``; ``None`` if absent/garbage."""
    try:
        return int(backend_pid_path(home).read_text().strip())
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


def backend_process_running(home: Path | None = None) -> bool:
    """True iff a previously-started bare-metal backend process is still alive."""
    pid = _read_backend_pid(home)
    return pid is not None and _pid_alive(pid)


def build_process_backend_env(root: Path) -> dict[str, str]:
    """Environment for the bare-metal backend — the host equivalent of the
    docker-compose passthrough.

    docker-compose bind-mounts host dirs onto fixed *container* mount points and
    passes those container paths to the backend (``/workspaces``, ``/agent_logs``,
    ``/root/.specflow/...``). Those paths don't exist on the bare host and ``/`` is
    read-only, so in process mode we **override** them with the host equivalents of
    compose's host-side bind mounts — never ``setdefault``. A container path shipped
    in ``.env`` (the quickstart sets ``WORKSPACE_BASE_PATH=/workspaces``) would
    otherwise shadow the host path and the backend would crash creating a dir under
    ``/`` (Errno 30, read-only file system). Genuine user choices that are *not*
    mount targets (e.g. ``DATABASE_TYPE=firestore``) are still respected.
    ``BACKEND_RUNTIME`` is forced to ``process`` so the backend engages the agent
    OS-sandbox and its own fail-closed gate.
    """
    env = dict(os.environ)
    env.update(read_dotenv(root))  # API keys, provider, git identity, etc.
    env.setdefault("DATABASE_TYPE", "sqlite")  # a real user choice, not a mount target
    # Container mount points → host equivalents of compose's bind-mount defaults
    # (./workspaces, ./agent_logs, ${HOME}/.specflow). Overridden unconditionally:
    # the container values are never valid on the bare host.
    env["WORKSPACE_BASE_PATH"] = str(root / "workspaces")
    env["AGENT_LOGS_BASE_PATH"] = str(root / "agent_logs")
    env["SQLITE_DB_PATH"] = str(specflow_home_dir() / "db" / "specflow.db")
    env["BACKEND_RUNTIME"] = BackendRuntime.PROCESS.value  # always forced
    return env


async def start_backend_process(
    root: Path,
    on_line: Callable[[str], None] | None = None,
    home: Path | None = None,
) -> int:
    """Launch the backend as a detached bare-metal ``uvicorn`` process.

    Mirrors the Dockerfile CMD on the host: ``uv run uvicorn app.main:app`` from
    ``root/backend`` bound to localhost. Detaches from the terminal via a new
    session (``start_new_session=True``) so it survives the TUI, redirecting
    output to the per-project ``.specflow-local/backend.log`` and recording the
    PID under the machine-wide ``~/.specflow/backend.pid``. Returns the spawned
    PID; readiness is confirmed separately via ``wait_backend_ready``.
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
    pid_path = backend_pid_path(home)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid))
    if on_line is not None:
        on_line(f"backend started detached (pid {proc.pid}); logs → {log_path}\n")
    return proc.pid


def stop_backend_process(home: Path | None = None) -> bool:
    """SIGTERM the detached backend's process group and clear the pidfile.

    Returns ``True`` if a live process was signalled, ``False`` if none was
    recorded/alive. Signals the whole session group (the process is a group
    leader from ``start_new_session``) so uvicorn workers are torn down too.
    """
    pid = _read_backend_pid(home)
    signalled = False
    if pid is not None and _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            signalled = True
        except (ProcessLookupError, PermissionError, OSError):
            signalled = False
    backend_pid_path(home).unlink(missing_ok=True)
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
