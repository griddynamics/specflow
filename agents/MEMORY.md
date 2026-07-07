# Agent memory

Concise, generalized lessons (not a changelog — that is `agents/IMPLEMENTATION.md`).

- Prefer verifying state machine transitions in code against `transitions.py` and CI `check_state_writes.sh` before assuming a Firestore field can be set outside `state/`.
- When MCP session paths differ from workspace paths, always apply `apply_mcp_project_root_from_context` (and related) before assuming `specflow_session.json` or roots exist.
- Test failures after async refactors: check for shared `TelemetryContext` or dict mutation across tasks.
- For first-run `run_generation`, validate upload contract before creating generation sessions or allocating workspace sets; once `/workspace/sync` returns a `generation_id`, MCP may persist `specflow_session.json`.
- When fixing a rejection/validation bug, enumerate every rejection code and every path that raises that rejection before planning the side-effect boundary; do not plan from only the observed failure case.
- Firestore emulator imports require the `*overall_export_metadata` file path; export destinations are directories, but `--import-data` must not point at the snapshot directory itself.
- Global SpecFlow/TUI settings live in `~/.specflow/config.json` (SSOT) — read/write via `mcp_server/tui/mcp_clients.py` `_read_config`/`_write_config`, which preserve unknown top-level keys; add each new setting as its own top-level section. Do NOT put global settings in the project-local `.specflow-local/` (that dir is per-project runtime: `mcp-config.json`, `init.log`; the workspace pool now lives only in the SQLite DB, seeded straight in by `create_generation_session_repos.py` — no `workspaces.json` flat file). MCP-client connection status is stored globally because connecting a client is a machine-wide act (`claude/gemini mcp add -s user`, Cursor `~/.cursor/mcp.json`).
- Run MCP server pytest commands from `mcp_server/` (`uv run pytest tests/...`); root-level `uv run pytest ...` may not expose a pytest executable because the repo has per-package `pyproject.toml` environments.
