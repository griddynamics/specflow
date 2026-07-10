"""Unit tests for the shared workspace-pool seeding module.

Covers the single source of truth now used by both init_db.py (file-based seeding) and
create_generation_session_repos.py (direct-to-DB seeding):
  - WorkspacePoolEntry validation
  - parse_pool_entries (dict -> typed, with indexed errors)
  - assign_pool_entries (ordered + prefix id assignment, set_number consistency)
  - build_workspace_document (full schema incl. cleaning_started_at)
  - seed_workspace_pool (idempotent upsert, replace semantics, dry-run, counts)
"""

from datetime import datetime, timezone

import pytest

from app.database.memory import InMemoryDatabase
from app.services.workspace_pool_seeding import (
    WORKSPACES_COLLECTION,
    SeedResult,
    WorkspacePoolEntry,
    assign_pool_entries,
    build_workspace_document,
    parse_pool_entries,
    seed_workspace_pool,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entry(ws_id="ws-01-1", pool="default", p10y=111, set_number=1):
    return WorkspacePoolEntry(
        workspace_id=ws_id,
        repo_url=f"https://github.com/org/{ws_id}",
        p10y_repository_id=p10y,
        workspace_pool=pool,
        set_number=set_number,
    )


class TestWorkspacePoolEntry:
    def test_rejects_bool_p10y(self):
        with pytest.raises(ValueError):
            WorkspacePoolEntry(
                workspace_id="x", repo_url="y", p10y_repository_id=True, workspace_pool="default"
            )

    def test_rejects_non_int_p10y(self):
        with pytest.raises(ValueError):
            WorkspacePoolEntry(
                workspace_id="x", repo_url="y", p10y_repository_id="12", workspace_pool="default"
            )

    def test_set_number_optional(self):
        e = WorkspacePoolEntry(
            workspace_id="x", repo_url="y", p10y_repository_id=1, workspace_pool="default"
        )
        assert e.set_number is None

    def test_workspace_pool_required(self):
        # The file schema mandates all four fields — workspace_pool has no default.
        with pytest.raises(TypeError):
            WorkspacePoolEntry(workspace_id="x", repo_url="y", p10y_repository_id=1)


class TestParsePoolEntries:
    def test_valid(self):
        raw = [{"workspace_id": "ws-01-1", "repo_url": "u", "p10y_repository_id": 5, "workspace_pool": "default"}]
        entries = parse_pool_entries(raw)
        assert len(entries) == 1
        assert entries[0].p10y_repository_id == 5

    def test_non_dict_entry_raises_with_index(self):
        with pytest.raises(ValueError, match="Entry 0"):
            parse_pool_entries(["notadict"])

    def test_missing_field_raises_with_index(self):
        with pytest.raises(ValueError, match="Entry 0"):
            parse_pool_entries([{"workspace_id": "x", "repo_url": "y", "p10y_repository_id": 1, "extra": 9}])


class TestAssignPoolEntries:
    def test_prefix_path_derives_id_and_set_from_repo_number(self):
        # repos 4,5,6 -> set 2, indices 1,2,3
        repo_id_map = {"ws-4": 104, "ws-5": 105, "ws-6": 106}
        entries = assign_pool_entries(repo_id_map, "org", "default", prefix="ws-")
        by_id = {e.workspace_id: e for e in entries}
        assert set(by_id) == {"ws-02-1", "ws-02-2", "ws-02-3"}
        # set_number stays consistent with the workspace_id (both from the repo number).
        assert all(e.set_number == 2 for e in entries)

    def test_ordered_path_assigns_by_position(self):
        repo_id_map = {"a": 1, "b": 2, "c": 3, "d": 4}
        entries = assign_pool_entries(
            repo_id_map, "org", "default", ordered_repos=["a", "b", "c", "d"]
        )
        assert [e.workspace_id for e in entries] == ["ws-01-1", "ws-01-2", "ws-01-3", "ws-02-1"]
        assert entries[3].set_number == 2

    def test_prefix_required_without_ordered(self):
        with pytest.raises(ValueError, match="prefix is required"):
            assign_pool_entries({"x1": 1}, "org", "default")

    def test_unparseable_name_skipped(self):
        entries = assign_pool_entries({"weird-name": 1}, "org", "default", prefix="ws-")
        assert entries == []


class TestBuildWorkspaceDocument:
    def test_full_schema_including_cleaning_started_at(self):
        doc = build_workspace_document(_entry(), set_number=3, now=_NOW)
        # The field that had drifted out of one duplicate must be present.
        assert doc["cleaning_started_at"] is None
        assert doc["status"] == "available"
        assert doc["clean_verified"] is True
        assert doc["set_number"] == 3
        assert doc["last_cleaned_at"] == _NOW
        assert doc["allocation_history"] == []
        assert doc["error"] is None


class TestSeedWorkspacePool:
    def test_creates_new(self):
        db = InMemoryDatabase()
        result = seed_workspace_pool(db, [_entry("ws-01-1"), _entry("ws-01-2")], now=_NOW)
        assert result == SeedResult(created=2, updated=0, skipped=0)
        assert db.get(WORKSPACES_COLLECTION, "ws-01-1") is not None

    def test_skips_existing_without_replace(self):
        db = InMemoryDatabase()
        seed_workspace_pool(db, [_entry("ws-01-1")], now=_NOW)
        result = seed_workspace_pool(db, [_entry("ws-01-1", p10y=999)], replace=False, now=_NOW)
        assert result.skipped == 1 and result.created == 0
        assert db.get(WORKSPACES_COLLECTION, "ws-01-1")["p10y_repository_id"] == 111

    def test_replaces_existing_with_replace(self):
        db = InMemoryDatabase()
        seed_workspace_pool(db, [_entry("ws-01-1")], now=_NOW)
        result = seed_workspace_pool(db, [_entry("ws-01-1", p10y=999)], replace=True, now=_NOW)
        assert result.updated == 1
        assert db.get(WORKSPACES_COLLECTION, "ws-01-1")["p10y_repository_id"] == 999

    def test_dry_run_writes_nothing(self):
        db = InMemoryDatabase()
        result = seed_workspace_pool(db, [_entry("ws-01-1")], dry_run=True, now=_NOW)
        assert result.created == 1
        assert db.get(WORKSPACES_COLLECTION, "ws-01-1") is None

    def test_set_number_falls_back_to_position_when_absent(self):
        db = InMemoryDatabase()
        # File-loaded entries carry no set_number -> derived from position (groups of 3).
        entries = [
            WorkspacePoolEntry(
                workspace_id=f"ws-x-{i}", repo_url="u", p10y_repository_id=i, workspace_pool="default"
            )
            for i in range(4)
        ]
        seed_workspace_pool(db, entries, now=_NOW)
        assert db.get(WORKSPACES_COLLECTION, "ws-x-0")["set_number"] == 1
        assert db.get(WORKSPACES_COLLECTION, "ws-x-3")["set_number"] == 2

    def test_seed_result_total(self):
        assert SeedResult(created=1, updated=2, skipped=3).total == 6
