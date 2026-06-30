"""SpecFlow interactive terminal UI (local-only).

A view layer over the existing CLI service functions — it adds no business
logic and no new backend routes. Everything network/file goes through the
shared ``services`` package (``cli_service``, ``specflow_backend``, ``session``,
``tool_helpers``) exactly as the thin CLI and MCP server do.

Textual (the only TUI runtime dependency) is a base dependency of the package, so
``specflow tui`` works from any install. ``cli.py`` still imports this package lazily
— so the MCP/CLI code paths never load Textual — and guards the import to give a clear
message if an install is somehow incomplete. ``app.run_tui`` is the only entry point
the CLI calls.
"""
