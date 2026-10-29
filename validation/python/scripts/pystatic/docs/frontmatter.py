"""The frontmatter block: `pyyaml` is the reader of record; a lenient line reader tolerates an
unquoted colon in prose so one malformed value still yields every other field."""

from __future__ import annotations

import re
from typing import Any

import yaml

FRONTMATTER_RE = re.compile(r"\A---\n(.*?\n)---\n", re.DOTALL)
_FIELD_RE = re.compile(r"^([A-Za-z_][\w-]*):(.*)$")


class FrontmatterError(ValueError):
    """A frontmatter block is present but is not valid YAML (distinct from no block)."""


def blank_frontmatter(text: str) -> str:
    """Blank a leading `---` block, keeping the line count so line numbers still match."""
    m = FRONTMATTER_RE.match(text)
    return text if not m else ("\n" * text.count("\n", 0, m.end())) + text[m.end() :]


def frontmatter_from_text(text: str) -> dict[str, Any] | None:
    """Parsed frontmatter; `None` with no block, `{}` for a non-mapping block."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError as exc:
        raise FrontmatterError(str(exc).splitlines()[0]) from exc
    return data if isinstance(data, dict) else {}


def frontmatter_fields_lenient(text: str) -> dict[str, str] | None:
    """Top-level `key: value` pairs read line by line; `None` with no block."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    fields: dict[str, str] = {}
    for raw in m.group(1).splitlines():
        fm = _FIELD_RE.match(raw)
        if fm:
            fields[fm.group(1).strip()] = fm.group(2).strip().strip('"')
    return fields
