"""``Contained``: the boundary of a best-effort step that must never fail its caller.

Some steps of the API are isolated on purpose: priming the storage client at startup,
starting a parse after the upload committed, deleting an unreferenced object, the
compensation of a failed import start. Whatever they raise, the request (or the start)
keeps its answer, and only the event and the exception CLASS are logged: the text of an
error can hold document, driver or Google text and never reaches a log.

``with Contained() as step:`` stops an ``Exception`` raised in its body and keeps it in
``step.error``; the code after the block reads ``step.failed`` / ``step.error_type``.
``BaseException`` (cancellation, ``KeyboardInterrupt``, ``SystemExit``) always passes, as
does any type listed in ``passing`` (for example ``ApiError``, which is its own answer).
"""

from __future__ import annotations

from types import TracebackType
from typing import Self


class Contained:
    """A context manager that contains (and records) the failure of its body."""

    def __init__(self, *, passing: tuple[type[Exception], ...] = ()) -> None:
        self.error: Exception | None = None
        self._passing = passing

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        if isinstance(exc, Exception) and not isinstance(exc, self._passing):
            self.error = exc
            return True
        return False

    @property
    def failed(self) -> bool:
        """``True`` when the body raised a contained ``Exception``."""

        return self.error is not None

    @property
    def error_type(self) -> str | None:
        """The class name of the contained error (never its text), or ``None``."""

        return type(self.error).__name__ if self.error is not None else None
