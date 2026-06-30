# Agent memory

Concise, generalized lessons (not a changelog — that is `agents/IMPLEMENTATION.md`).

- Prefer verifying state machine transitions in code against `transitions.py` and CI `check_state_writes.sh` before assuming a Firestore field can be set outside `state/`.
- When MCP session paths differ from workspace paths, always apply `apply_mcp_project_root_from_context` (and related) before assuming `specflow_session.json` or roots exist.
- Test failures after async refactors: check for shared `TelemetryContext` or dict mutation across tasks.
- For first-run `run_generation`, validate upload contract before creating generation sessions or allocating workspace sets; once `/workspace/sync` returns a `generation_id`, MCP may persist `specflow_session.json`.
- When fixing a rejection/validation bug, enumerate every rejection code and every path that raises that rejection before planning the side-effect boundary; do not plan from only the observed failure case.
- Firestore emulator imports require the `*overall_export_metadata` file path; export destinations are directories, but `--import-data` must not point at the snapshot directory itself.
