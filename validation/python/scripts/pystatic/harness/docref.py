"""Resolves a `doc: /path/to/file.md#anchor` finding reference to a real Markdown heading.

`heading_anchor` feeds `markdown-it-py`'s heading tokens through the GitHub-slug algorithm
instead of matching `#` by regex. References resolve under `DOCS_ROOT`: the kit's own
directory (its `README.md`), or `PYSTATIC_DOCS_ROOT` when a project re-points them."""

from __future__ import annotations

import os
import re
from functools import cache
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

DOCS_ROOT = Path(os.environ.get("PYSTATIC_DOCS_ROOT", Path(__file__).resolve().parents[2]))

_MD = MarkdownIt("gfm-like")
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
_ANCHOR_PUNCTUATION_RE = re.compile(r"[^\w\s-]")
_ANCHOR_WHITESPACE_RE = re.compile(r"\s")


def _blank_frontmatter(text: str) -> str:
    """Blank a leading `---` frontmatter block, keeping the line count, so a bare `#`
    inside it is never read as a heading."""
    match = _FRONTMATTER_RE.match(text)
    return text if not match else ("\n" * text.count("\n", 0, match.end())) + text[match.end() :]


def _heading_titles(text: str) -> list[str]:
    tree = SyntaxTreeNode(_MD.parse(_blank_frontmatter(text)))
    return [
        node.children[0].content
        for node in tree.walk(include_self=False)
        if node.type == "heading" and node.children
    ]


def heading_anchor(title: str) -> str:
    """GitHub-style heading slug: lowercase, strip punctuation, one hyphen per whitespace."""
    text = _ANCHOR_PUNCTUATION_RE.sub("", title.lower())
    return _ANCHOR_WHITESPACE_RE.sub("-", text.strip())


@cache
def _document_anchors(doc_path: Path) -> frozenset[str]:
    """Parse each document once per process."""
    text = doc_path.read_text(encoding="utf-8")
    return frozenset(heading_anchor(title) for title in _heading_titles(text))


def doc_anchor_exists(ref: str, docs_root: Path | None = None) -> bool:
    """True if `ref` (e.g. `/README.md#enum-literals`) names a real heading under `docs_root`."""
    if not ref.startswith("/") or "#" not in ref:
        return False
    rel_path, _, anchor = ref[1:].partition("#")
    doc_path = (DOCS_ROOT if docs_root is None else docs_root) / rel_path
    if not doc_path.is_file():
        return False
    return anchor in _document_anchors(doc_path)
