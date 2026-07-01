# IDE setup — Cursor and Claude Code (in-repo)

This repository ships **shared intent** for both IDEs: the same engineering rules, slash-style command prompts, and backend quality expectations.

## Cursor

- **Rules**: `.cursor/rules/*.mdc` (always-on and request-classified workflows). Start a **new chat** after large rule changes so the client reloads them.
- **Commands**: `.cursor/commands/*.md` — use as `/command` name in Cursor (e.g. review, test).
- **Hooks**: `.cursor/hooks.json` — after a **Write** tool on backend Python, a script runs `ruff` and `radon` and injects `additional_context` (hints only). Install: ensure `uv` is on `PATH` and open **Hooks** in Cursor settings; restart if hooks do not load.
- **MCP**: User-level MCP config (not committed). See `README.md` for SpecFlow MCP env.

### MCP config: local self-host

The MCP runs **inside your IDE** as a local process (it is never part of the backend
Docker stack). Local config can omit `BACKEND_URL` and should omit `SPECFLOW_API_KEY`;
the backend authorises via its seeded local identity. `specflow-init.sh` writes a
ready-to-paste snippet to `.specflow-local/mcp-config.json`:


  ```json
  {
    "mcpServers": {
      "specflow": {
        "command": "uvx",
        "args": ["--from", "/abs/path/to/mcp_server", "specflow-mcp"],
        "env": {
          "USER_EMAIL": "you@example.com"
        }
      }
    }
  }
  ```

**Guided setup (recommended):** instead of hand-editing client config, run
`specflow tui` and press **`c`** (*Add MCP to AI tool*). The setup screen detects
your installed clients and registers SpecFlow for you:

- **Claude Code** — runs `claude mcp add-json … -s user` and verifies the server with `claude mcp get`.
- **Gemini CLI** — runs `gemini mcp add … -s user` (trust the folder if Gemini shows it disabled).
- **Cursor** — writes/merges `~/.cursor/mcp.json` and opens Cursor's quick-install; since Cursor
  has no read-back, the status stays **"added — confirm in your client"** until you reopen the
  screen and confirm it (or report it isn't working).
- **Other clients** — shows the exact JSON + path to copy.

Statuses persist in the global `~/.specflow/config.json` (under a `clients` section), so an
unverified add is never assumed connected — and the TUI can read it from any project.

See the [Local Self-Host Quickstart](../QUICKSTART.md).

## Claude Code

- **Project instructions**: `CLAUDE.md` (root) is the primary project file.
- **Commands**: `.claude/commands/*.md` — parallel prompts to `.cursor/commands/` (same workflows; e.g. `review-specflow` next to `review-backend`).
- **Skills**: `.claude/skills/` — e.g. `deploy-requirements`, `backend-quality-gate`, (run `make check` / complexity when the skill says to).
- **No Cursor hooks in Claude** — use the `backend-quality-gate` skill after substantial `backend/app` edits, or run `make check` and `make check-complexity` yourself.

## Cross-IDE checklist

1. Read `docs/CONTEXT.md` + `docs/ARCHITECTURE.md` for orientation.
2. Follow `CLAUDE.md` and STEEL commandments for backend work.
3. Run `make unit-tests` and `make check` before merge; use complexity commands from `CLAUDE.md` for non-trivial backend changes.
