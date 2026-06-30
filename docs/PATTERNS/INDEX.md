# Patterns & quality bar — backend

**Purpose:** Short pointer for AI and humans; keep one level of indirection to detailed standards.

- **Principles (runtime code):** Single responsibility, DRY, small functions, clear types — align with `CLAUDE.md` coding patterns and `backend/app/standards/` (`feature_implementation_standards.md`, `commit_standards.md`, `deployment_standards.md`, `tech_stacks.md`).
- **State & Firestore:** Only `backend/app/state/` writes status/checkpoint/workspace fields; use state machines, never ad-hoc Firestore — see `docs/ARCHITECTURE.md` and STEEL in `CLAUDE.md`.
- **Metrics:** After substantive backend work, use `make check` and, for complexity, `make check-complexity` / `make check-complexity-diff` (or per-file `make check-complexity-cc FILE=app/...`).

IDE hooks (Cursor) and the `backend-quality-gate` skill (Claude Code) nudge the same checks after Python edits; they do not replace CI or review.
