Parity: same intent as Cursor `/review` (`.cursor/commands/review.md`). For PR-style `gh` review, use `review-backend.md`.

Review code changes against SpecFlow project standards:

1. Check that state changes go through `backend/app/state/` machines only
2. Verify no direct Firestore writes for status/checkpoint/workspace_phases
3. Confirm tests still pass (baseline: 584+)
4. Check type hints on all function signatures
5. Verify error handling follows guard clause pattern
6. Confirm logging includes context (request_id, generation_id, etc.)
7. Check async/await usage is correct
8. Verify STEEL COMMANDMENTS compliance (workspace safety)
9. Check for anti-patterns (see `.cursor/rules/backend-python.mdc`)
10. Confirm no absolute paths in generated files

Run `make check` and `make unit-tests` to validate.
