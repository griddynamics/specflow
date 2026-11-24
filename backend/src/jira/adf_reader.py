"""Atlassian Document Format to Markdown, for the importers (``jira.adf`` only builds documents).

Paragraphs, headings, bold, italic, code, strikethrough and links, bullet, ordered and task lists, tables, code blocks,
quotes, panels, expands, rules, mentions as names, emoji, dates, statuses and cards as links. A node this reader does
not know renders the text of its children, so nothing a person wrote is lost. Nothing here logs.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

_SPACES: Final = re.compile(r"[ \t]+")
_BREAK: Final = "\x00"  # a hard break in inline text, kept apart from the spaces the collapse folds
_AROUND_BREAK: Final = re.compile(r"[ \t]*\x00[ \t]*")
_MARKS: Final = {"strong": "**", "em": "*", "code": "`", "strike": "~~"}
_BLOCK_CONTAINERS: Final = frozenset(
    {
        "doc",
        "layoutSection",
        "layoutColumn",
        "mediaSingle",
        "mediaGroup",
        "decisionList",
        "extension",
        "bodiedExtension",
    }
)


def adf_to_markdown(doc: Any) -> str:
    """Markdown of an ADF document (blocks separated by a blank line); an empty or malformed value is ``""``."""

    if not isinstance(doc, Mapping):
        return ""
    return "\n\n".join(_blocks(_children(doc)))


def _children(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = node.get("content")
    return [child for child in content if isinstance(child, Mapping)] if isinstance(content, list) else []


def _attrs(node: Mapping[str, Any]) -> Mapping[str, Any]:
    attrs = node.get("attrs")
    return attrs if isinstance(attrs, Mapping) else {}


def _blocks(nodes: Sequence[Mapping[str, Any]]) -> list[str]:
    rendered = (_block(node) for node in nodes)
    return [text for text in rendered if text]


def _block(node: Mapping[str, Any]) -> str:
    renderer = _BLOCK_RENDERERS.get(str(node.get("type")))
    if renderer is not None:
        return renderer(node)
    if node.get("type") in _BLOCK_CONTAINERS or "content" in node:
        return _join(_children(node))
    return _inline([node])


def _join(nodes: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(_blocks(nodes))


def _paragraph(node: Mapping[str, Any]) -> str:
    return _inline(_children(node))


def _heading(node: Mapping[str, Any]) -> str:
    level = min(max(int(_attrs(node).get("level", 1) or 1), 1), 6)
    text = _inline(_children(node))
    return f"{'#' * level} {text}" if text else ""


def _bullets(node: Mapping[str, Any]) -> str:
    return "\n".join(_list(node, ""))


def _task_list(node: Mapping[str, Any]) -> str:
    return "\n".join(_tasks(node))


def _code_block(node: Mapping[str, Any]) -> str:
    text = "".join(str(child.get("text", "")) for child in _children(node))
    return f"```{_attrs(node).get('language', '') or ''}\n{text.strip(chr(10))}\n```"


def _quoted(node: Mapping[str, Any]) -> str:
    return _quote(_join(_children(node)))


def _expand(node: Mapping[str, Any]) -> str:
    title = str(_attrs(node).get("title", "") or "")
    return "\n\n".join(([f"**{title}**"] if title else []) + _blocks(_children(node)))


def _rule(_node: Mapping[str, Any]) -> str:
    return "---"


def _attachment(_node: Mapping[str, Any]) -> str:
    return "[attachment]"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n")) if text else ""


def _list(node: Mapping[str, Any], indent: str) -> list[str]:
    lines: list[str] = []
    ordered = node.get("type") == "orderedList"
    number = int(_attrs(node).get("order", 1) or 1)
    for item in _children(node):
        marker = f"{number}. " if ordered else "- "
        number += 1
        parts: list[str] = []
        nested: list[str] = []
        for child in _children(item):
            if child.get("type") in ("bulletList", "orderedList"):
                nested += _list(child, indent + " " * len(marker))
            elif rendered := _block(child):
                parts.append(rendered)
        head, *more = "\n".join(parts).split("\n") if parts else [""]
        lines.append(f"{indent}{marker}{head}".rstrip())
        lines += [f"{indent}{' ' * len(marker)}{line}" for line in more]
        lines += nested
    return lines


def _tasks(node: Mapping[str, Any]) -> list[str]:
    lines = []
    for item in _children(node):
        done = _attrs(item).get("state") == "DONE"
        lines.append(f"- [{'x' if done else ' '}] {_inline(_children(item))}".rstrip())
    return lines


def _table(node: Mapping[str, Any]) -> str:
    rows = [[_cell(cell) for cell in _children(row)] for row in _children(node)]
    rows = [row for row in rows if row]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = ["| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows]
    lines.insert(1, "|" + " --- |" * width)
    return "\n".join(lines)


def _cell(cell: Mapping[str, Any]) -> str:
    return " ".join(_blocks(_children(cell))).replace("\n", " ").replace("|", "\\|")


def _card(node: Mapping[str, Any]) -> str:
    url = str(_attrs(node).get("url", "") or "")
    return f"[{url}]({url})" if url else ""


_BLOCK_RENDERERS: Final = {
    "paragraph": _paragraph,
    "heading": _heading,
    "bulletList": _bullets,
    "orderedList": _bullets,
    "taskList": _task_list,
    "codeBlock": _code_block,
    "blockquote": _quoted,
    "panel": _quoted,
    "expand": _expand,
    "nestedExpand": _expand,
    "rule": _rule,
    "table": _table,
    "decisionItem": _paragraph,
    "listItem": _paragraph,
    "blockCard": _card,
    "embedCard": _card,
    "media": _attachment,
}


def _inline(nodes: Sequence[Mapping[str, Any]]) -> str:
    folded = _SPACES.sub(" ", "".join(_inline_node(node) for node in nodes))
    return _AROUND_BREAK.sub("  \n", folded).strip()


def _inline_node(node: Mapping[str, Any]) -> str:
    renderer = _INLINE_RENDERERS.get(str(node.get("type")))
    if renderer is not None:
        return renderer(node)
    return "".join(_inline_node(child) for child in _children(node)) or str(_attrs(node).get("text", "") or "")


def _text(node: Mapping[str, Any]) -> str:
    return _marked(str(node.get("text", "")), node.get("marks"))


def _hard_break(_node: Mapping[str, Any]) -> str:
    return _BREAK


def _mention(node: Mapping[str, Any]) -> str:
    return str(_attrs(node).get("text") or "@someone")


def _emoji(node: Mapping[str, Any]) -> str:
    attrs = _attrs(node)
    return str(attrs.get("text") or attrs.get("shortName") or "")


def _status(node: Mapping[str, Any]) -> str:
    return f"[{_attrs(node).get('text', '')}]"


def _date_node(node: Mapping[str, Any]) -> str:
    return _date(_attrs(node).get("timestamp"))


_INLINE_RENDERERS: Final = {
    "text": _text,
    "hardBreak": _hard_break,
    "mention": _mention,
    "emoji": _emoji,
    "inlineCard": _card,
    "status": _status,
    "date": _date_node,
    "media": _attachment,
}


def _marked(text: str, marks: Any) -> str:
    if not text or not isinstance(marks, list):
        return text
    names = {str(mark.get("type")): mark for mark in marks if isinstance(mark, Mapping)}
    for name, token in _MARKS.items():
        if name in names and text.strip():
            lead, core, tail = text[: len(text) - len(text.lstrip())], text.strip(), text[len(text.rstrip()) :]
            text = f"{lead}{token}{core}{token}{tail}"
    link = names.get("link")
    href = str(_attrs(link).get("href", "")) if link is not None else ""
    return f"[{text}]({href})" if href else text


def _date(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(int(timestamp) / 1000, UTC).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
