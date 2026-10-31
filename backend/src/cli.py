"""The ``specflow`` command: one entrypoint per deployment role.

``specflow api [--check-config]`` ·
``specflow worker [--queue parse|agents]... [--check] [--check-config]`` ·
``specflow migrate`` · ``specflow mcp [--check-config]``. A role's work lives in a module that is imported only
when its command runs, so ``--check-config`` and ``--help`` never depend on it.
Each work module exposes ``run(settings: RoleSettings)`` returning an exit
code, ``None`` (success) or ``bool``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import inspect
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from logger.correlation import new_request_context
from logger.logger import ERROR, get_logger, log_event
from logger.logger import close as close_logger
from settings import (
    WORKER_QUEUE_NAMES,
    ConfigurationError,
    Role,
    RoleSettings,
    load_settings,
)

logger = get_logger("cli")

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_CONFIGURATION: Final = 2

_API_MODULE: Final = "api.server"
_WORKER_MODULE: Final = "temporal.worker"
_MCP_MODULE: Final = "api.mcp.server"
_MIGRATE_MODULE: Final = "db.migrate"
_HEALTH_MODULE: Final = "health"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specflow", description="Run a specflow role.")
    roles = parser.add_subparsers(dest="role", metavar="{api,worker,migrate,mcp}", required=True)
    api = roles.add_parser("api", help="serve the HTTP API")
    api.add_argument("--check-config", action="store_true", help="validate settings and exit")
    worker = roles.add_parser("worker", help="poll the Temporal task queues")
    worker.add_argument(
        "--queue",
        action="append",
        choices=WORKER_QUEUE_NAMES,
        help="queue to poll, repeatable (default: WORKER_QUEUES)",
    )
    worker.add_argument("--check", action="store_true", help="probe dependencies and exit 0 or 1")
    worker.add_argument("--check-config", action="store_true", help="validate settings and exit")
    roles.add_parser("migrate", help="apply database migrations")
    mcp = roles.add_parser("mcp", help="serve the MCP endpoint")
    mcp.add_argument("--check-config", action="store_true", help="validate settings and exit")
    return parser


def _load(role: Role, queues: Sequence[str] | None = None) -> RoleSettings | None:
    """Load the role's settings; log and return ``None`` when they are refused."""

    try:
        return load_settings(role, queues=queues)
    except ConfigurationError as error:
        log_event(
            logger,
            ERROR,
            "config.validation.failed",
            role=role,
            variables=list(error.variables),
        )
        return None


def _exit_code(result: Any) -> int:
    if result is None or result is True:
        return EXIT_OK
    if result is False:
        return EXIT_FAILED
    if isinstance(result, Mapping):
        # Health checks: every entry is a bool or has a boolean `.ok`; none is not healthy.
        healthy = bool(result) and all(getattr(check, "ok", check) is True for check in result.values())
        return EXIT_OK if healthy else EXIT_FAILED
    return int(result)


def _run(module_name: str, attribute: str, *args: Any, **kwargs: Any) -> int:
    """Import ``module_name`` now and call ``attribute``; a missing module is a clear error."""

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        parts = module_name.split(".")
        if error.name not in {".".join(parts[: i + 1]) for i in range(len(parts))}:
            raise
        log_event(
            logger,
            ERROR,
            "cli.module.unavailable",
            f"{module_name} is not available in this build.",
            target_module=module_name,
        )
        return EXIT_FAILED
    function: Callable[..., Any] = getattr(module, attribute)
    result = function(*args, **kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    return _exit_code(result)


async def _await(awaitable: Any) -> Any:
    return await awaitable


def _api(args: argparse.Namespace) -> int:
    settings = _load("api")
    if settings is None:
        return EXIT_CONFIGURATION
    if args.check_config:
        return EXIT_OK
    return _run(_API_MODULE, "run", settings)


def _mcp(args: argparse.Namespace) -> int:
    settings = _load("mcp")
    if settings is None:
        return EXIT_CONFIGURATION
    if args.check_config:
        return EXIT_OK
    return _run(_MCP_MODULE, "run", settings)


def _worker(args: argparse.Namespace) -> int:
    settings = _load("worker", args.queue)
    if settings is None:
        return EXIT_CONFIGURATION
    if args.check_config:
        return EXIT_OK
    if args.check:
        # The parsed settings carry --queue; a reload from the environment would not.
        return _run(_HEALTH_MODULE, "run_checks", "worker", settings=settings)
    return _run(_WORKER_MODULE, "run", settings)


def _migrate(_args: argparse.Namespace) -> int:
    settings = _load("migrate")
    if settings is None:
        return EXIT_CONFIGURATION
    return _run(_MIGRATE_MODULE, "run", settings)


_COMMANDS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "api": _api,
    "worker": _worker,
    "migrate": _migrate,
    "mcp": _mcp,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; the process exit code is the return value."""

    # onnxruntime (pulled in by the PDF parser) sends telemetry to a third party by
    # default and its exit-time thread can abort the interpreter; opt out up front.
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

    with new_request_context():
        try:
            args = _parser().parse_args(argv)
            return _COMMANDS[args.role](args)
        finally:
            close_logger()
