"""Tests for the optional agent-activity tail (tui/activity.py)."""

from tui import activity


class TestFindLogForWorkspace:
    def test_missing_directory_returns_none(self, tmp_path):
        assert activity.find_log_for_workspace(tmp_path / "nope", "ws-01-1") is None

    def test_empty_directory_returns_none(self, tmp_path):
        d = tmp_path / "agent_logs"
        d.mkdir()
        assert activity.find_log_for_workspace(d, "ws-01-1") is None

    def test_prefers_workspace_named_file(self, tmp_path):
        d = tmp_path / "agent_logs"
        d.mkdir()
        (d / "other.log").write_text("x")
        target = d / "ws-01-1.log"
        target.write_text("y")
        assert activity.find_log_for_workspace(d, "ws-01-1") == target

    def test_falls_back_to_newest(self, tmp_path):
        import os

        d = tmp_path / "agent_logs"
        d.mkdir()
        old = d / "a.log"
        old.write_text("old")
        new = d / "b.log"
        new.write_text("new")
        os.utime(old, (1, 1))
        os.utime(new, (10**9, 10**9))
        assert activity.find_log_for_workspace(d, None) == new


class TestTail:
    def test_returns_last_nonempty_lines(self, tmp_path):
        f = tmp_path / "log.txt"
        f.write_text("a\n\nb\nc\n\n")
        assert activity.tail(f, lines=2) == ["b", "c"]

    def test_missing_file_returns_empty(self, tmp_path):
        assert activity.tail(tmp_path / "nope.txt") == []


class TestRecentActivity:
    def test_unavailable_returns_empty(self, tmp_path):
        assert activity.recent_activity(tmp_path, "ws-01-1") == []

    def test_reads_log_when_present(self, tmp_path):
        d = tmp_path / "agent_logs"
        d.mkdir()
        (d / "ws-01-1.log").write_text("line1\nline2\n")
        assert activity.recent_activity(tmp_path, "ws-01-1") == ["line1", "line2"]
