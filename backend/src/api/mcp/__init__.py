"""The MCP server role (``specflow mcp``): Streamable HTTP at ``/mcp``, its own bearer guard.

Nothing here imports FastAPI or the REST modules, and ``api/__init__`` never imports this
package (import rule M2 of docs/mcp-server/architecture.md).
"""
