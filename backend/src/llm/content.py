"""Provider-neutral content that can be attached to an LLM request.

An attachment is bytes plus a media type. Nothing here names a provider's wire
format: translating an attachment into provider-specific content blocks is the
job of that provider's client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PDF_MEDIA_TYPE = "application/pdf"
TEXT_MEDIA_TYPE = "text/plain"
IMAGE_MEDIA_TYPE_PREFIX = "image/"


@dataclass(frozen=True)
class Attachment:
    # Excluded from repr so a multi-megabyte payload never lands in logs or
    # tracebacks.
    data: bytes = field(repr=False)
    media_type: str
    name: str | None = None

    @classmethod
    def from_path(cls, path: Path, media_type: str | None = None) -> Attachment:
        data = path.read_bytes()
        resolved = media_type or detect_media_type(data)
        if resolved is None:
            raise ValueError(f"Cannot infer a media type for {path.name!r}; pass media_type explicitly.")
        return cls(data=data, media_type=resolved, name=path.name)


_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", PDF_MEDIA_TYPE),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_RIFF_HEADER_LENGTH = 12


def detect_media_type(data: bytes) -> str | None:
    """Return the media type detected from the content of ``data``, or ``None`` when unrecognised."""

    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    if data.startswith(b"RIFF") and data[8:_RIFF_HEADER_LENGTH] == b"WEBP":
        return "image/webp"
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return TEXT_MEDIA_TYPE if "\0" not in text else None
