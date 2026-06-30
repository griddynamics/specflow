---
name: backend-quality-gate
description: After substantive edits under backend/app, run static checks and optional Radon complexity (matches Makefile and Cursor post-write hook intent).
---

# Backend quality gate (SpecFlow)

Use when finishing or reviewing a change that touches `backend/app/**/*.py`.

## Steps

1. From repo root: `make check` (ruff, mypy, vulture as in Makefile).
2. If the change is non-trivial: `make check-complexity` (summary) or `make check-complexity-diff` against `main` (see `CLAUDE.md` for `METRIC=cc|mi|hal`).
3. For a single file: `make check-complexity-cc FILE=app/...` and `make check-complexity-mi FILE=app/...`.
4. Confirm SRP, DRY, and that state/credential rules match `docs/PATTERNS/INDEX.md` and `.cursor/rules/backend-python.mdc`.

**Note:** Cursor may show ruff/radon inline via `.cursor/hooks.json`; that does not replace `make check` or tests.

## Tests

- `make unit-tests` (required before merge for substantive work)
