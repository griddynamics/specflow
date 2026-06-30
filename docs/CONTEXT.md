# Context — SpecFlow

**SpecFlow** is an AI SDLC accelerator: spec analysis and planning run **locally in the IDE** via MCP tools (PR255); a server-side harness runs upload, contract validation, and long-running parallel code generation on isolated workspaces, then deploy/E2E and P10Y-style complexity analysis for variance-reduced estimates.

**Target state**: secure multi-tenant operation on GKE, Firestore-backed state, NFS workspaces, no real customer systems in the loop during generation. External dependencies and credentials are controlled and mocked in tests; private operators keep deployment runbooks outside the public documentation set.

**Who uses it**: platform engineers, teams driving specs through the MCP (Cursor, Claude Code, or other clients) to produce and compare generated implementations.

**Out of scope in repo docs**: one-off feature plans, PR review write-ups, and pre-merge fix lists — see `docs/_archive/ephemeral/` for preserved notes.

**Business vs technical detail**: this file stays non-implementation; see `docs/ARCHITECTURE.md` for system design and `CLAUDE.md` for engineering commandments and day-to-day development protocol.
