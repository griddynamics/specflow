"""
Baseline regression test for state management refactoring.
This test must pass at the END of every phase.
It drives a complete generation lifecycle through SKIP_MODE and
asserts the exact ordered sequence of status + checkpoint changes.

Tag: @pytest.mark.regression
Run with: pytest -m regression
"""
# Patch logging configuration BEFORE any app imports to avoid filesystem issues
# (configure_logging tries to mkdir /agent_logs which doesn't exist in dev/CI)
from unittest.mock import AsyncMock, Mock
import app.core.logging as _logging_module
from app.state.db_adapter import COL_GENERATION_SESSIONS
_logging_module.configure_logging = Mock()

import tempfile  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from app.database.memory import InMemoryDatabase  # noqa: E402
from app.schemas.generation_workflow_enums import GenerationCheckpoint, GenerationStatus  # noqa: E402
from app.services.generation_session import GenerationSessionService  # noqa: E402
from app.services.workspace_pool import WorkspacePoolService  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db():
    """Fresh in-memory database for each test."""
    db = InMemoryDatabase()
    yield db
    db.clear()


@pytest.fixture
def temp_workspace_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def workspace_pool(mock_db, temp_workspace_dir):
    service = WorkspacePoolService(mock_db, workspace_base_path=temp_workspace_dir)

    async def mock_ensure_repo_cloned(workspace_id: str, ws_doc: dict, generation_id: str):
        workspace_path = service._get_workspace_path(workspace_id)
        workspace_path.mkdir(parents=True, exist_ok=True)
        (workspace_path / ".git").mkdir(exist_ok=True)

    service._ensure_repo_cloned = AsyncMock(side_effect=mock_ensure_repo_cloned)
    return service


@pytest.fixture
def generation_session_service(mock_db, workspace_pool):
    return GenerationSessionService(mock_db, workspace_pool)


@pytest.fixture
def sample_workspaces(mock_db):
    now = datetime.now(timezone.utc)
    for i in range(1, 4):
        ws_id = f"ws-01-{i}"
        mock_db.set("workspaces", ws_id, {
            "repo_url": f"https://github.com/org/workspace-1-{i}",
            "p10y_repository_id": 74910 + i,
            "workspace_pool": "default",
            "set_number": 1,
            "status": "available",
            "locked_by": None,
            "locked_at": None,
            "lease_expires_at": None,
            "clean_verified": True,
            "last_used_by": None,
            "last_cleaned_at": now,
            "allocation_history": [],
            "error": None,
        })


@pytest.fixture
def skip_mode_context():
    """Fixture that enables SKIP_MODE for tests that drive the workflow layer.

    Phase 0 tests operate at the service layer only (no workflow invocation),
    so this fixture is a no-op here. It is kept in the signature so later
    phases can activate SKIP_MODE without changing method signatures.
    """
    yield


# ---------------------------------------------------------------------------
# Helper — collect status sequence from state_history
# ---------------------------------------------------------------------------

def _status_sequence(db: InMemoryDatabase, generation_id: str) -> list[str]:
    doc = db.get(COL_GENERATION_SESSIONS, generation_id)
    return [entry["status"] for entry in doc.get("state_history", [])]


def _checkpoint_value(db: InMemoryDatabase, generation_id: str) -> str | None:
    doc = db.get(COL_GENERATION_SESSIONS, generation_id)
    return doc.get("checkpoint")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.regression
class TestGenerationLifecycleBaseline:

    @pytest.mark.asyncio
    async def test_complete_lifecycle_status_sequence(
        self, mock_db, skip_mode_context, generation_session_service, sample_workspaces
    ):
        """
        Full lifecycle: create → start → complete.
        Assert state_history entries appear in exactly this order.
        """
        expected_status_sequence = [
            GenerationStatus.PENDING.value,
            GenerationStatus.INITIALIZING.value,
            GenerationStatus.RUNNING.value,
            GenerationStatus.COMPLETED.value,
        ]

        # Create
        est_id = await generation_session_service.create_generation_session(
            user_email="regression@example.com",
            parameters={"spec_file": "spec.md", "model": "claude-sonnet-4"},
        )
        doc = mock_db.get(COL_GENERATION_SESSIONS, est_id)
        assert doc["status"] == GenerationStatus.PENDING.value

        # Start (allocates workspaces, transitions PENDING → INITIALIZING → RUNNING)
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        assert len(workspace_ids) == 3

        doc = mock_db.get(COL_GENERATION_SESSIONS, est_id)
        assert doc["status"] == GenerationStatus.RUNNING.value

        # Mimic checkpoint OUTPUTS_ARCHIVED (see GenerationSessionStateMachine.advance_checkpoint)
        from app.services.artifact_store import ARTIFACTS_BASE

        mock_db.update(
            COL_GENERATION_SESSIONS,
            est_id,
            {
                "outputs_archived": True,
                "artifact_path": str(ARTIFACTS_BASE / est_id),
            },
        )
        generation_session_service._archive_svc.verify_archive_branch = AsyncMock(return_value=True)

        # Complete
        await generation_session_service.complete_generation_session(
            est_id,
            result={"p10y_scores": [], "commit_count": 0},
        )

        # Assert status sequence in state_history
        sequence = _status_sequence(mock_db, est_id)
        assert sequence == expected_status_sequence, (
            f"Expected status sequence {expected_status_sequence}, got {sequence}"
        )

        # Assert final status
        doc = mock_db.get(COL_GENERATION_SESSIONS, est_id)
        assert doc["status"] == GenerationStatus.COMPLETED.value
        assert doc["completed_at"] is not None

    @pytest.mark.asyncio
    async def test_checkpoint_sequence_is_unidirectional(
        self, mock_db, skip_mode_context, generation_session_service, sample_workspaces
    ):
        """
        Assert checkpoints only advance forward when called in order.
        advance_checkpoint enforces this: backward moves raise (Phase 2 invariant).
        """
        # Checkpoints to advance through (FILES_UPLOADED is set at creation, skip it).
        advance_sequence = [
            GenerationCheckpoint.CONTRACT_VALIDATED,
            GenerationCheckpoint.KB_INIT_DONE,
            GenerationCheckpoint.GENERATION_DONE,
            GenerationCheckpoint.OUTPUTS_ARCHIVED,
            GenerationCheckpoint.ESTIMATION_DONE,
        ]

        est_id = await generation_session_service.create_generation_session(
            user_email="regression@example.com",
            parameters={"spec_file": "spec.md"},
        )
        # Must be RUNNING to advance checkpoints
        await generation_session_service.start_generation_session(est_id)

        # Verify initial checkpoint set at creation
        assert _checkpoint_value(mock_db, est_id) == GenerationCheckpoint.FILES_UPLOADED.value

        observed_sequence = []

        for checkpoint in advance_sequence:
            await generation_session_service.update_checkpoint(est_id, checkpoint)
            current = _checkpoint_value(mock_db, est_id)
            observed_sequence.append(current)

        # Verify each checkpoint was stored correctly
        assert observed_sequence == [cp.value for cp in advance_sequence], (
            f"Checkpoint sequence mismatch: {observed_sequence}"
        )

        # Verify the final stored checkpoint is the last one
        assert _checkpoint_value(mock_db, est_id) == GenerationCheckpoint.ESTIMATION_DONE.value

        # Verify the sequence is strictly forward
        for i in range(1, len(observed_sequence)):
            prev = GenerationCheckpoint(observed_sequence[i - 1])
            curr = GenerationCheckpoint(observed_sequence[i])
            assert advance_sequence.index(curr) > advance_sequence.index(prev), (
                f"Checkpoint went backward: {prev.value} → {curr.value}"
            )

    @pytest.mark.asyncio
    async def test_fail_and_retry_preserves_workspace_ids(
        self, mock_db, skip_mode_context, generation_session_service, sample_workspaces
    ):
        """
        Assert that after fail(), workspace_ids remain on the generation document
        so a retry can reuse the exact same workspaces (Commandment V).
        Workspaces stay ALLOCATED — code on filesystem is preserved (Commandment II).
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="regression@example.com",
            parameters={"spec_file": "spec.md"},
        )
        original_workspace_ids = set(await generation_session_service.start_generation_session(est_id))
        assert len(original_workspace_ids) == 3

        # Fail the generation — workspaces must stay ALLOCATED (Commandment II)
        await generation_session_service.fail_generation_session(est_id, "test failure")

        # Commandment V: workspace_ids remain on the generation doc after fail.
        # A retry uses these IDs to reuse the same workspaces.
        est_doc = mock_db.get(COL_GENERATION_SESSIONS, est_id)
        assert set(est_doc.get("workspace_ids", [])) == original_workspace_ids, (
            f"workspace_ids must be preserved on generation doc after fail. "
            f"Original: {original_workspace_ids}, Found: {est_doc.get('workspace_ids')}"
        )

        # Workspaces must remain ALLOCATED — generated code is intact
        for ws_id in original_workspace_ids:
            ws_doc = mock_db.get("workspaces", ws_id)
            assert ws_doc is not None, f"Workspace {ws_id} disappeared after fail"
            assert ws_doc["status"] == "allocated", (
                f"Workspace {ws_id} must stay ALLOCATED after fail, got {ws_doc['status']}"
            )

    @pytest.mark.asyncio
    async def test_fail_does_not_release_workspace(
        self, mock_db, skip_mode_context, generation_session_service, sample_workspaces
    ):
        """
        After fail(), workspace status must still be ALLOCATED.
        Generated code on the workspace filesystem must be preserved.

        FAILS on current code: fail_generation_session() calls release_workspaces()
        which transitions workspaces to CLEANING (wiping the filesystem).
        Phase 2 fixes this by wiring fail() through GenerationSessionStateMachine
        which does NOT release workspaces.
        """
        est_id = await generation_session_service.create_generation_session(
            user_email="regression@example.com",
            parameters={"spec_file": "spec.md"},
        )
        workspace_ids = await generation_session_service.start_generation_session(est_id)
        assert len(workspace_ids) == 3

        # Verify workspaces are ALLOCATED before fail
        for ws_id in workspace_ids:
            ws_doc = mock_db.get("workspaces", ws_id)
            assert ws_doc["status"] == "allocated", (
                f"Workspace {ws_id} should be ALLOCATED before fail, got {ws_doc['status']}"
            )

        # Fail the generation
        await generation_session_service.fail_generation_session(est_id, "simulated failure")

        # INVARIANT: workspaces must remain ALLOCATED after fail
        # (this assertion FAILS on current code — documents the known bug)
        for ws_id in workspace_ids:
            ws_doc = mock_db.get("workspaces", ws_id)
            assert ws_doc["status"] == "allocated", (
                f"INVARIANT VIOLATED: Workspace {ws_id} status is '{ws_doc['status']}' "
                f"after fail_generation_session. Expected 'allocated'. "
                f"Generated code on the filesystem may have been wiped."
            )
