Debug or modify state machine logic:

1. Understand state machine architecture:
   - Read docs/ARCHITECTURE.md (State Machine Layer)
   - Read backend/app/state/estimation_state_machine.py
   - Read backend/app/state/workspace_state_machine.py
   - Read backend/app/state/transitions.py (all valid transitions)

2. Key principles (STEEL COMMANDMENTS):
   - State machines are ONLY writers of status/checkpoint
   - No code outside backend/app/state/ may write these fields
   - CI enforces this: ci/check_state_writes.sh
   - Every transition logged in state_history
   - Invalid transitions raise immediately
   - Checkpoints never go backward

3. Common tasks:

   **Add new transition**:
   a. Add to ESTIMATION_TRANSITIONS or WORKSPACE_TRANSITIONS in transitions.py
   b. Add method to EstimationStateMachine or WorkspaceStateMachine
   c. Method must: validate transition, write status, append state_history
   d. Add tests to backend/test/test_state/
   e. Update docs/ARCHITECTURE.md state diagrams

   **Fix invalid transition error**:
   a. Check current status in Firestore
   b. Review transitions.py for valid paths
   c. Check if estimation stuck in FAILED (cannot resume)
   d. Use retry with workspace reuse if code not archived

   **Debug transition not happening**:
   a. Check state_history in Firestore document
   b. Look for InvalidEstimationStateError in logs
   c. Verify caller using state machine (not direct DB write)
   d. Check if CI guard would catch it: `./ci/check_state_writes.sh`

4. State machine methods (EstimationStateMachine):
   - create() - PENDING
   - begin_allocation() - PENDING → INITIALIZING
   - allocation_succeeded() - INITIALIZING → RUNNING
   - allocation_failed() - INITIALIZING → FAILED
   - advance_checkpoint() - Update progress
   - fail() - * → FAILED
   - stuck_detected() - Write failed_at, keep workspaces ALLOCATED
   - complete() - RUNNING → COMPLETED, archive_and_release()

5. State machine methods (WorkspaceStateMachine):
   - allocate() - AVAILABLE → ALLOCATED
   - archive_and_release() - ALLOCATED → CLEANING
   - allocation_rollback() - INITIALIZING estimation failed
   - mark_available() - CLEANING → AVAILABLE
   - force_release() - Operator only, audit trail required

6. Testing state machines:
   ```bash
   cd backend && uv run pytest test/test_state/ -v
   ```
   - Test all valid transitions
   - Test invalid transitions raise
   - Test state_history logging
   - Test checkpoint ordering

Reference:
- State machines: backend/app/state/
- Transitions: backend/app/state/transitions.py
- Tests: backend/test/test_state/
- CI guard: ci/check_state_writes.sh
- STEEL COMMANDMENTS: CLAUDE.md (rules VII-X)
