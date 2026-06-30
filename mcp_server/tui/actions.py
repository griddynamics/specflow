"""In-app actions — thin wrappers over the existing CLI command handlers.

These deliberately call the same ``cmd_*`` coroutines the standalone
subcommands use (``cli.cmd_retry_generation`` etc.), so every guard, capacity
message, precheck, and backend call stays in exactly one place. The TUI never
re-implements those flows; it suspends its screen (see ``app.py``) and runs the
real handler, which prints its familiar output to the terminal.

Each wrapper builds the ``argparse.Namespace`` the handler expects and returns
its integer exit code. ``--yes`` is forced on for the workspace clear because
the TUI gathers confirmation through its own dialog before calling.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import cli


def _ns(**overrides: object) -> SimpleNamespace:
    """Build a Namespace with the common CLI fields the handlers read.

    Defaults mirror the argparse defaults in ``cli._build_parser`` so a handler
    never trips over a missing attribute.
    """
    base: dict[str, object] = {
        "root_path": None,
        "backend_url": None,
        "user_email": None,
        "force": False,
        "generation_id": None,
        "spec_dir": "specs",
        "src_dir": "src",
        "outputs_dir": "docs",
        "workspace_count": None,
        "set": None,
        "yes": False,
        "watch": False,
        "interval": 15,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def do_retry(root: Path) -> int:
    """Retry the current generation (reuses ``cmd_retry_generation`` guards)."""
    return await cli.cmd_retry_generation(_ns(root_path=str(root)))


async def do_clear_set(set_number: int) -> int:
    """Clear all members of a workspace set (confirmation handled by the TUI)."""
    return await cli.cmd_clear_workspace(_ns(set=set_number, yes=True))
