# Contributing to SpecFlow

**Who is this for?** First-time and returning contributors.
**When should I read this?** Before your first PR, and as a checklist for every PR after.

---

## Before You Start

- Read the [README](../README.md) to understand what SpecFlow is and how to run it
- Skim the [Architecture](ARCHITECTURE.md) — system design and data flow
- Read [CLAUDE.md](../CLAUDE.md) — the development protocol, coding patterns, and the **⛔ STEEL COMMANDMENTS**. These are non-negotiable invariants around generated code and state machines; a change that violates one will be sent back.
- Working on the backend? Follow the [Backend Development Guide](backend/DEVELOPMENT.md). Working on the MCP server? See [MCP_USER.md](../MCP_USER.md) and the [MCP API Reference](mcp/API_REFERENCE.md).

## What Contributions Are Welcome

- **Documentation** — fixes, clarifications, new guides
- **Bug fixes** — in the backend, the MCP server, or the TUI
- **Tooling** — CLI/TUI improvements, MCP enhancements, developer experience
- **Runtime support** — new language runtimes, build tools, or SDKs (see the checklist below)
- **Feature requests** — open an issue describing the problem and your proposed solution
- **Feedback** — positive or negative, both matter. Tell us what works, what frustrates you, what confuses you. File an issue or start a discussion.

Not sure where your idea fits? Open an issue first.

## How to Contribute
Create changes from private forks!

1. Pick a small, scoped issue (or open one with your proposal)
2. Make focused edits. One concern per PR.
3. Validate locally — `make unit-tests`, `make check`, and any check relevant to your change
4. Submit a PR that explains *why*, not just *what*, and its expected behavioral impact

Small PRs get reviewed faster and merged sooner. For the full local setup, testing, and workflow sequence, see the [Backend Development Guide](backend/DEVELOPMENT.md).

## Coding Standards

SpecFlow is TDD-first. Follow the patterns in [CLAUDE.md](../CLAUDE.md):

- **Small functions, DRY, SRP.** Reuse over duplication; prefer Pydantic models, dataclasses, and Enums over raw strings and loose collections.
- **Type hints everywhere**, imports at the top of the file (no lazy imports), 120-char lines. `make check` runs ruff and mypy.
- **State is sacred.** Only code under `backend/app/state/` may write status/checkpoint/workspace phases. Never make `fail()` or `stuck_detected()` release a workspace. When in doubt, re-read the STEEL COMMANDMENTS.
- **After substantive backend work**, run the complexity diff against `main`: `make check-complexity-diff` (default `METRIC=cc`).

### Adding a new runtime, build tool, or SDK

These must be changed together (see CLAUDE.md → *New runtime/dependency support*):

- Agent **allowed tools** — `backend/app/core/tool_usage.py`
- Per-workspace **cache dirs** — `setup_workspace_cache_directories` in `backend/app/services/claude_code.py`
- **Heavy SDKs and dependencies** — small binaries in `backend/Dockerfile`, large SDKs via `backend/scripts/init-mobile-sdk.sh` or other install script

## AI-Assisted Contributions

AI help is welcome. These norms apply:

- **You own the result.** The author is responsible for every line, whether hand-written or generated.
- **No unexplained bulk diffs.** Large generated changes without clear rationale will be sent back.
- **Small PRs.** Prefer reviewable, focused changes over sweeping rewrites.
- **No fabrication.** Generated content must not introduce secrets, fake docs, fake benchmarks, or unverifiable claims.

## Pull Request Checklist

Before requesting review:

- [ ] Scope is narrow and explicit; one concern per PR
- [ ] `make unit-tests` passes and the pass count is ≥ your baseline
- [ ] `make integration-tests` and `make skip-mode-e2e-tests` passes
- [ ] `make check` passes (ruff + mypy)
- [ ] No STEEL COMMANDMENT or state-machine invariant weakened
- [ ] Rejection-catalog / file-contract changes keep message text and checks in sync (single source of truth)
- [ ] New runtime/SDK support touches all three layers (tools, cache dirs, SDK install)
- [ ] Docs updated in the same changeset — [Architecture](ARCHITECTURE.md) for structural changes
- [ ] Complexity diff run for substantive backend work (`make check-complexity-diff`)
- [ ] PR description explains *why*, not just *what* - we have a "pr-desc" skill as a starter

## Community

This project is licensed under [MIT](../LICENSE). By contributing, you agree that your contributions are licensed under the same terms.

Please treat every interaction with respect. No gatekeeping, no condescension.

---

## Related Docs

- [README](../README.md) — what SpecFlow is, where to start
- [CLAUDE.md](../CLAUDE.md) — development protocol and STEEL commandments
- [QUICKSTART](../QUICKSTART.md) — local self-host setup and first run
- [Architecture](ARCHITECTURE.md) — system structure, components, data flow
- [Backend Development Guide](backend/DEVELOPMENT.md) — setup, testing, project layout
- [MCP API Reference](mcp/API_REFERENCE.md) — MCP tool reference
- [IDE Setup](IDE-SETUP.md) — Cursor + Claude Code configuration
