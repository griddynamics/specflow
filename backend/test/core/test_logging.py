"""
Tests for app.core.logging.

Coverage:
- SmartFormatter caches agent formatters per workspace (no per-call allocation)
- configure_cleanup_logging closes the previous FileHandler on repeated calls
- configure_cleanup_logging leaves exactly one handler per cleanup logger (no accumulation)
- configure_cleanup_logging creates the log file under cleanup_logs/
- configure_logging also sets up cleanup logging (no separate call required)
- configure_logging does not fail if cleanup logging setup raises
- importing notifications.py does not replace the root logger's FileHandler
"""

import logging

import pytest

import app.core.logging as log_mod
from app.core.logging import (
    CLEANUP_LOGS_DIR_NAME,
    SmartFormatter,
    _CLEANUP_LOGGER_NAMES,
    configure_cleanup_logging,
)


def _teardown_logging_state():
    """Close and unregister module-level logging state set by configure_cleanup_logging."""
    if log_mod._cleanup_handler is not None:
        for name in _CLEANUP_LOGGER_NAMES:
            logging.getLogger(name).removeHandler(log_mod._cleanup_handler)
        log_mod._cleanup_handler.close()
        log_mod._cleanup_handler = None
    for name in _CLEANUP_LOGGER_NAMES:
        logging.getLogger(name).handlers.clear()
    SmartFormatter._agent_formatters.clear()


@pytest.fixture(autouse=True)
def _reset_logging_state():
    """
    Bracket each test with a clean logging state.

    configure_cleanup_logging mutates _cleanup_handler and appends to named
    loggers.  Since configure_logging now also calls configure_cleanup_logging,
    any test in the full suite that calls configure_logging will leave
    _cleanup_handler set.  Resetting both before AND after the test prevents
    cross-module bleed.
    """
    _teardown_logging_state()   # pre-test: clear state left by other modules
    yield
    _teardown_logging_state()   # post-test: restore for next test


class TestSmartFormatter:
    """SmartFormatter workspace-formatter caching."""

    def _make_record(self, logger_name: str) -> logging.LogRecord:
        return logging.LogRecord(
            name=logger_name,
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )

    def test_agent_logger_includes_workspace_prefix(self):
        formatter = SmartFormatter()
        record = self._make_record("agent.ws-001")
        result = formatter.format(record)
        assert result.startswith("[ws-001]")

    def test_non_agent_logger_has_no_workspace_prefix(self):
        formatter = SmartFormatter()
        record = self._make_record("api.main")
        result = formatter.format(record)
        assert not result.startswith("[")

    def test_agent_formatter_is_cached_not_reallocated(self):
        """The same Formatter object is returned on the second call for the same workspace."""
        formatter = SmartFormatter()
        record = self._make_record("agent.ws-cache")

        formatter.format(record)
        first_instance = SmartFormatter._agent_formatters["ws-cache"]

        formatter.format(record)
        second_instance = SmartFormatter._agent_formatters["ws-cache"]

        assert first_instance is second_instance

    def test_different_workspaces_get_separate_formatters(self):
        formatter = SmartFormatter()
        formatter.format(self._make_record("agent.ws-a"))
        formatter.format(self._make_record("agent.ws-b"))

        assert "ws-a" in SmartFormatter._agent_formatters
        assert "ws-b" in SmartFormatter._agent_formatters
        assert SmartFormatter._agent_formatters["ws-a"] is not SmartFormatter._agent_formatters["ws-b"]


class TestConfigureCleanupLogging:
    """configure_cleanup_logging handler lifecycle."""

    def test_attaches_handler_to_all_cleanup_loggers(self, tmp_path):
        configure_cleanup_logging(str(tmp_path))

        for name in _CLEANUP_LOGGER_NAMES:
            logger = logging.getLogger(name)
            assert any(
                isinstance(h, logging.FileHandler) for h in logger.handlers
            ), f"No FileHandler on logger '{name}'"

    def test_creates_log_file_under_cleanup_logs_dir(self, tmp_path):
        configure_cleanup_logging(str(tmp_path))

        cleanup_dir = tmp_path / "main" / CLEANUP_LOGS_DIR_NAME
        log_files = list(cleanup_dir.glob("*_cleanup.log"))
        assert len(log_files) == 1

    def test_repeated_call_closes_previous_handler(self, tmp_path):
        """The old FileHandler's stream must be closed — not just removed — on replacement.

        Python's FileHandler.close() sets self.stream = None after closing, so we
        capture the underlying stream reference before the second call and check it.
        """
        configure_cleanup_logging(str(tmp_path))
        first_handler = log_mod._cleanup_handler
        assert first_handler is not None
        first_stream = first_handler.stream  # capture before close() nulls it

        configure_cleanup_logging(str(tmp_path))

        assert first_stream.closed, (
            "Old FileHandler stream not closed — file descriptor was leaked"
        )

    def test_repeated_call_leaves_exactly_one_handler_per_logger(self, tmp_path):
        """Calling twice must not accumulate duplicate handlers."""
        configure_cleanup_logging(str(tmp_path))
        configure_cleanup_logging(str(tmp_path))

        for name in _CLEANUP_LOGGER_NAMES:
            handlers = [
                h for h in logging.getLogger(name).handlers
                if isinstance(h, logging.FileHandler)
            ]
            assert len(handlers) == 1, (
                f"Logger '{name}' has {len(handlers)} FileHandlers after two calls "
                f"(expected 1)"
            )

    def test_second_call_installs_a_new_handler(self, tmp_path):
        """After a second call the installed handler is a different object."""
        configure_cleanup_logging(str(tmp_path))
        first_handler = log_mod._cleanup_handler

        configure_cleanup_logging(str(tmp_path))

        assert log_mod._cleanup_handler is not first_handler


class TestConfigureLoggingIncludesCleanup:
    """configure_logging must set up cleanup logging without a separate call."""

    def test_configure_logging_attaches_cleanup_handler(self, tmp_path):
        """A single configure_logging call is sufficient to attach cleanup handlers."""
        log_mod.configure_logging(logging.DEBUG, str(tmp_path))

        for name in _CLEANUP_LOGGER_NAMES:
            assert any(
                isinstance(h, logging.FileHandler)
                for h in logging.getLogger(name).handlers
            ), f"configure_logging did not attach a FileHandler to logger '{name}'"

    def test_configure_logging_creates_cleanup_log_file(self, tmp_path):
        """configure_logging creates the cleanup log file alongside the backend log."""
        log_mod.configure_logging(logging.DEBUG, str(tmp_path))

        cleanup_dir = tmp_path / "main" / CLEANUP_LOGS_DIR_NAME
        assert any(cleanup_dir.glob("*_cleanup.log")), (
            "No cleanup log file created under cleanup_logs/"
        )

    def test_configure_logging_succeeds_when_cleanup_logging_fails(self, tmp_path, monkeypatch):
        """Failure in cleanup logging setup must not prevent the main log from being configured."""
        from unittest.mock import patch

        with patch("app.core.logging.configure_cleanup_logging", side_effect=OSError("disk full")):
            log_mod.configure_logging(logging.DEBUG, str(tmp_path))  # must not raise

        root_handlers = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
        assert len(root_handlers) == 1, "Root FileHandler must be set up even when cleanup logging fails"


class TestNotificationsDoNotReconfigureLogging:
    """Importing notifications must not replace the root logger's FileHandler."""

    def test_importing_notifications_preserves_root_file_handler(self, tmp_path):
        """
        configure_logging installs a FileHandler on the root logger.
        Importing app.core.notifications must leave that exact handler in place —
        it must not call configure_logging() again and swap it for a new file.
        """
        # Install a known FileHandler on the root logger.
        log_mod.configure_logging(logging.DEBUG, str(tmp_path))
        root_logger = logging.getLogger()
        file_handlers_before = [
            h for h in root_logger.handlers if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers_before) == 1
        original_handler = file_handlers_before[0]
        original_stream_name = original_handler.stream.name

        # Force re-execution of notifications module top-level code by reloading it.
        import importlib
        import app.core.notifications as notif_mod
        importlib.reload(notif_mod)

        file_handlers_after = [
            h for h in root_logger.handlers if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers_after) == 1
        assert file_handlers_after[0].stream.name == original_stream_name, (
            "notifications.py replaced the root FileHandler — "
            "notification logs now go to a different file than the main server log"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
