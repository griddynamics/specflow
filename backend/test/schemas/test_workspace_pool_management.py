"""Tests for the reclaim classifier.

This is the single rule the listing badge and the reclaim dispatch both read, so the
matrix here is the contract: which workspace states can be reclaimed, what reclaiming
would do, and when an operator is about to end a still-retryable run.
"""

import pytest

from app.schemas.generation_workflow_enums import GenerationStatus
from app.schemas.workspace_pool_management import (
    ReclaimAction,
    classify_reclaim,
    classify_removal,
)


def _ws(status: str, **overrides) -> dict:
    doc = {"status": status, "clean_verified": True, "locked_by": None}
    doc.update(overrides)
    return doc


class TestNonAllocatedStates:
    """CLEANING / STUCK / AVAILABLE never depend on a generation."""

    def test_cleaning_finishes_cleaning(self):
        plan = classify_reclaim(_ws("cleaning", clean_verified=False))
        assert plan.action is ReclaimAction.FINISH_CLEANING
        assert plan.reclaimable is True
        assert plan.retry_lost is False

    def test_stuck_releases_stuck(self):
        plan = classify_reclaim(_ws("stuck", clean_verified=False))
        assert plan.action is ReclaimAction.RELEASE_STUCK
        assert plan.reclaimable is True

    def test_available_and_verified_is_a_no_op(self):
        plan = classify_reclaim(_ws("available", clean_verified=True))
        assert plan.action is ReclaimAction.ALREADY_AVAILABLE
        # Nothing to do, so it must not be offered as a reclaim target.
        assert plan.reclaimable is False

    def test_available_but_unverified_needs_force_clean(self):
        plan = classify_reclaim(_ws("available", clean_verified=False))
        assert plan.action is ReclaimAction.FORCE_CLEAN
        assert plan.reclaimable is True

    def test_unrecognised_status_is_blocked_not_assumed_safe(self):
        plan = classify_reclaim(_ws("something-new"))
        assert plan.action is ReclaimAction.BLOCKED
        assert plan.reclaimable is False
        assert "something-new" in plan.blocked_reason


class TestAllocated:
    """ALLOCATED hinges entirely on whether the owning generation is still running."""

    @pytest.mark.parametrize(
        "owner_status",
        [
            GenerationStatus.PENDING.value,
            GenerationStatus.INITIALIZING.value,
            GenerationStatus.RUNNING.value,
        ],
    )
    def test_live_owner_blocks(self, owner_status):
        plan = classify_reclaim(
            _ws("allocated", clean_verified=False, locked_by="gen_live"), owner_status
        )
        assert plan.action is ReclaimAction.BLOCKED
        assert plan.reclaimable is False
        # The message must name the generation and its status — that is what the operator reads.
        assert "gen_live" in plan.blocked_reason
        assert owner_status in plan.blocked_reason

    @pytest.mark.parametrize(
        "owner_status",
        [
            GenerationStatus.COMPLETED.value,
            GenerationStatus.FAILED.value,
            GenerationStatus.CANCELLED.value,
        ],
    )
    def test_terminal_owner_allows_release_and_clean(self, owner_status):
        plan = classify_reclaim(
            _ws("allocated", clean_verified=False, locked_by="gen_done"),
            owner_status,
            owner_code_archived=True,
        )
        assert plan.action is ReclaimAction.RELEASE_AND_CLEAN
        assert plan.reclaimable is True
        assert plan.blocked_reason is None

    def test_missing_owner_document_is_reclaimable(self):
        """A vanished generation cannot still be running, so the set must not stay stranded."""
        plan = classify_reclaim(
            _ws("allocated", clean_verified=False, locked_by="gen_gone"), owner_status=None
        )
        assert plan.action is ReclaimAction.RELEASE_AND_CLEAN

    def test_no_lock_owner_is_reclaimable(self):
        plan = classify_reclaim(_ws("allocated", clean_verified=False, locked_by=None))
        assert plan.action is ReclaimAction.RELEASE_AND_CLEAN


class TestRetryLostWarning:
    """retry_lost drives the operator warning; it must be exact, not merely cautious."""

    def test_failed_and_unarchived_loses_retry(self):
        plan = classify_reclaim(
            _ws("allocated", clean_verified=False, locked_by="gen_f"),
            GenerationStatus.FAILED.value,
            owner_code_archived=False,
        )
        assert plan.action is ReclaimAction.RELEASE_AND_CLEAN
        assert plan.retry_lost is True

    def test_failed_but_archived_does_not_lose_retry(self):
        plan = classify_reclaim(
            _ws("allocated", clean_verified=False, locked_by="gen_f"),
            GenerationStatus.FAILED.value,
            owner_code_archived=True,
        )
        assert plan.retry_lost is False

    @pytest.mark.parametrize(
        "owner_status",
        [GenerationStatus.CANCELLED.value, GenerationStatus.COMPLETED.value],
    )
    def test_cancelled_and_completed_never_lose_retry(self, owner_status):
        """Neither is retryable in the first place, so there is nothing to warn about."""
        plan = classify_reclaim(
            _ws("allocated", clean_verified=False, locked_by="gen_x"),
            owner_status,
            owner_code_archived=False,
        )
        assert plan.retry_lost is False


class TestTerminalStatusSSOT:
    """GenerationStatus.is_terminal is the shared definition; guard its edges."""

    def test_terminal_set(self):
        assert GenerationStatus.terminal() == frozenset(
            {GenerationStatus.COMPLETED, GenerationStatus.FAILED, GenerationStatus.CANCELLED}
        )

    def test_accepts_raw_strings_and_enum_members(self):
        assert GenerationStatus.is_terminal("failed") is True
        assert GenerationStatus.is_terminal(GenerationStatus.FAILED) is True
        assert GenerationStatus.is_terminal("running") is False

    def test_unknown_status_is_not_terminal(self):
        """An unrecognised status must never read as 'safe to reclaim'."""
        assert GenerationStatus.is_terminal("bogus") is False
        assert GenerationStatus.is_terminal(None) is False


class TestClassifyRemoval:
    """Shrink eligibility — one rule, read by both the listing and the shrink service."""

    def test_clean_available_is_removable(self):
        check = classify_removal({"id": "ws-01-1", "status": "available", "clean_verified": True})
        assert check.removable is True
        assert check.reason is None

    def test_unverified_available_is_refused(self):
        check = classify_removal({"id": "ws-01-1", "status": "available", "clean_verified": False})
        assert check.removable is False
        assert check.reason == "ws-01-1 is not clean-verified"

    @pytest.mark.parametrize("state", ["allocated", "cleaning", "stuck"])
    def test_busy_states_are_refused_and_named(self, state):
        check = classify_removal({"id": "ws-01-1", "status": state, "clean_verified": False})
        assert check.removable is False
        assert check.reason == f"ws-01-1 is {state}"

    def test_unrecognised_status_is_refused(self):
        check = classify_removal({"id": "ws-01-1", "status": "weird", "clean_verified": True})
        assert check.removable is False
        assert "unrecognised status" in check.reason

    def test_accepts_either_id_key(self):
        """Query results carry 'id'/'_id'; listing rows carry 'workspace_id'."""
        for key in ("id", "_id", "workspace_id"):
            check = classify_removal({key: "ws-01-1", "status": "allocated"})
            assert check.reason == "ws-01-1 is allocated"
