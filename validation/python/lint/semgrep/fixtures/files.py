"""Fixtures for semgrep/files.yml: positive and negative cases per rule."""

import mimetypes

# --- kit.files.mime-from-extension-guess ---


def classify_by_name(filename: str) -> str | None:
    # ruleid: kit.files.mime-from-extension-guess
    return mimetypes.guess_type(filename)[0]


def classify_by_content(head: bytes) -> str:
    # ok: kit.files.mime-from-extension-guess
    return "application/pdf" if head.startswith(b"%PDF-") else "application/octet-stream"
