"""Notion-flavored Markdown (NFM v2) to plain GitHub-flavored Markdown (docs/notion/source.md "Conversion"). Stream B
fills this package; the prototype is ``plans/NOTION/prototype/nfm_to_gfm.py`` and the rules are the table of
``docs/notion/discovery.md``.

Rules the implementation keeps: a line-based pass with no HTML parser (``tree`` finds the blocks, ``inline`` the
inline tags, ``render`` writes GFM); no NFM tag is left in the output (outside fenced code); no e-mail is left in the
output or in ``scrub_original``; images are named, never fetched; the output is deterministic; input too deeply nested
raises ``MarkupTooDeepError`` (the import records ``convert_failed``, step ``convert``); hostile input (a tag injected
in text, a huge page: the text is cut at ``render.MAX_CHARS``) never raises anything else.
"""

from __future__ import annotations

from notion.convert.render import to_markdown
from notion.convert.result import ConvertedPage, MarkupTooDeepError
from notion.convert.row import render_row
from notion.convert.scrub import scrub_original

__all__ = ["ConvertedPage", "MarkupTooDeepError", "render_row", "scrub_original", "to_markdown"]
