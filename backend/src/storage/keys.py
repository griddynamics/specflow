"""Object key rules and builders (SPECS §7, AC-4.4)."""

from __future__ import annotations

from storage.base import InvalidObjectKeyError

MAX_KEY_BYTES = 1024
FIRST_PRINTABLE = 0x20
DELETE_CHAR = 0x7F


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def validate_key(key: str) -> str:
    """Return ``key`` unchanged if it is a safe relative object key.

    Rejected: empty, a leading ``/``, an empty segment (``a//b``, trailing ``/``),
    a ``.`` or ``..`` segment, a backslash, control characters, a key that is
    not encodable as UTF-8, and one longer than 1024 UTF-8 bytes (GCS limit).

    Not checked on purpose: whitespace-padded segments and slash look-alike
    characters; callers build keys from uuid ids.
    """

    if not _is_text(key):
        raise InvalidObjectKeyError("object key must be a non-empty string")
    try:
        encoded = key.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidObjectKeyError("object key is not valid UTF-8") from None
    if len(encoded) > MAX_KEY_BYTES:
        raise InvalidObjectKeyError("object key is longer than 1024 bytes")
    if key.startswith("/"):
        raise InvalidObjectKeyError("object key must be relative")
    if "\\" in key or any(ord(c) < FIRST_PRINTABLE or ord(c) == DELETE_CHAR for c in key):
        raise InvalidObjectKeyError("object key contains a forbidden character")
    for segment in key.split("/"):
        if segment == "":
            raise InvalidObjectKeyError("object key has an empty segment")
        if segment in (".", ".."):
            raise InvalidObjectKeyError("object key has a relative segment")
    return key


def _segment(name: str, value: str) -> str:
    if not _is_text(value) or "/" in value:
        raise InvalidObjectKeyError(f"{name} must be a single non-empty path segment")
    return value


def _original(project_id: str, document_id: str, filename: str) -> str:
    key = f"projects/{_segment('project_id', project_id)}/documents/{_segment('document_id', document_id)}/{filename}"
    return validate_key(key)


def document_original(project_id: str, document_id: str) -> str:
    """``projects/{pid}/documents/{did}/original.pdf`` (C10)."""

    return _original(project_id, document_id, "original.pdf")


def document_original_json(project_id: str, document_id: str) -> str:
    """``.../original.json``: the Google API response of a Drive Docs/Slides/Sheets file."""

    return _original(project_id, document_id, "original.json")


def document_original_bin(project_id: str, document_id: str) -> str:
    """``.../original.bin``: the bytes of a stored-only Drive file (not processed in v1)."""

    return _original(project_id, document_id, "original.bin")
