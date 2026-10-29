"""Workflow-id builders (SPECS section 11).

Pure and import-free so workflows can use them inside the sandbox. ``prefix``
is empty in production; tier-2 tests pass ``pytest-<run>-`` so every workflow
they start is recognisable and cleaned up.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID, uuid5

#: Ids per cleanup workflow (about 200 KB of payload, far under Temporal's 2 MB) [R07].
CLEANUP_CHUNK_SIZE = 5000


def parse_workflow_id(document_id: UUID | str, *, prefix: str = "") -> str:
    """``doc-parse-{document_id}``."""

    return f"{prefix}doc-parse-{document_id}"


def summary_workflow_id(document_id: UUID | str, *, prefix: str = "") -> str:
    """``doc-summary-{document_id}``."""

    return f"{prefix}doc-summary-{document_id}"


def drive_import_workflow_id(connection_id: UUID | str, *, prefix: str = "") -> str:
    """``drive-import-{connection_id}``: one import per source (INT-GDUI-0003)."""

    return f"{prefix}drive-import-{connection_id}"


def drive_document_id(connection_id: UUID, file_id: str) -> UUID:
    """The id of the document (and object path) of one Drive file; stable across retries."""

    return uuid5(connection_id, file_id)


def cleanup_workflow_id(removal_id: UUID | str, chunk: int, *, prefix: str = "") -> str:
    """``source-cleanup-{removal_id}-{chunk}``: one per chunk of one removal attempt (R07)."""

    return f"{prefix}source-cleanup-{removal_id}-{chunk}"


def chunk_ids[T](ids: Sequence[T], size: int = CLEANUP_CHUNK_SIZE) -> list[list[T]]:
    """``ids`` in order, split into lists of at most ``size`` (none for no ids) [R07]."""

    if size < 1:
        raise ValueError("chunk size must be at least 1")
    return [list(ids[start : start + size]) for start in range(0, len(ids), size)]
