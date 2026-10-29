"""``Contain``: one audited boundary that keeps an unexpected failure from escaping.

Activities, the summarizer workflow, the diagnostics thread and the worker each
have a boundary where *any* ``Exception`` must be turned into a fixed outcome:
a sanitized ``ApplicationError`` (the Temporal SDK stores and logs the message
of whatever leaves an activity, and database or Google errors can carry
statement parameters, file names or document text), a logged warning, or a
default value. ``Contain`` is that boundary, written once:

- an ``Exception`` raised in the block is kept on ``.error`` and the block
  ends; the caller decides what to log (the type only) and what to raise;
- ``BaseException`` (cancellation, sandbox eviction, ``KeyboardInterrupt``,
  ``SystemExit``) is never contained, and neither are the ``passthrough``
  types (for example an already-sanitized ``ApplicationError``);
- the caller raises its fixed error *after* the block and still ``from None``:
  Temporal's failure converter follows ``__context__`` unless
  ``__suppress_context__`` is set, and an exception already in flight (for
  example the one ``contextlib`` throws into a generator) would otherwise
  become that context.

Typical use::

    with Contain(ApplicationError) as contained:
        result = await work()
    if contained.error is not None:
        log_event(logger, ERROR, "x.failed", error_type=contained.error_type)
        raise ApplicationError("internal_error", type="XError") from None
"""

from __future__ import annotations

from types import TracebackType
from typing import Self


class Contain:
    """Context manager: an ``Exception`` leaving the block is kept on ``error``."""

    __slots__ = ("_passthrough", "error")

    def __init__(self, *passthrough: type[Exception]) -> None:
        self._passthrough = passthrough
        self.error: Exception | None = None

    @property
    def error_type(self) -> str | None:
        """The contained error's class name (never its message), or ``None``."""

        return type(self.error).__name__ if self.error is not None else None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if not isinstance(exc, Exception) or isinstance(exc, self._passthrough):
            return False
        self.error = exc
        return True
