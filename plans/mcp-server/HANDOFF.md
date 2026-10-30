# HANDOFF — MCP server

Master index of execution status. Plan: [MCP-SERVER-PLAN.md](MCP-SERVER-PLAN.md). Design: `docs/mcp-server/`.

## Status

| # | Session | Status | Handoff |
|---|---|---|---|
| 01 | Graph paged read | not started | `01-handoff.md` (created by the session) |
| 02 | Auth claim profile | not started | `02-handoff.md` |
| 03 | Local OIDC provider and QA harness | not started | `03-handoff.md` |
| 04 | MCP role, transport and auth guard | not started (needs 02) | `04-handoff.md` |
| 05 | Tools and access control | not started (needs 01, 04) | `05-handoff.md` |
| 06 | Stack wiring, real-client QA, docs, release check | not started (needs 03, 05) | `06-handoff.md` |

Next: 01, 02 and 03 in parallel.

## Planning evidence

- Spike (not committed): an MCP SDK 2.3.0 resource server verifying Keycloak 26.4 tokens; Claude Code signed in with
  `claude mcp login --no-browser` (scripted) and called a tool from `claude -p`; the MCP Python SDK
  `OAuthClientProvider` with the pre-registered public client did the same. Facts in [discovery.md](discovery.md).
- Baseline at `01a02b1`: `make check` 3879 passed, 431 skipped; live Neo4j and Postgres tiers 119 passed.

## Blockers

None.

## Deferred decisions

None blocking: the open questions in `docs/mcp-server/questions.md` carry defaults that sessions apply. Items after
version 1: `docs/mcp-server/later.md`.

## Common issues

| Issue | Fix |
|---|---|
| `docker info` cannot reach the daemon | start `dockerd` |
| Docker Hub 429 | pull from `mirror.gcr.io` and tag |
| Keycloak login over plain HTTP fails with 400 | forward the `Secure` cookies by hand |
| `claude mcp login` output has no plain URL | read it from the OSC 8 escape |
| Leftover QA server or tokens in `~/.claude` | `claude mcp logout <name>`; `claude mcp remove <name> --scope local` |
| `python -m pystatic.docs.indexes --check` reports `INDEX.md` stale | pre-existing on main and not part of `make validate`; leave it |

## Session handoff template

`<NN>-handoff.md`: status · what changed (paths) · requirement coverage (owned requirement id → test or evidence) ·
evidence (commands and results) · deviations (decision, why, proposed change) · open issues · next-session notes.
Terse, factual, no tokens or secrets.
