from __future__ import annotations

from types import TracebackType


class CapturedException:
    """Context manager that stores an ``Exception`` raised in its block and swallows it.

    ``BaseException`` subclasses that are not ``Exception`` (``CancelledError``,
    ``KeyboardInterrupt``) are not captured and propagate.
    """

    def __init__(self) -> None:
        self.error: Exception | None = None

    def __enter__(self) -> CapturedException:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if isinstance(exc, Exception):
            self.error = exc
            return True
        return False
