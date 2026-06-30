You are an expert reviewer for this AI SDLC accelerator codebase. Your job is to find real bugs, not style issues.

## Context

This backend uses a strict state machine architecture. The canonical sources of truth are:
- `backend/app/schemas/estimation_enums.py` — `EstimationStatus`, `EstimationCheckpoint`, `WorkspaceStatus`, `CHECKPOINT_ORDER`
- `backend/app/state/` — the ONLY place allowed to write status/checkpoint/workspace_phases to Firestore
- `backend/app/state/transitions.py` — legal transitions
- `CLAUDE.md` — Steel Commandments (especially I–X)

## Steps

1. If no PR number given, run `gh pr list` and ask which one to review.
2. Run `gh pr diff <number>` to get the full diff.
3. Run `gh pr view <number>` for the description.

## What to check — in this exact priority order

### 🔴 CRITICAL — These are production bugs if missed

**1. Enum contract violations (`estimation_enums.py`)**
- Was `EstimationStatus` modified (values added, removed, renamed, reordered)?
- Was `EstimationCheckpoint` modified?
- Was `CHECKPOINT_ORDER` list changed? This controls checkpoint ordering validation — any change breaks `advance_checkpoint()` for all in-flight estimations.
- Was `WorkspaceStatus` modified?
- Flag ANY change to this file. Even adding a new value can break running estimations that don't know about it.

**2. Direct Firestore writes outside `backend/app/state/`** (Commandment VII)
Look for any of these patterns outside `backend/app/state/`:
- `db.update("estimations", ...)` or `db.set("estimations", ...)` with `status`, `checkpoint`, `workspace_phases`, or `workspace_phases_deployment`
- `self._esm._db.` accessed from service or workflow files
- `self.db.update(...)` writing status/checkpoint fields directly
- String literals like `{"status": "running"}` or `{"checkpoint": "generation_done"}` written directly
The CI guard (`ci/check_state_writes.sh`) catches most but not all patterns. Review manually too.

**3. Missing checkpoint advancement**
Every major workflow step that completes a logical phase MUST advance `EstimationCheckpoint` via `estimation_service.update_checkpoint()` or `_esm.advance_checkpoint()`. Check:
- Does each new workflow step call `update_checkpoint` at the end?
- Is the checkpoint value from `EstimationCheckpoint` (never a bare string)?
- Is the checkpoint consistent with `CHECKPOINT_ORDER`? (must go forward)
- For LOCAL_ONLY vs INTEGRATION_TESTS_READY paths: does each path advance to the correct checkpoint? (e.g. LOCAL_ONLY skips `DEPLOY_AND_E2E_DONE`)

**4. Missing `state_history` append** (Commandment IX)
Any new state machine method that writes to Firestore must append to `state_history` with: `status`, `at` (UTC), `triggered_by`, `metadata`. Check `_record_phase_progress_for_field`, `init_deployment_phases`, and any new methods added in `backend/app/state/`.

**5. Workspace safety violations** (Commandments I–VI)
- Does `fail()` or `stuck_detected()` release any workspace? It must NOT — only `failed_at` timestamp side-effect is allowed.
- Does any code path touch an ALLOCATED workspace from a background job? Only `stuck_detected()` on the estimation is allowed.
- Does `complete()` still require `outputs_archived == True` AND archive branch confirmed before releasing workspaces?
- On retry: if `code_archived == False`, does it reuse the same `workspace_ids`? Never fall back to fresh workspaces silently.

### 🟡 HIGH — Logic bugs that cause silent corruption

**6. Brittle status/checkpoint string comparisons**
Look for:
- `if status == "running":` — must use `EstimationStatus.RUNNING`
- `if checkpoint == "generation_done":` — must use `EstimationCheckpoint.GENERATION_DONE`
- `doc.get("status") == "failed"` — same issue
- Resume logic using raw dict string keys for status comparisons (using `.get("last_completed_phase", 0)` for data fields is fine; comparing status values is not)

**7. Invalid transition action names in `_validate_transition` / `_require_running`**
- Is a method using `_validate_transition(doc, "advance_checkpoint", ...)` when it's not actually advancing a checkpoint? The action name appears in error messages and logs — misuse produces misleading diagnostics.
- New state machine methods that only need a RUNNING guard should use `_require_running(doc, "method_name", generation_id)`.

**8. Checkpoint order violations**
- Is `advance_checkpoint()` called with a checkpoint that could go backward? (e.g., `GENERATION_DONE` after `ESTIMATION_DONE`)
- On retry/resume: does the code respect the saved checkpoint and resume forward from it? Does it ever reset checkpoint to an earlier value?

**9. `init_*` methods that aren't idempotent**
Any init method called before a loop (like `init_deployment_phases`) must be a no-op if already initialized, to survive retries. Check that new init methods guard against overwriting existing progress.

### 🔴 CRITICAL — Graceful failure and audit logging

**12. Hard stoppers in non-essential operations**
Non-essential operations (P10Y estimation, archiving, notifications, metric collection, telemetry) must NEVER crash the workflow. Check:
- Does any new non-essential step propagate an unhandled exception that would abort the whole estimation run?
- Are errors caught, logged with `logger.warning`/`logger.error`, and stored in the relevant Firestore audit fields (e.g. `state_history`, error context on the estimation/workspace doc)?
- `assert` statements are forbidden outside of tests — they raise `AssertionError` (stripped with `-O`) and produce opaque crashes. Replace with a typed exception and a clear message.
- Background tasks (archiving, notifications) must catch and log their own exceptions so a failure does not surface to the caller or abort an in-progress estimation.

### 🟠 MEDIUM — Robustness issues

**10. Resume logic correctness**
For any new phase loop (generation or deployment):
- Does `last_completed >= total_phases` correctly skip already-done workspaces?
- Does `last_completed > 0` correctly set `start_phase = last_completed + 1`?
- Is `total_phases` read from the stored checkpoint data (not recalculated), so a retry with a different plan doesn't corrupt resume?

**11. `workspace_ids` assumptions**
- Does new code assume `workspace_ids` is always populated? It may be empty before allocation.
- Is `workspace_ids` ever modified outside of the allocation path?

### 🟡 HIGH — CLAUDE.md coding pattern adherence

**13. Coding pattern violations**
Check new code against the mandatory patterns from `CLAUDE.md`:
- **Pydantic/dataclasses/OOP over raw dicts and strings** — new data passed between functions should use typed models, not bare `dict`/`str`.
- **Enums over string literals** — any new status, type, or category value must be an `Enum` member, not a bare string.
- **SRP / Open-Closed** — new classes should have a single responsibility; extensions should not require modifying existing classes.
- **Imports at the top of the file** — no lazy imports inside functions or methods.
- **Small functions / DRY** — flag functions that duplicate logic already present elsewhere, or that are doing more than one thing.
- **No raw collections as public API** — functions returning `dict`/`list` for structured data should return a typed model instead.
- **Model with the right paradigm for clarity** — choose the tool that makes the domain intent obvious at the call site: pure functions for stateless transforms, magic methods for natural domain operations (`current + delta` via `__add__` instead of manual field construction), OOP for encapsulating state and invariants. Flag code that uses a weaker paradigm when a stronger one would eliminate boilerplate and make the intent self-evident.

## What NOT to flag

- Minor inefficiencies (extra dict copy, redundant log line)
- Test structure preferences
- Docstring/comment quality on unchanged code
- The `progress` field — this is explicitly exempt from the state machine write guard (it's owned by the workflow display layer)

## Output format

Group findings by severity. For each finding:
- File and line number
- The exact problematic code snippet
- Why it's a bug (reference the relevant Commandment or rule above)
- The correct fix

End with a one-line verdict: **APPROVED**, **APPROVED WITH NITS**, or **CHANGES REQUIRED**.
