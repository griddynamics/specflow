"""The ``add_file`` of the fake import writers of the Box and Dropbox importer tests (no tests in here): it streams the
chunks, holds the size cap, tells bytes already seen from new ones and counts the calls open at once."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from api.imports import OriginalTooLargeError


class StreamingFileWriter:
    """Mixin of a fake writer that sets ``open``, ``peak``, ``hashes``, ``duplicates``, ``files`` and ``parsed``."""

    open: int
    peak: int
    hashes: set[str]
    duplicates: list[str]
    files: dict[str, tuple[str, bytes, str]]
    parsed: list[str]

    async def add_file(self, external_id: str, *, name: str, chunks: Any, media_type: str, max_bytes: int) -> str:
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            data = b""
            async for chunk in chunks:
                data += chunk
                await asyncio.sleep(0)
                if len(data) > max_bytes:
                    raise OriginalTooLargeError
        finally:
            self.open -= 1
        digest = hashlib.sha256(data).hexdigest()
        if digest in self.hashes:
            self.duplicates.append(external_id)
            return "duplicate"
        self.hashes.add(digest)
        self.files[external_id] = (name, data, media_type)
        self.parsed.append(external_id)
        return "added"
