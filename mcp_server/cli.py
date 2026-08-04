"""SpecFlow CLI — local-only, no-auth, localhost-default.

A thin client over existing mcp_server service code. Intended for users who
cannot use the MCP server (Cursor / Claude Code). All backend interaction
reuses SpecFlowBackendService; all path resolution reuses session.py.

Entry point: specflow <command> [options]
Also runnable as: python -m cli

Commands:
  run-generation      Upload specs and start code generation
  check-status        Poll the status of the current generation
  retry-generation    Retry a failed generation
  download-outputs    Download and extract completed generation outputs
  clear-workspace     Free a CLEANING workspace set early
  sessions            List active generation sessions
  refine              Spec refinement (see services/refine_commands.py)
  plugin              Install the Claude Code plugin

Local-only invariant: refuses to connect to non-localhost URLs unless --force
is passed.  No API key is ever sent in local mode.
"""

import argparse
import asyncio
import datetime
import inspect
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from services import local_env, refine_commands
from tui import mcp_clients

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

# Single source of truth lives in local_env; re-exported here for the existing
# importers (e.g. tui.config) that read it from cli.
_MCP_CONFIG_FILENAME = local_env.MCP_CONFIG_FILENAME
_DEFAULT_BACKEND_URL = "http://127.0.0.1:8000"
_LOCALHOST_HOSTS = {"localhost", "127.0.0.1"}


def _load_mcp_config(root: Path) -> dict[str, Any]:
    """Load .specflow-local/mcp-config.json from the project root, if present."""
    config_path = root / _MCP_CONFIG_FILENAME
    try:
        if config_path.exists():
            data = json.loads(config_path.read_text())
            env_block = data.get("mcpServers", {}).get("specflow", {}).get("env", {})
            return env_block if isinstance(env_block, dict) else {}
    except Exception as exc:
        logger.debug("Could not read mcp-config.json: %s", exc)
    return {}


def resolve_backend_config(
    backend_url_flag: str | None,
    user_email_flag: str | None,
    workspace_count_flag: int | None,
    root: Path,
) -> tuple[str, str | None, int | None]:
    """Resolve backend URL, user email, and workspace count.

    Priority: CLI flag → env var → mcp-config.json → default.

    Returns (backend_url, user_email, workspace_count).
    """
    mcp_env = _load_mcp_config(root)

    backend_url = (
        backend_url_flag
        or os.getenv("BACKEND_URL")
        or mcp_env.get("BACKEND_URL")
        or _DEFAULT_BACKEND_URL
    )
    user_email = user_email_flag or os.getenv("USER_EMAIL") or mcp_env.get("USER_EMAIL") or None

    workspace_count: int | None = None
    if workspace_count_flag is not None:
        workspace_count = workspace_count_flag
    else:
        wc_env = os.getenv("WORKSPACE_COUNT") or mcp_env.get("WORKSPACE_COUNT")
        if wc_env is not None:
            try:
                parsed = int(wc_env)
                if parsed in (1, 2, 3):
                    workspace_count = parsed
            except (ValueError, TypeError):
                pass

    return backend_url, user_email, workspace_count


def resolve_backend_runtime(
    runtime_flag: str | None = None, home: Path | None = None
) -> local_env.BackendRuntime:
    """Resolve how the backend is launched: docker (default) or process.

    Precedence: CLI flag → ``BACKEND_RUNTIME`` env var → the launcher's saved
    choice (``~/.specflow/backend-runtime``, written by the TUI first-run
    chooser) → default ``docker``. Unknown values fall back to ``docker``.

    The saved choice is machine-wide (the backend is a singleton), so it is read
    from ``~/.specflow`` rather than any per-checkout dir. mcp-config.json is
    deliberately NOT consulted: how the backend is launched is a local-launcher
    concern, whereas mcp-config configures the MCP server, which merely calls
    ``backend_url`` and is indifferent to the backend's runtime.
    """
    raw = runtime_flag or os.getenv("BACKEND_RUNTIME")
    if raw:
        return local_env.BackendRuntime.parse(raw)
    saved = local_env.read_saved_runtime(home)
    return saved if saved is not None else local_env.BackendRuntime.DOCKER


def backend_runtime_is_configured(home: Path | None = None) -> bool:
    """True when the runtime is explicitly pinned (``BACKEND_RUNTIME`` env var or a
    saved choice), so the TUI's first-run chooser should be skipped."""
    return bool(os.getenv("BACKEND_RUNTIME")) or local_env.read_saved_runtime(home) is not None


def _is_localhost(url: str) -> bool:
    """Return True if the URL host is localhost or 127.0.0.1."""
    try:
        host = urlparse(url).hostname or ""
        return host.lower() in _LOCALHOST_HOSTS
    except Exception:
        return False


def check_localhost_guard(backend_url: str, force: bool) -> None:
    """Print a warning and exit non-zero if backend_url is not localhost and --force not set."""
    if force or _is_localhost(backend_url):
        return
    print(
        f"ERROR: The CLI is local-only (no API key is sent).\n"
        f"  Resolved BACKEND_URL: {backend_url}\n"
        f"  This URL is not localhost — it will NOT authenticate against a remote backend.\n"
        f"  Use the MCP server for remote deployments.\n"
        f"  Pass --force to bypass this check.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Environment setup — must be called before importing service modules
# ---------------------------------------------------------------------------


def _configure_env(backend_url: str, user_email: str | None) -> None:
    """Push resolved config into the environment so service singletons pick it up."""
    os.environ["BACKEND_URL"] = backend_url
    if user_email:
        os.environ["USER_EMAIL"] = user_email
    else:
        os.environ.pop("USER_EMAIL", None)
    # Never set SPECFLOW_API_KEY in local mode — service omits X-API-Key when unset.
    os.environ.pop("SPECFLOW_API_KEY", None)


# ---------------------------------------------------------------------------
# Root resolution
# ---------------------------------------------------------------------------


def resolve_root(root_path_arg: str | None) -> Path:
    """Return absolute project root. Defaults to cwd; --root-path overrides.

    Implementation lives in ``local_env`` so the ``refine`` group can resolve the
    same flag from ``services/`` without importing this module back; kept here
    under its original name for the existing call sites.
    """
    return local_env.resolve_project_root(root_path_arg)


# ---------------------------------------------------------------------------
# Command implementations (async)
# ---------------------------------------------------------------------------


async def cmd_run_generation(args: argparse.Namespace) -> int:
    """run-generation: upload specs and start generation."""
    from services.session import set_project_root, resolve_generation_id, write_session
    from services.generation_orchestrator import GenerationOrchestrator
    from services.run_generation_precheck import precheck as run_precheck
    from services.tool_helpers import check_status_safe, is_generation_in_progress
    from services.cli_service import fetch_pool_status, render_capacity_message

    root = resolve_root(args.root_path)
    print(f"Using project root: {root}")
    set_project_root(root)

    spec_dir = root / args.spec_dir
    src_dir = root / args.src_dir
    outputs_dir = args.outputs_dir

    # Pre-run notice (FR-8, LV4.2)
    print(
        f"\nNote: outputs land in {root / outputs_dir}/{{generation_id}}/... and are\n"
        f"archived in the artifact store — nothing is lost on workspace recycle.\n"
        f"Retrieve them any time with: specflow download-outputs\n"
    )

    rejection = run_precheck(root, args.spec_dir, outputs_dir)
    if rejection is not None:
        print(f"ERROR: {rejection.to_dict().get('error', rejection.code.value)}", file=sys.stderr)
        return 1

    generation_id = resolve_generation_id(None, root)
    if generation_id:
        status_data = await check_status_safe(generation_id)
        if is_generation_in_progress(status_data):
            print(
                "ERROR: A generation is already running. "
                "Wait for the email notification before starting another one.",
                file=sys.stderr,
            )
            return 1

    workspace_count = args.workspace_count  # may be None

    try:
        response_data = await GenerationOrchestrator.run_generation(
            spec_dir=spec_dir,
            src_dir=src_dir,
            outputs_dir=outputs_dir,
            generation_id=generation_id,
            workspace_count=workspace_count,
        )
    except Exception as exc:
        error_msg = str(exc)
        # Capacity UX (LV4.1): if allocation failed, show cleaning sets
        if "no" in error_msg.lower() and "workspace" in error_msg.lower():
            try:
                pool = await fetch_pool_status()
                cleaning = pool.get("cleaning_sets") or []
                if cleaning:
                    print(render_capacity_message(cleaning))
                    return 1
            except Exception:
                pass
        print(f"ERROR: {error_msg}", file=sys.stderr)
        return 1

    new_id = response_data.get("generation_id")
    if new_id:
        write_session(new_id, root)

    print(json.dumps(response_data, indent=2))
    print(
        "\nFiles uploaded and generation started (usually 2-8 hours).\n"
        "Run `specflow check-status` to poll progress, or `specflow sessions --watch` for\n"
        "continuous monitoring with a desktop notification on completion.\n"
        "(Email/Slack notifications fire only if configured in .env.)"
    )
    return 0


async def cmd_check_status(args: argparse.Namespace) -> int:
    """check-status: poll status of the current generation."""
    from services.session import set_project_root, resolve_generation_id
    from services.specflow_backend import call_backend_endpoint

    root = resolve_root(args.root_path)
    print(f"Using project root: {root}")
    set_project_root(root)

    generation_id = resolve_generation_id(None, root)
    if not generation_id:
        print("No active generation in this project. Run `specflow run-generation` to start one.")
        return 0

    response_text = await call_backend_endpoint(
        endpoint=f"/api/v1/generation-sessions/{generation_id}/status",
        method="GET",
        timeout_seconds=30,
    )
    data = json.loads(response_text)
    print(json.dumps(data, indent=2))
    return 0


async def cmd_retry_generation(args: argparse.Namespace) -> int:
    """retry-generation: retry a failed generation."""
    from services.session import set_project_root, resolve_generation_id
    from services.retry import retry_generation_core

    root = resolve_root(args.root_path)
    print(f"Using project root: {root}")
    set_project_root(root)

    generation_id = resolve_generation_id(args.generation_id, root)
    if not generation_id:
        print("No previous generation found. Run `specflow run-generation` to start one.")
        return 0

    try:
        backend_data = await retry_generation_core(generation_id)
    except Exception as exc:  # noqa: BLE001 - backend validates state; surface its reason
        print(f"ERROR: Couldn't retry: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(backend_data, indent=2))
    print("\nRetry queued. Generation will resume from the last checkpoint on the same workspaces.")
    return 0


async def cmd_download_outputs(args: argparse.Namespace) -> int:
    """download-outputs: download and extract completed generation outputs."""
    from services.session import set_project_root, resolve_generation_id
    from services.cli_service import download_and_extract_outputs

    root = resolve_root(args.root_path)
    print(f"Using project root: {root}")
    set_project_root(root)

    generation_id = resolve_generation_id(args.generation_id, root)
    if not generation_id:
        print(
            "ERROR: No generation_id found. Pass --generation-id or run run-generation first.",
            file=sys.stderr,
        )
        return 1

    outputs_dir = args.outputs_dir
    try:
        result = await download_and_extract_outputs(generation_id, outputs_dir)
    except Exception as exc:
        print(f"ERROR: Couldn't download outputs: {exc}", file=sys.stderr)
        return 1

    if result.get("status") == "success":
        dest = result.get("outputs_dir", "")
        count = result.get("files_extracted", 0)
        print(f"Downloaded {count} files to: {dest}")
    else:
        print(json.dumps(result, indent=2))
    return 0


def _render_sessions_table(sessions: list[dict]) -> None:
    """Print a sessions table to stdout."""
    if not sessions:
        print("No generation sessions found.")
        return
    header = f"{'GENERATION ID':<40}  {'STATUS':<16}  {'CREATED':<20}  {'CHECKPOINT'}"
    print(header)
    print("-" * len(header))
    for s in sessions:
        created = s.get("created_at", "")[:16].replace("T", " ") if s.get("created_at") else ""
        print(f"{s['generation_id']:<40}  {s['status']:<16}  {created:<20}  {s.get('checkpoint', '')}")


async def cmd_sessions(args: argparse.Namespace) -> int:
    """sessions: list active generation sessions (no new backend route)."""
    from services.cli_service import fetch_sessions
    from tui.poller import MilestoneTracker, fire_milestones

    watch = getattr(args, "watch", False)
    interval = getattr(args, "interval", 15)

    if not watch:
        sessions = await fetch_sessions()
        _render_sessions_table(sessions)
        return 0

    # --watch: poll, refresh screen, notify on run/checkpoint/workspace progress.
    trackers: dict[str, MilestoneTracker] = {}
    print(f"Watching sessions (every {interval}s) — Ctrl+C to stop\n")
    while True:
        try:
            sessions = await fetch_sessions()
        except Exception as exc:
            print(f"Error fetching sessions: {exc}")
        else:
            # Clear terminal and reprint
            print("\033[2J\033[H", end="", flush=True)
            now = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"SpecFlow sessions — every {interval}s — {now}   (Ctrl+C to stop)\n")
            _render_sessions_table(sessions)

            for s in sessions:
                gid = s["generation_id"]
                tracker = trackers.setdefault(gid, MilestoneTracker(gid))
                milestones = tracker.process(s)
                fire_milestones(milestones)
                for milestone in milestones:
                    print(f"\n[Desktop notification sent: {milestone.message}]")

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
    return 0


async def cmd_clear_workspace(args: argparse.Namespace) -> int:
    """clear-workspace --set N: clear all 3 members of a CLEANING workspace set."""
    from services.cli_service import clear_workspace_set

    set_number = args.set
    if not args.yes:
        answer = input(f"Clear all 3 workspaces in set {set_number}? This cannot be undone. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 0

    results = await clear_workspace_set(set_number)
    all_ok = True
    for r in results:
        status = "OK" if r["success"] else "FAIL"
        print(f"  {r['workspace_id']}: {status} — {r['message']}")
        if not r["success"]:
            all_ok = False

    if all_ok:
        print(f"\nSet {set_number} cleared. Workspaces are available for the next generation.")
    else:
        print("\nSome workspaces failed to clear. See errors above.", file=sys.stderr)
        return 1
    return 0


async def cmd_tui(args: argparse.Namespace) -> int:
    """tui: launch the interactive terminal UI."""
    try:
        from tui.app import run_tui
    except ImportError as exc:
        # `textual` is the only TUI runtime dependency (a base dependency). If a
        # different module failed to import, that's a real bug in the TUI stack
        # — surface it instead of the misleading "not installed" hint below.
        missing = exc.name or ""
        if missing != "textual" and not missing.startswith("textual."):
            raise
        # `textual` is a base dependency, so it should always be present. If it
        # isn't, the install is incomplete — re-install the uv tool. This is NOT
        # a project-venv `pip install`, which would not touch the tool
        # environment this command actually runs from.
        print(
            "The TUI dependency `textual` is missing — this install is incomplete.\n"
            "Re-install specflow from the repo root:\n"
            "  uv tool install --reinstall --editable ./mcp_server\n"
            "then run `specflow tui` again.",
            file=sys.stderr,
        )
        return 1
    return await run_tui(args)


async def cmd_init(args: argparse.Namespace) -> int:
    """init: one-shot local bootstrap — wraps specflow-init.sh end to end."""
    start = Path(args.root_path).expanduser().resolve() if args.root_path else None
    # Resolve from cwd, else from this install's own checkout (editable installs
    # run from the clone), so `specflow init` is reachable from any folder.
    root = local_env.resolve_repo_root(start)
    if root is None:
        print(
            "ERROR: Couldn't locate a SpecFlow checkout.\n"
            f"  No {' + '.join(local_env.SENTINEL_FILES)} found walking up from "
            f"{(start or Path.cwd())}, and this CLI isn't installed editable from a "
            "checkout.\n"
            "  The local bootstrap needs the repo (docker-compose.yml, the backend "
            "Dockerfile, and scripts/) — run from your clone or `uv tool install "
            "--editable ./mcp_server`.",
            file=sys.stderr,
        )
        return 1

    if not local_env.env_exists(root):
        print(
            f"ERROR: No .env found at {local_env.env_file_path(root)}.\n"
            f"  Copy {local_env.env_example_path(root)} → .env and fill the required keys\n"
            "  (GITHUB_TOKEN, P10Y_API_KEY, and one of OPENROUTER_API_KEY /\n"
            "  ANTHROPIC_API_KEY), then run `specflow init` again.",
            file=sys.stderr,
        )
        return 1

    flags = local_env.InitFlags(
        max_parallel_runs=args.max_parallel_runs,
        skip_build=args.skip_build,
        reset_local_db=args.reset_local_db,
        provide_own_repos=args.provide_own_repos,
        dry_run=args.dry_run,
    )
    rc = await local_env.run_init(root, flags, on_line=lambda s: print(s, end=""))
    if rc == 0 and not args.dry_run:
        print(mcp_clients.render_cli_hint(local_env.mcp_config_path(root)))
    return rc


# ---------------------------------------------------------------------------
# Plugin installation
# ---------------------------------------------------------------------------

# The plugin ships through the marketplace, not through this package. The wheel
# carries the oracles; the marketplace carries the skills that call them. So
# install means "point the tool at the published marketplace" — never copying
# files out of this install, which would fork the skills from the ones the
# marketplace serves and let the two drift.
_PLUGIN_MARKETPLACE_REPO = "griddynamics/specflow"
# Name declared by .claude-plugin/marketplace.json, not the repo name.
_PLUGIN_MARKETPLACE_NAME = "specflow-marketplace"
_PLUGIN_NAME = "specflow"
_DEFAULT_PLUGIN_TARGET = "claude"
_PLUGIN_TARGETS = {_DEFAULT_PLUGIN_TARGET}


def _plugin_install_steps(target: str) -> list[list[str]]:
    """The commands that install the published plugin into ``target``."""
    if target != "claude":  # pragma: no cover - argparse restricts the choices
        raise ValueError(f"Unsupported plugin target: {target!r}")
    return [
        [target, "plugin", "marketplace", "add", _PLUGIN_MARKETPLACE_REPO],
        # Qualified: the user may well have other marketplaces installed.
        [target, "plugin", "install", f"{_PLUGIN_NAME}@{_PLUGIN_MARKETPLACE_NAME}"],
    ]


def cmd_plugin_install(args: argparse.Namespace) -> int:
    """Install the published SpecFlow plugin by driving the target tool's own CLI."""
    target = args.target
    steps = _plugin_install_steps(target)

    if args.dry_run:
        for step in steps:
            print(" ".join(step))
        return 0

    if shutil.which(target) is None:
        print(
            f"'{target}' was not found on PATH, so the plugin cannot be installed.\n"
            f"Install {target.capitalize()} Code first, then re-run: "
            f"specflow plugin install --target {target}",
            file=sys.stderr,
        )
        return 1

    for step in steps:
        print(f"$ {' '.join(step)}")
        result = subprocess.run(step)
        if result.returncode != 0:
            print(
                f"'{' '.join(step)}' failed with exit code {result.returncode}. "
                "Nothing further was attempted.",
                file=sys.stderr,
            )
            return result.returncode

    print(
        "\nInstalled. The skills call this CLI, which is already on your PATH — "
        "that is why they are distributed separately.\n"
        "Run /specflow-refine on a spec directory; /specflow-resolve settles what "
        "it finds."
    )
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="specflow",
        description=(
            "SpecFlow CLI — local-only tool for managing code-generation sessions.\n"
            "Use the MCP server for remote/hosted deployments."
        ),
    )
    parser.add_argument(
        "--backend-url",
        default=None,
        help="Backend URL (default: BACKEND_URL env or http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--user-email",
        default=None,
        help="User email (default: USER_EMAIL env)",
    )
    parser.add_argument(
        "--root-path",
        default=None,
        help="Project root directory (default: current working directory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the localhost guard for non-local backend URLs",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # init
    p_init = subparsers.add_parser("init", help="One-shot local bootstrap (wraps specflow-init.sh)")
    p_init.add_argument(
        "--max-parallel-runs",
        type=int,
        default=None,
        dest="max_parallel_runs",
        help="Provision K integral sets of 3 workspace repos",
    )
    p_init.add_argument(
        "--skip-build",
        action="store_true",
        dest="skip_build",
        help="Reuse existing docker images (skip build)",
    )
    p_init.add_argument(
        "--reset-local-db",
        action="store_true",
        dest="reset_local_db",
        help="Reset the local SQLite database before seeding",
    )
    p_init.add_argument(
        "--provide-own-repos",
        default=None,
        dest="provide_own_repos",
        metavar="REPO_LIST",
        help="Comma-separated repos to use instead of creating",
    )
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print planned actions without starting services or seeding",
    )

    p_init.set_defaults(func=cmd_init)

    # run-generation
    p_run = subparsers.add_parser("run-generation", help="Upload specs and start code generation")
    p_run.add_argument("--spec-dir", default="specs", help="Spec directory (default: specs)")
    p_run.add_argument("--outputs-dir", default="docs", help="Outputs directory (default: docs)")
    p_run.add_argument("--src-dir", default="src", help="Source directory (default: src)")
    p_run.add_argument(
        "--workspace-count",
        type=int,
        choices=[1, 2, 3],
        default=None,
        dest="workspace_count",
        help="Number of active workspaces (1-3; default from env/config)",
    )

    # check-status
    p_status = subparsers.add_parser("check-status", help="Check progress of a running generation")
    p_status.set_defaults(func=cmd_check_status)

    p_run.set_defaults(func=cmd_run_generation)

    # retry-generation
    p_retry = subparsers.add_parser("retry-generation", help="Retry a failed generation")
    p_retry.add_argument(
        "--generation-id",
        default=None,
        dest="generation_id",
        help="Generation ID (default: from specflow_session.json)",
    )

    p_retry.set_defaults(func=cmd_retry_generation)

    # download-outputs
    p_dl = subparsers.add_parser(
        "download-outputs", help="Download and extract completed generation outputs"
    )
    p_dl.add_argument(
        "--generation-id",
        default=None,
        dest="generation_id",
        help="Generation ID (default: from specflow_session.json)",
    )
    p_dl.add_argument(
        "--outputs-dir",
        default="docs",
        help="Local directory to extract outputs into (default: docs)",
    )

    p_dl.set_defaults(func=cmd_download_outputs)

    # clear-workspace
    p_clear = subparsers.add_parser(
        "clear-workspace",
        help="Free a CLEANING workspace set early (all 3 members)",
    )
    p_clear.add_argument("--set", type=int, required=True, dest="set", help="Set number to clear")
    p_clear.add_argument("--yes", action="store_true", help="Skip confirmation prompt")

    p_clear.set_defaults(func=cmd_clear_workspace)

    # sessions
    p_sessions = subparsers.add_parser("sessions", help="List active generation sessions")
    p_sessions.add_argument(
        "--watch",
        action="store_true",
        help="Poll continuously and send a desktop notification on completion",
    )
    p_sessions.add_argument(
        "--interval",
        type=int,
        default=15,
        metavar="SECONDS",
        help="Polling interval in seconds for --watch mode (default: 15)",
    )

    p_sessions.set_defaults(func=cmd_sessions)

    # tui
    p_tui = subparsers.add_parser("tui", help="Launch the interactive terminal UI")
    p_tui.add_argument(
        "--generation-id",
        default=None,
        dest="generation_id",
        help="Generation ID to attach to (default: from specflow_session.json)",
    )
    p_tui.add_argument(
        "--interval",
        type=int,
        default=3,
        metavar="SECONDS",
        help="Status poll interval in seconds (default: 3)",
    )

    p_tui.set_defaults(func=cmd_tui)

    # refine — the whole group registers itself
    refine_commands.register(subparsers)

    # plugin
    p_plugin = subparsers.add_parser("plugin", help="Install the SpecFlow plugin into an AI coding tool")
    plugin_actions = p_plugin.add_subparsers(dest="plugin_command", metavar="ACTION")
    plugin_actions.required = True
    p_plugin_install = plugin_actions.add_parser("install", help="Install the published plugin")
    p_plugin_install.add_argument(
        "--target",
        default=_DEFAULT_PLUGIN_TARGET,
        choices=sorted(_PLUGIN_TARGETS),
        help=f"Which tool to install into (default: {_DEFAULT_PLUGIN_TARGET})",
    )
    p_plugin_install.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print the commands that would run, without running them",
    )

    p_plugin_install.set_defaults(func=cmd_plugin_install)

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Commands with no backend to talk to. They skip config resolution, the
# localhost guard and the env push — not as exceptions to the normal path, but
# because none of that means anything to them. `init` brings the backend up, so
# it cannot require one; `refine` and `plugin` never touch it at all.
_LOCAL_COMMANDS = {"init", "refine", "plugin"}


def _dispatch(handler: Any, args: argparse.Namespace) -> int:
    """Run a command handler, awaiting it only if it is actually async.

    Backend commands are coroutines; the local ones are plain functions. Which
    it is belongs to the handler, not to the caller, so this asks rather than
    keeping a second list to fall out of step with the first.
    """
    try:
        result = handler(args)
        return asyncio.run(result) if inspect.isawaitable(result) else result
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s - %(message)s",
    )

    parser = _build_parser()
    args = parser.parse_args()

    # Every parser carries its own handler. Resolved here rather than from a
    # module-level table because the table binds function objects at import
    # time, which silently defeats patching them in tests.
    handler = args.func

    # Local commands run before any backend config is resolved or guarded —
    # see _LOCAL_COMMANDS for why that is not an exception but the correct path.
    if args.command in _LOCAL_COMMANDS:
        sys.exit(_dispatch(handler, args))

    # Determine root early so config resolution can read mcp-config.json
    root = resolve_root(getattr(args, "root_path", None))

    # Resolve backend config (flag → env → mcp-config.json → default)
    workspace_count_flag = getattr(args, "workspace_count", None)
    backend_url, user_email, workspace_count = resolve_backend_config(
        backend_url_flag=getattr(args, "backend_url", None),
        user_email_flag=getattr(args, "user_email", None),
        workspace_count_flag=workspace_count_flag,
        root=root,
    )

    # Propagate resolved workspace_count back to args so cmd_run_generation picks it up
    if not hasattr(args, "workspace_count") or args.workspace_count is None:
        args.workspace_count = workspace_count

    # Localhost guard
    check_localhost_guard(backend_url, force=args.force)

    # Push config into env before importing service singletons
    _configure_env(backend_url, user_email)

    sys.exit(_dispatch(handler, args))


if __name__ == "__main__":
    main()
