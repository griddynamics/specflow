"""Live backend e2e: graceful shutdown + boot recovery.

Drives the backend HTTP API directly (no MCP): uploads minimal specs, starts a generation,
restarts the backend container mid-run (SIGTERM → boot), and asserts the interrupted
run is auto-recovered (`completed` with `retry_count >= 1`). The container name comes from
`BACKEND_CONTAINER` (the Makefile passes the isolated test stack's `specflow-test-backend`).

Requires a live local stack and is gated off the normal suite — run via
`make shutdown-recovery-e2e-tests` (sets RUN_SHUTDOWN_RECOVERY_E2E=1, brings up the stack in
SKIP mode, and passes credentials). Stdlib only; never imports app modules.
"""
import gzip
import io
import json
import os
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_SHUTDOWN_RECOVERY_E2E"),
    reason="requires a live local stack (run via `make shutdown-recovery-e2e-tests`)",
)

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
BACKEND_CONTAINER = os.getenv("BACKEND_CONTAINER", "specflow-backend")

# Populated by the test at runtime via _resolve_credentials().
_AUTH = {"key": "", "email": ""}

RUNNING_TIMEOUT_S = 90.0
TERMINAL_TIMEOUT_S = 180.0
HEALTH_TIMEOUT_S = 90.0

_ANALYSIS = """\
# Specification Completeness Analysis

## Critical Issues
None.

## Part F: Integration & Deployment Readiness

**Integration Readiness:** LOCAL_ONLY

**Rationale:** No deployment automation in scope for this E2E run.
"""

_PLAN = """\
# Implementation Plan

## Architectural Decisions - Locked Values
| Dimension | Value |
|-----------|-------|
| A1. Data Persistence | None (in-memory) |

## Phase 1: Bootstrap
**Agent MCPs**: none

- Initialise project skeleton.
- Wire up the no-op entrypoint.
"""


def _resolve_credentials() -> tuple[str, str]:
    """(SPECFLOW_API_KEY, USER_EMAIL): env vars if set, else fetched from the Firestore
    emulator via scripts/get-api-key.py (same as the MCP e2e harness). The key is normally
    not in .env — it's created in the emulator by `make e2e-setup`."""
    key = os.getenv("SPECFLOW_API_KEY", "").strip()
    email = os.getenv("USER_EMAIL", "").strip()
    if key and email:
        return key, email

    result = subprocess.run(
        ["uv", "run", "python", "../scripts/get-api-key.py"],
        capture_output=True, text=True,
        env={
            **os.environ,
            "FIRESTORE_EMULATOR_HOST": os.getenv("FIRESTORE_EMULATOR_HOST", "localhost:8080"),
            "DATABASE_TYPE": "emulator",
        },
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if not key and "API Key:" in line:
                key = line.split("API Key:")[1].strip()
            if not email and "User:" in line:
                email = line.split("User:")[1].strip()
    return key, email


def _headers(extra: dict | None = None) -> dict:
    h = {"X-API-Key": _AUTH["key"], "X-User-Email": _AUTH["email"]}
    h.update(extra or {})
    return h


def _build_archive() -> bytes:
    """tar.gz with the minimal contract files (specs + analysis + plan)."""
    files = {
        "specs/spec.md": "# Tiny App\nA no-op service for the shutdown-recovery e2e.\n",
        "docs/analysis/specification_completeness.md": _ANALYSIS,
        "docs/planning/IMPLEMENTATION_PLAN.md": _PLAN,
    }
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def _post_multipart(path: str, fields: dict, file_field: str, filename: str, file_bytes: bytes) -> dict:
    boundary = "----specflowE2EBoundary7MA4YWxkTrZu0gW"
    parts = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: application/gzip\r\n\r\n".encode()
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        BACKEND_URL + path, data=body, method="POST",
        headers=_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _post_form(path: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        BACKEND_URL + path, data=body, method="POST",
        headers=_headers({"Content-Type": "application/x-www-form-urlencoded"}),
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _get_status(generation_id: str) -> dict:
    req = urllib.request.Request(
        f"{BACKEND_URL}/api/v1/generation-sessions/{generation_id}/status",
        method="GET", headers=_headers(),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _poll_status(generation_id: str, predicate, timeout_s: float, interval_s: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        last = _get_status(generation_id)
        if predicate(last):
            return last
        time.sleep(interval_s)
    raise TimeoutError(f"poll timed out after {timeout_s}s. Last status: {last}")


def _restart_backend() -> None:
    result = subprocess.run(["docker", "restart", BACKEND_CONTAINER], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"`docker restart {BACKEND_CONTAINER}` failed: {result.stderr or result.stdout}"
    )


def _wait_healthy() -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    last = "no attempt"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{BACKEND_URL}/health", timeout=5) as resp:
                if resp.status == 200:
                    return
                last = f"status {resp.status}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(2)
    raise TimeoutError(f"backend not healthy within {HEALTH_TIMEOUT_S}s ({last})")


def test_shutdown_recovery_live():
    _AUTH["key"], _AUTH["email"] = _resolve_credentials()
    if not _AUTH["key"] or not _AUTH["email"]:
        pytest.skip("could not resolve SPECFLOW_API_KEY / USER_EMAIL (is the emulator up?)")

    # 1. Upload specs + start a generation.
    sync = _post_multipart(
        "/api/v1/workspace/sync",
        fields={
            "params": json.dumps({"spec_path": "specs", "src_dir": "src", "workflow_type": "generation_run"}),
            "sync_to_all": "false",
        },
        file_field="archive", filename="specs.tar.gz", file_bytes=_build_archive(),
    )
    generation_id = sync["generation_id"]

    _post_form("/api/v1/generation-sessions/run", {
        "spec_path": "specs", "outputs_dir": "docs", "src_dir": "src",
        "generation_id": generation_id,
    })

    # 2. Wait until it's RUNNING (its in-process task is live on the backend pod).
    running = _poll_status(
        generation_id,
        lambda d: (d.get("status") or "").lower() in ("running", "completed", "failed"),
        timeout_s=RUNNING_TIMEOUT_S, interval_s=1.0,
    )
    assert (running.get("status") or "").lower() == "running", (
        f"generation reached {running.get('status')!r} before it could be interrupted "
        "(finished too fast — re-run)."
    )

    # 3. Restart the backend mid-run: SIGTERM (Half A) → boot (Half B).
    _restart_backend()
    _wait_healthy()

    # Give the boot-recovery asyncio task time to run inside the container's event loop
    # before the first status poll. In SKIP mode the generation completes in milliseconds,
    # so without this sleep the first poll may see the pre-recovery FAILED state before
    # `reset_for_retry` fires (timing race). 10 s is negligible against the 180 s timeout.
    time.sleep(10)

    # 4. The interrupted run must be auto-recovered.
    final = _poll_status(
        generation_id,
        lambda d: (d.get("status") or "").lower() in ("completed", "failed"),
        timeout_s=TERMINAL_TIMEOUT_S, interval_s=5.0,
    )
    assert (final.get("status") or "").lower() == "completed", f"did not recover: {final}"
    assert final.get("retry_count", 0) >= 1, (
        f"retry_count is 0 — boot recovery did not re-fire the interrupted session: {final}"
    )
