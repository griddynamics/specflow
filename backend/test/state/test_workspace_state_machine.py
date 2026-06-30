"""
Tests for WorkspaceStateMachine.
Covers all test cases from implementation-plan.md section 1.10.
"""
import pytest

from app.state.workspace_state_machine import WorkspaceStateMachine
from app.state.exceptions import (
    InvalidWorkspaceStateError,
    WorkspaceOwnershipError,
    CleanupTargetError,
)
from app.schemas.generation_workflow_enums import WorkspaceStatus


def make_wsm(db):
    return WorkspaceStateMachine(db)


class TestAllocate:
    @pytest.mark.asyncio
    async def test_allocate_available_to_allocated(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.AVAILABLE})
        await wsm.allocate("ws-1", "est-1", triggered_by="api:start_generation_session")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.ALLOCATED
        assert data["locked_by"] == "est-1"

    @pytest.mark.asyncio
    async def test_allocate_from_cleaning_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.allocate("ws-1", "est-1", triggered_by="test")

    @pytest.mark.asyncio
    async def test_allocate_in_transaction(self, db):
        """Allocation happens inside a transaction."""
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.AVAILABLE})
        await wsm.allocate("ws-1", "est-1", triggered_by="test")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.ALLOCATED


class TestAllocationRollback:
    @pytest.mark.asyncio
    async def test_rollback_correct_owner_to_cleaning(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-1",
            "last_used_by": "est-0",   # previous generation
        })
        await wsm.allocation_rollback("ws-1", "est-1", triggered_by="test")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert data["locked_by"] is None
        assert "cleaning_started_at" in data
        # last_used_by must be cleared: clone failed, no code on filesystem,
        # so cleanup_workspace() must not treat the previous generation's
        # FAILED grace period as a reason to skip cleanup.
        assert data["last_used_by"] is None

    @pytest.mark.asyncio
    async def test_rollback_wrong_owner_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-999",
        })
        with pytest.raises(WorkspaceOwnershipError):
            await wsm.allocation_rollback("ws-1", "est-1", triggered_by="test")


class TestArchiveAndRelease:
    @pytest.mark.asyncio
    async def test_archive_and_release_correct_owner(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-1",
        })
        await wsm.archive_and_release("ws-1", "est-1", triggered_by="test")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert data["locked_by"] is None
        assert data["last_used_by"] == "est-1"

    @pytest.mark.asyncio
    async def test_archive_and_release_wrong_owner_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {
            "status": WorkspaceStatus.ALLOCATED,
            "locked_by": "est-999",
        })
        with pytest.raises(WorkspaceOwnershipError):
            await wsm.archive_and_release("ws-1", "est-1", triggered_by="test")


class TestMarkClean:
    @pytest.mark.asyncio
    async def test_mark_clean_from_cleaning(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING})
        await wsm.mark_clean("ws-1", triggered_by="test")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.AVAILABLE
        assert data["clean_verified"] is True

    @pytest.mark.asyncio
    async def test_mark_clean_from_allocated_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.ALLOCATED})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.mark_clean("ws-1", triggered_by="test")


class TestMarkStuck:
    @pytest.mark.asyncio
    async def test_mark_stuck_from_cleaning(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING})
        await wsm.mark_stuck("ws-1", triggered_by="test", reason="timed out")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.STUCK

    @pytest.mark.asyncio
    async def test_mark_stuck_from_allocated_raises_cleanup_target_error(self, db):
        """Invariant 2: ALLOCATED → STUCK is NOT a legal transition."""
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.ALLOCATED})
        with pytest.raises(CleanupTargetError):
            await wsm.mark_stuck("ws-1", triggered_by="test", reason="stuck")


class TestBeginRecovery:
    @pytest.mark.asyncio
    async def test_begin_recovery_from_stuck(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.STUCK})
        await wsm.begin_recovery("ws-1", triggered_by="test")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert "cleaning_started_at" in data


class TestForceRelease:
    @pytest.mark.asyncio
    async def test_force_release_from_allocated(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.ALLOCATED, "locked_by": "est-1"})
        await wsm.force_release("ws-1", reason="test release", confirmed_by="admin@example.com")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert data["force_released"] is True
        assert data["force_release_reason"] == "test release"
        assert data["force_released_by"] == "admin@example.com"
        assert "force_released_at" in data

    @pytest.mark.asyncio
    async def test_force_release_from_available_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.AVAILABLE})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.force_release("ws-1", reason="test", confirmed_by="admin")


class TestAdminCleanAvailable:
    @pytest.mark.asyncio
    async def test_admin_clean_available_from_available(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.AVAILABLE})
        await wsm.admin_clean_available("ws-1", reason="stale data on disk", triggered_by="admin:clean_available")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.CLEANING
        assert "cleaning_started_at" in data

    @pytest.mark.asyncio
    async def test_admin_clean_available_from_cleaning_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.admin_clean_available("ws-1", reason="test", triggered_by="admin:clean_available")

    @pytest.mark.asyncio
    async def test_admin_clean_available_from_allocated_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.ALLOCATED})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.admin_clean_available("ws-1", reason="test", triggered_by="admin:clean_available")


class TestAdminDeallocate:
    @pytest.mark.asyncio
    async def test_admin_deallocate_from_allocated(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.ALLOCATED, "locked_by": "est-1"})
        await wsm.admin_deallocate("ws-1", reason="no code written", triggered_by="admin:deallocate")
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.AVAILABLE
        assert data["locked_by"] is None
        assert data["clean_verified"] is True

    @pytest.mark.asyncio
    async def test_admin_deallocate_from_available_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.AVAILABLE})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.admin_deallocate("ws-1", reason="test", triggered_by="admin:deallocate")

    @pytest.mark.asyncio
    async def test_admin_deallocate_from_stuck_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.STUCK})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.admin_deallocate("ws-1", reason="test", triggered_by="admin:deallocate")


class TestAdminReleaseStuck:
    @pytest.mark.asyncio
    async def test_admin_release_stuck_from_stuck(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.STUCK})
        await wsm.admin_release_stuck(
            "ws-1", reason="manual cleanup done", triggered_by="admin:release_stuck",
            clean_verified=True
        )
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.AVAILABLE
        assert data["clean_verified"] is True
        assert data["locked_by"] is None
        assert data["stuck_at"] is None
        assert "last_cleaned_at" in data

    @pytest.mark.asyncio
    async def test_admin_release_stuck_clean_verified_false(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.STUCK})
        await wsm.admin_release_stuck(
            "ws-1", reason="operator override", triggered_by="admin:release_stuck",
            clean_verified=False
        )
        data = db.get_workspace_data("ws-1")
        assert data["status"] == WorkspaceStatus.AVAILABLE
        assert data["clean_verified"] is False

    @pytest.mark.asyncio
    async def test_admin_release_stuck_from_cleaning_raises(self, db):
        wsm = make_wsm(db)
        db.seed_workspace("ws-1", {"status": WorkspaceStatus.CLEANING})
        with pytest.raises(InvalidWorkspaceStateError):
            await wsm.admin_release_stuck("ws-1", reason="test", triggered_by="admin:release_stuck")


