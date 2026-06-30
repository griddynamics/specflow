"""SpecFlow interactive terminal UI (local-only).

A view layer over the existing CLI service functions — it adds no business
logic and no new backend routes. Everything network/file goes through the
shared ``services`` package (``cli_service``, ``specflow_backend``, ``session``,
``tool_helpers``) exactly as the thin CLI and MCP server do.

The package is shipped behind the optional ``tui`` extra (Textual). ``cli.py``
imports it lazily so the base install carries no TUI dependency; ``app.run_tui``
is the only entry point the CLI calls.
"""
