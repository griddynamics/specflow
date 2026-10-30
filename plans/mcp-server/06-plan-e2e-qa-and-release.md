# 06 — Stack wiring, real-client QA, docs and release check

Depends on: 03, 05. Runs alone (whole stack; ports 8000, 8002, 8180, 8765).

## Do

1. Compose service `mcp` in profile `mcp` per `docs/mcp-server/deployment.md` (Local stack): backend image, command
   `specflow mcp`, local settings with `AUTH_DEV_SIGNING_KEY` empty, depends on healthy Postgres, Neo4j, Keycloak,
   readiness healthcheck; forward the `AUTH_*` claim profile variables to `api` and `mcp`. Optional host target
   `make mcp` like `make api`, with the same dev-key override.
2. Extend `backend/examples/mcp_qa_lib.py` with the MCP Python SDK client (pre-registered public client, login helper
   as redirect and callback handlers) and `examples/mcp_qa.py` with `check` and `claude` commands.
3. End-to-end tier `backend/tests/test_mcp_e2e.py` (`SPECFLOW_TEST_MCP_E2E=1`): scenarios E1 to E13 of
   `docs/mcp-server/testing.md`; NFR-MCP-01 latency measured and reported.
4. Agent tier `backend/tests/test_mcp_claude_qa.py` (`SPECFLOW_TEST_MCP_CLAUDE=1`): the driver and the three persona
   prompts of testing.md (Agent QA with Claude Code), asserting on tool events only.
5. Run both tiers on the local stack and record per-scenario results and the p95 in `06-handoff.md`.
6. Docs: `docs/configuration.md` (local stack rows; `MCP_*` rows came with 04), `backend/README.md` (MCP section),
   `.env.example` (`MCP_RESOURCE_URL`, empty) with the key sets of `test_specflow_settings_env.py`, `QUICKSTART.md`
   (one paragraph); fold deviations from 01 to 05 into `docs/mcp-server/`; set verified docs to `status: stable`.
7. Security review against auth.md (Security rules) and access-control.md; findings and fixes in the handoff.
8. Release check: `make check`, `make validate`, `make frontend-build`, `make e2e-production`.

## Subagents

- qa-runner (long-running): owns the stack, runs the e2e and agent tiers, collects evidence.
- security-reviewer (short-term): independent review of the delivered role (no access to earlier reviews' conclusions).
- docs-editor (short-term): applies doc changes and runs the docs bundle check.

## Rules

- The local API keeps dev login; QA data comes from the seed, never from the web UI.
- Building `specflow-api:local` in a cloud container may need the proxy notes of that environment; if the image build
  is blocked, run `specflow mcp` on the host against the compose services and record that in the handoff.
- The agent tier spends model tokens and edits the real Claude configuration: unique server name, logout and remove
  in `finally`; never in CI.
- Assert on tool calls and tool results from `stream-json`; never on model prose.
- Port 8765 must be free before each login.
- Docs: no `file.py:NN`, no internal hosts, no tracker keys; keep the bundle check green.

## Done when

- `make up WITH=mcp` (or the host fallback) serves the role healthy with the imported realm (FR-MCP-OPS-08).
- E1 to E13 pass with the Python SDK client; the three agent prompts pass with Claude Code; p95 recorded (NFR-MCP-01).
- The settings-env tests pass (every name read in code has a `docs/configuration.md` row; `.env.example` keys match),
  the docs bundle check passes, and no document in `docs/mcp-server/` still describes a deviation listed in 01 to 05 handoffs.
- The four release commands pass (NFR-MCP-04); the handoff maps FR-MCP-OPS-08 and NFR-MCP-01, 04, 05 to evidence.

## Checklist

- [ ] Compose `mcp` service: image, command, env, dependencies, healthcheck, loopback port 8002
- [ ] `AUTH_*` forwarding for `api` and `mcp`
- [ ] SDK client in the QA library; `check` and `claude` commands
- [ ] E1 to E13 implemented and passing
- [ ] NFR-MCP-01 measured (10 calls per second, 300 features) and recorded
- [ ] Agent prompt 1: `list_projects` called, correct projects
- [ ] Agent prompt 2: all 7 features gathered across pages, no `REQ::` ids
- [ ] Agent prompt 3: `project_not_found`, no foreign feature names anywhere in the run
- [ ] Agent tier teardown verified (`claude mcp list` clean afterwards)
- [ ] Evidence table in `06-handoff.md` (scenario, result, duration); no tokens
- [ ] `docs/configuration.md` local stack rows; settings-env doc test green
- [ ] `.env.example` and the key sets in `test_specflow_settings_env.py` updated together
- [ ] `backend/README.md` MCP section; `QUICKSTART.md`
- [ ] Deviations of 01 to 05 folded into `docs/mcp-server/`; statuses updated
- [ ] Docs bundle check green
- [ ] Security review findings resolved or recorded with owner and reason
- [ ] Secret scan clean; no screenshots or reports committed
- [ ] `make check`, `make validate`, `make frontend-build`, `make e2e-production` green
- [ ] `06-handoff.md` written; `HANDOFF.md` status set to delivered
