import json
import logging
import sys
import queue
import atexit
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from logging.handlers import QueueHandler, QueueListener

from app.core.config import settings

MAIN_LOGS_DIR_NAME = "main_logs"
CLEANUP_LOGS_DIR_NAME = "cleanup_logs"

_STANDARD_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
_DEFAULT_FORMATTER = logging.Formatter(_STANDARD_FORMAT)
_CLEANUP_LOGGER_NAMES = ("app.jobs", "app.core.background_jobs")

# Global queue and listener for thread-safe logging
_log_queue: Optional[queue.Queue] = None
_log_listener: Optional[QueueListener] = None
_cleanup_handler: Optional[logging.FileHandler] = None


class SmartFormatter(logging.Formatter):
    """
    Formatter that applies workspace-specific formatting for agent loggers
    and default formatting for other loggers.
    """
    _agent_formatters: dict[str, logging.Formatter] = {}

    def format(self, record: logging.LogRecord) -> str:
        if record.name.startswith("agent."):
            workspace_name = record.name.split(".", 1)[1]
            if workspace_name not in self._agent_formatters:
                self._agent_formatters[workspace_name] = logging.Formatter(
                    f'[{workspace_name}] {_STANDARD_FORMAT}'
                )
            return self._agent_formatters[workspace_name].format(record)
        return _DEFAULT_FORMATTER.format(record)


def _resolve_log_base_dir(log_base_dir: Optional[str]) -> str:
    return log_base_dir if log_base_dir is not None else settings.AGENT_LOGS_BASE_PATH


def _make_file_handler(
    log_dir: Path,
    filename: str,
    formatter: Optional[logging.Formatter] = None,
) -> logging.FileHandler:
    """Create a FileHandler, making the directory if needed."""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / filename)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter or _DEFAULT_FORMATTER)
    return handler


def _setup_queue_logging():
    """
    Set up the global queue-based logging infrastructure.
    This should be called once at application startup.
    """
    global _log_queue, _log_listener

    if _log_queue is not None:
        return

    _log_queue = queue.Queue(-1)  # Unlimited size

    # Set level to WARNING to reduce noise in K8s logs
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(SmartFormatter())

    _log_listener = QueueListener(_log_queue, stream_handler, respect_handler_level=True)
    _log_listener.start()

    atexit.register(_cleanup_queue_logging)


def _cleanup_queue_logging():
    """Stop the queue listener and flush remaining records."""
    global _log_listener
    if _log_listener is not None:
        _log_listener.stop()
        _log_listener = None


def configure_logging(log_level: str = logging.INFO, log_base_dir: Optional[str] = None, generation_id: Optional[str] = None):
    """
    Configure logging for the application.

    Uses QueueHandler/QueueListener pattern to prevent blocking I/O errors
    in multi-threaded/async environments.

    Args:
        log_level: Logging level (default: INFO)
        log_base_dir: Base directory for log files (defaults to settings.AGENT_LOGS_BASE_PATH)
        generation_id: Optional generation ID to partition logs by generation
    """
    global _log_queue

    _setup_queue_logging()

    prefix = generation_id if generation_id else "main"
    log_dir = Path(_resolve_log_base_dir(log_base_dir)) / prefix / MAIN_LOGS_DIR_NAME
    timestamp = datetime.now().strftime(_TIMESTAMP_FORMAT)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # QueueHandler for non-blocking stdout (via listener's StreamHandler)
    root_logger.addHandler(QueueHandler(_log_queue))
    # FileHandler direct — file I/O is thread-safe; captures everything at DEBUG
    root_logger.addHandler(_make_file_handler(log_dir, f"{timestamp}_backend.log"))

    # Suppress verbose third-party library logs to reduce K8s log noise
    for name in (
        "uvicorn.access", "fastapi", "claude_agent_sdk", "anthropic",
        "httpx", "google.cloud", "google.auth", "posthog", "urllib3", "httpcore", "langfuse",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Attach dedicated cleanup log file — optional, failure must not block app startup
    try:
        configure_cleanup_logging(log_base_dir)
    except Exception:
        logging.getLogger(__name__).warning(
            "configure_cleanup_logging failed — background job logs will only appear in main_logs/",
            exc_info=True,
        )


def configure_cleanup_logging(log_base_dir: Optional[str] = None):
    """
    Attach a dedicated FileHandler for background job logs.

    Background job loggers (app.jobs.* and app.core.background_jobs) still
    propagate to root so their output remains in main_logs/ as well — this
    handler adds a parallel write to cleanup_logs/ for easier browsing.

    Safe to call multiple times: replaces the previous handler (mirrors the
    remove-before-add pattern used by configure_logging and create_agent_logger).
    """
    global _cleanup_handler

    loggers = [logging.getLogger(name) for name in _CLEANUP_LOGGER_NAMES]

    # Remove and close the previously installed handler before adding a new one.
    # Closing is required to release the file descriptor; removing alone leaks it.
    if _cleanup_handler is not None:
        for logger in loggers:
            logger.removeHandler(_cleanup_handler)
        _cleanup_handler.close()

    log_dir = Path(_resolve_log_base_dir(log_base_dir)) / "main" / CLEANUP_LOGS_DIR_NAME
    timestamp = datetime.now().strftime(_TIMESTAMP_FORMAT)
    _cleanup_handler = _make_file_handler(log_dir, f"{timestamp}_cleanup.log")

    for logger in loggers:
        logger.addHandler(_cleanup_handler)


def create_agent_logger(workspace_name: str, log_base_dir: Optional[str] = None, generation_id: Optional[str] = None) -> logging.Logger:
    """
    Create a logger for an agent with both file and stream handlers.

    Uses QueueHandler for stream output to prevent blocking I/O errors.

    Args:
        workspace_name: Name of the workspace (used for logger name and directory)
        log_base_dir: Base directory for log files (defaults to settings.AGENT_LOGS_BASE_PATH)
        generation_id: Optional generation ID to partition logs by generation

    Returns:
        Configured logger instance
    """
    global _log_queue

    _setup_queue_logging()

    prefix = generation_id if generation_id else "main"
    log_dir = Path(_resolve_log_base_dir(log_base_dir)) / prefix / workspace_name
    timestamp = datetime.now().strftime(_TIMESTAMP_FORMAT)

    formatter = logging.Formatter(f'[{workspace_name}] {_STANDARD_FORMAT}')
    agent_logger = logging.getLogger(f"agent.{workspace_name}")
    agent_logger.setLevel(logging.DEBUG)

    for handler in agent_logger.handlers[:]:
        agent_logger.removeHandler(handler)
        handler.close()

    agent_logger.addHandler(_make_file_handler(log_dir, f"{timestamp}.log", formatter))
    # QueueHandler routes stdout output through the global listener (non-blocking)
    agent_logger.addHandler(QueueHandler(_log_queue))
    agent_logger.propagate = False

    return agent_logger


# ---------------------------------------------------------------------------
# Structured log formatting helpers
# ---------------------------------------------------------------------------

def format_json_to_log(data: dict) -> str:
    """Compact single-line JSON string safe for GCP log entries.

    Uses ``default=str`` so non-serializable values (Path, lambda, Enum, etc.)
    are coerced to their string representation instead of raising TypeError.
    No indentation is applied — each call produces exactly one log line.
    """
    return json.dumps(data, default=str)


def log_agent_options(
    options: Any,
    extra: Optional[dict] = None,
    skip_fields: Optional[list[str]] = None,
) -> str:
    """Format an agent options object as a compact JSON string for logging.

    Converts *options* to a dict (via ``__dict__``), merges any *extra* keys,
    applies skip logic, then serialises with :func:`format_json_to_log`.

    Args:
        options: An agent options instance (dataclass, Pydantic model, or any
            object that exposes its fields through ``__dict__``).
        extra: Additional key/value pairs merged into the dict after conversion.
            These override same-named fields from *options*.
        skip_fields: Fields to omit.  ``None`` defaults to
            ``["allowed_tools", "system_prompt", "disallowed_tools", "env", "stderr"]``.

            Special behaviour for ``"allowed_tools"`` and ``"disallowed_tools"``
            when they appear in *skip_fields*: items whose string representation
            starts with ``"mcp"`` are **kept** in a filtered list; all others are
            dropped.  If no MCP items remain the key is removed entirely.
            This lets MCP server tool names survive the skip for diagnostics.

            Removal is always best-effort (missing keys are silently ignored).

    Returns:
        A compact single-line JSON string for use as the sole argument to
        ``logger.info("%s", log_agent_options(...))``.
    """
    if skip_fields is None:
        skip_fields = ["allowed_tools", "system_prompt", "disallowed_tools", "env", "stderr"]

    try:
        data: dict = vars(options).copy()
    except TypeError:
        data = {}

    if extra:
        data.update(extra)

    for field in skip_fields:
        if field in ("allowed_tools", "disallowed_tools"):
            raw = data.get(field)
            if raw is None:
                continue
            mcp_items = [t for t in raw if str(t).startswith("mcp")]
            if mcp_items:
                data[field] = mcp_items
            else:
                data.pop(field, None)
        else:
            data.pop(field, None)

    return format_json_to_log(data)
