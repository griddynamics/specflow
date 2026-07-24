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


class TestNestedLogDiscovery:
    """Logs live nested (agent_logs/<gen>/<ws>-phase<N>/<ts>.log) — see backend logging."""

    def test_finds_nested_workspace_log(self, tmp_path):
        d = tmp_path / "agent_logs"
        nested = d / "est-abc123" / "ws-01-1-phase12"
        nested.mkdir(parents=True)
        target = nested / "2026-07-23_10-00-00.log"
        target.write_text("phase 12 log line")
        assert activity.find_log_for_workspace(d, "ws-01-1") == target

    def test_ignores_non_log_files_like_ds_store(self, tmp_path):
        """The regression from the dashboard screenshot: binary .DS_Store rendered as garbage."""
        import os

        d = tmp_path / "agent_logs"
        nested = d / "est-abc123" / "ws-01-1-phase12"
        nested.mkdir(parents=True)
        log = nested / "run.log"
        log.write_text("real log")
        junk = d / ".DS_Store"
        junk.write_bytes(b"\x00\x01Bud1\x00")
        os.utime(log, (1, 1))
        os.utime(junk, (10**9, 10**9))  # junk is newest — must still be ignored
        assert activity.find_log_for_workspace(d, None) == log
        assert activity.find_log_for_workspace(d, "ws-01-1") == log

    def test_picks_newest_log_across_generations(self, tmp_path):
        import os

        d = tmp_path / "agent_logs"
        old_dir = d / "est-old" / "ws-01-1-phase3"
        new_dir = d / "est-new" / "ws-01-1-phase5"
        old_dir.mkdir(parents=True)
        new_dir.mkdir(parents=True)
        old = old_dir / "old.log"
        old.write_text("old")
        new = new_dir / "new.log"
        new.write_text("new")
        os.utime(old, (1, 1))
        os.utime(new, (10**9, 10**9))
        assert activity.find_log_for_workspace(d, "ws-01-1") == new
