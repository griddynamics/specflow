"""The Drive gateway contract. Every method BLOCKS: callers offload to a thread.

No method returns or logs a Google image ``contentUri`` link, and no error
carries Google's text (names and emails can be inside it).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final, Literal, Protocol

from drive.model import DriveFolder, Listing
from drive.reasons import Reason

FAKE_DRIVE_EMAIL: Final = "fake-drive@specflow.local"
SCOPES: Final = ("https://www.googleapis.com/auth/drive.readonly",)
CHUNK_SIZE: Final = 8 * 1024 * 1024
LIST_PARENT_BATCH: Final = 20
LIST_PAGE_SIZE: Final = 1000
NUM_RETRIES: Final = 5


class DriveError(Exception):
    """A Drive failure as one closed reason; ``str(e)`` is the reason value."""

    def __init__(
        self,
        reason: Reason,
        *,
        http_status: int | None = None,
        google_reason: str | None = None,
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.http_status = http_status
        self.google_reason = google_reason


class DriveGateway(Protocol):
    @property
    def mode(self) -> Literal["real", "fake"]:
        """``real`` or ``fake``."""

    def identity(self) -> str:
        """The email Drive sees; DriveError(setup_error) without credentials."""

    def check_folder(self, folder_id: str) -> DriveFolder:
        """no_access | not_a_folder | drive_unavailable | setup_error."""

    def list_all(self, folder_id: str, *, deadline: float | None = None) -> Listing:
        """All levels; ``deadline`` is a ``time.monotonic()`` value -> too_slow."""

    def iter_download(self, file_id: str, *, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        """Stream the bytes of an uploaded file."""

    def read_google(self, file_id: str, mime_type: str) -> dict[str, Any]:
        """Raw Docs, Slides or Sheets JSON (shapes: converters' input)."""
