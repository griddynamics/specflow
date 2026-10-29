"""Type stub for the part of ``pymupdf4llm`` (2.0) this project calls.

project: the library ships no ``py.typed`` marker and no stub package exists on PyPI, so strict
mypy would skip it as ``import-untyped``. Only ``to_markdown`` (used by
``file_parsers.pdf.markdown_extractor``) is declared; Python ignores ``.pyi`` files at runtime.
"""

from typing import Any

import pymupdf

__version__: str
version: str

def to_markdown(
    doc: pymupdf.Document | str,
    *,
    page_chunks: bool = ...,
    write_images: bool = ...,
    embed_images: bool = ...,
    image_path: str = ...,
    image_format: str = ...,
    dpi: int = ...,
    pages: list[int] | None = ...,
    table_output: str = ...,
    **kwargs: Any,
) -> str | list[dict[str, Any]]: ...
