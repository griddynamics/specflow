"""Polling loop, assertion helpers, and API key resolution for E2E tests."""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Awaitable, Callable

from mcp import ClientSession

from tests.e2e.mcp_client import REPO_ROOT, call_tool

PLAN_MARKER = "# [E2E-TEST-MARKER]"


def prepend_plan_marker(plan_file: Path) -> None:
    """Prepend PLAN_MARKER to plan_file to verify the server preserves user edits through generation."""
    original = plan_file.read_text(encoding="utf-8")
    plan_file.write_text(PLAN_MARKER + "\n" + original, encoding="utf-8")


def _print_exc_tree(exc: BaseException, indent: int = 0) -> None:
    prefix = "   " * indent
    if isinstance(exc, BaseExceptionGroup):
        print(f"{prefix}{type(exc).__name__}: {exc.message} ({len(exc.exceptions)} sub-exception(s))", file=sys.stderr)
        for sub in exc.exceptions:
            _print_exc_tree(sub, indent + 1)
    else:
        print(f"{prefix}{type(exc).__name__}: {exc}", file=sys.stderr)
        tb_lines = traceback.format_tb(exc.__traceback__)
        for line in tb_lines:
            for ln in line.splitlines():
                print(f"{prefix}  {ln}", file=sys.stderr)


def run_scenario(prefix: str, coro_fn: Callable[[int, Path], Awaitable[None]]) -> None:
    """Run a scenario coroutine in a fresh temp dir, printing diagnostics and exiting non-zero on failure."""
    workspace_count = int(os.getenv("WORKSPACE_COUNT", "3"))
    tmp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    print(f"Temp dir: {tmp_dir}")
    print(f"Workspace count: {workspace_count}")
    print()
    try:
        asyncio.run(coro_fn(workspace_count, tmp_dir))
    except BaseException as exc:
        print(f"\n❌ TEST FAILED: {exc}", file=sys.stderr)
        _print_exc_tree(exc)
        print(f"\n   Temp dir preserved for inspection: {tmp_dir}", file=sys.stderr)
        sys.exit(1)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print("Temp dir cleaned up.")


async def poll_until(
    session: ClientSession,
    generation_id: str,
    condition_fn: Callable[[dict], bool],
    timeout_s: float,
    interval_s: float = 5.0,
    label: str = "",
) -> dict:
    """Poll check_status until condition_fn returns True or timeout is reached."""
    deadline = time.monotonic() + timeout_s
    last_data: dict = {}
    while time.monotonic() < deadline:
        data = await call_tool(session, "check_status", {"generation_id": generation_id})
        last_data = data
        if condition_fn(data):
            return data
        status = data.get("status", "?")
        checkpoint = data.get("checkpoint", "?")
        tag = f" [{label}]" if label else ""
        print(f"  polling{tag}: status={status} checkpoint={checkpoint}")
        await asyncio.sleep(interval_s)
    raise TimeoutError(
        f"poll_until timed out after {timeout_s}s"
        + (f" [{label}]" if label else "")
        + f". Last response: {last_data}"
    )


def assert_no_error(data: dict, step: str) -> None:
    if "error" in data:
        raise AssertionError(
            f"Step {step!r}: unexpected error key in response.\n"
            f"  error: {data['error']}\n"
            f"  full: {data}"
        )


def assert_files(base_dir: Path, required_globs: list[str]) -> None:
    """Assert that each glob pattern matches at least one file under base_dir."""
    missing = []
    for pattern in required_globs:
        if not list(base_dir.glob(pattern)):
            missing.append(pattern)
    if missing:
        existing = sorted(
            str(p.relative_to(base_dir))
            for p in base_dir.rglob("*")
            if p.is_file()
        )
        raise AssertionError(
            f"Missing required files in {base_dir}:\n"
            + "".join(f"  {m}\n" for m in missing)
            + f"Actual files ({len(existing)} total):\n"
            + "".join(f"  {f}\n" for f in existing[:40])
        )


def resolve_api_credentials() -> tuple[str, str]:
    """
    Return (SPECFLOW_API_KEY, USER_EMAIL): from env vars if both are set, otherwise
    fetch from the active database backend (sqlite by default) by running
    scripts/get-api-key.py.
    """
    key = os.environ.get("SPECFLOW_API_KEY", "").strip()
    email = os.environ.get("USER_EMAIL", "").strip()
    if key and email:
        return key, email

    result = subprocess.run(
        ["uv", "run", "python", "../scripts/get-api-key.py"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT / "backend"),
        env={
            **os.environ,
            "DATABASE_TYPE": os.getenv("DATABASE_TYPE", "sqlite"),
            "SQLITE_DB_PATH": os.getenv(
                "SQLITE_DB_PATH", str(Path.home() / ".specflow" / "specflow.db")
            ),
            "FIRESTORE_EMULATOR_HOST": os.getenv("FIRESTORE_EMULATOR_HOST", "localhost:8080"),
            "GCP_PROJECT_ID": os.getenv("GCP_PROJECT_ID", "local-dev"),
            "FIRESTORE_DATABASE_NAME": os.getenv("FIRESTORE_DATABASE_NAME", "specflow"),
        },
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"get-api-key.py exited with code {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    found_key = key
    found_email = email
    for line in result.stdout.splitlines():
        if not found_key and "API Key:" in line:
            parts = line.split("API Key:")
            if len(parts) == 2:
                found_key = parts[1].strip()
        if not found_email and "User:" in line:
            parts = line.split("User:")
            if len(parts) == 2:
                found_email = parts[1].strip()

    if found_key and found_email:
        return found_key, found_email

    raise RuntimeError(
        "SPECFLOW_API_KEY/USER_EMAIL not set and could not be fetched from the database.\n"
        f"get-api-key.py stdout:\n{result.stdout}\n"
        f"get-api-key.py stderr:\n{result.stderr}"
    )
