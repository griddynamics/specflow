"""The engine used when a caller names none: one ``DoclingEngine`` per process.

Importing this module imports Docling and torch (the ``parse`` extra, worker-parse only). Only the parse
activity imports ``file_parsers.convert``, and only the worker-parse image runs that activity; the api and
worker-agents images never import this package's parser modules.
"""

from __future__ import annotations

import functools

from file_parsers.engines.docling import DoclingEngine
from file_parsers.engines.port import DocumentEngine


@functools.cache
def default_engine() -> DocumentEngine:
    """One ``DoclingEngine`` per process: it keeps its loaded models (about 4 s to load)."""

    return DoclingEngine()
